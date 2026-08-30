"""Running a catalogue action, once at a time, and recording what happened.

Separate from actions.py on purpose: the catalogue is data an auditor
reads, this is the machinery that acts on it.

Four properties this module exists to guarantee, in order of how bad it
is to get them wrong:

1. Exactly one `action_run` row is written for every run -- success,
   non-zero exit, missing binary, timeout, or an operator cancel -- no
   matter what a caller-supplied `on_event` callback does, and no matter
   what the store does. A run that produces no row is indistinguishable,
   from the one place an operator can look, from nothing having
   happened.
2. A run that times out actually ends: not just its direct child, but
   the whole process group, so a descendant that inherited stdout (an
   apt-get helper, a backgrounded shell job) cannot keep a privileged
   process running unbounded after the console has told itself it
   stopped. Whether that is confirmed is decided fresh, at the moment
   the row is written, by actually probing the process group -- never
   from a flag frozen a few seconds earlier, which can already be wrong
   by the time anyone reads it.
3. The worker thread that owns (1) can never be blocked by drainage of
   the child's output: not the reading itself (a separate thread), not
   closing the pipe afterwards (skipped while that thread is still using
   it), and not by how long some *unrelated* descendant takes to let go
   of the pipe once the actual child is already gone -- the read bound
   restarts from the moment the child is reaped, not from the run's own
   (possibly 30-minute) timeout.
4. `is_busy()` can say "free" only once (1) is already true for the
   previous run -- otherwise a second run's row could land before the
   first's, and the log would no longer describe the order things
   actually happened in. The same ordering applies to events: an
   "output" event can never reach a caller after "finished" has.
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

from healthconsole.actions import Action
from healthconsole.store import Store

# Grace period between SIGTERM and SIGKILL for any process this module
# kills, whether the timeout watchdog or an operator cancel triggered it
# -- and, separately, the bound each of those two signals is given to be
# confirmed before giving up on knowing whether it landed. Enough time
# for a well-behaved process to unwind, short enough that a stuck one is
# not left running, or left unconfirmed, for long.
KILL_GRACE_SECONDS = 2.0

# How long the worker thread waits for the reader thread to see EOF
# after the child itself has been reaped (the ordinary case), or after
# giving up on waiting for the child at all (see hard_deadline in
# _run()). Killing the whole process group (see _kill_group) should make
# every descendant that inherited the pipe exit well within this window;
# it exists only so a pathological escapee -- a grandchild that
# double-forked out of the group, or one this process lacks permission
# to signal at all -- cannot keep the worker thread, and therefore the
# lock and the audit write, alive indefinitely. Measured from the moment
# the child is reaped, not from the run's start: a run whose direct
# child exits in milliseconds must not hold the lock for the rest of a
# 30-minute timeout merely because some unrelated descendant is still
# holding the pipe open.
READ_BOUND_SECONDS = 5.0

# How many lines of captured output to keep at the start and the end of
# a run. A chatty action must not be able to buffer unbounded output in
# RAM and then attempt one oversized INSERT -- a failure that would land
# on the very error path Critical 1 exists to guard.
OUTPUT_HEAD_LINES = 200
OUTPUT_TAIL_LINES = 200


class ActionBusy(Exception):
    """Raised by start() when a run is already in flight."""


def _log_error(message: str) -> None:
    # No logging framework exists elsewhere in this codebase (see
    # store.py's corruption handling) -- stderr, with the same prefix,
    # is the existing convention.
    print(f"health-console: {message}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)


def _kill_group(process: subprocess.Popen, grace: float) -> bool | None:
    """Terminate a process and everything in its process group.

    `start_new_session=True` at Popen time makes `process.pid` the
    leader of its own process group, so this reaches grandchildren that
    inherited stdout (apt-get's helper processes, a shell's backgrounded
    jobs) -- not just the direct child. Signalling only the direct child
    can leave those descendants holding the pipe open forever: the
    direct child exits, but a read loop waiting for EOF never sees it.

    Returns `None` if there was nothing to do -- the process was already
    dead when checked, so no signal was ever dispatched, and neither a
    cancel nor a timeout can claim credit for anything that happened to
    it. Otherwise returns whether the target could be confirmed dead
    within this call's own short, bounded wait. That result is a
    preliminary signal only, useful for logging -- the row this
    function's callers eventually write is decided from a *fresh* check
    at audit-write time (see `_group_is_dead`), not from a bool frozen
    here: `process.wait()` only ever confirms the direct child, and an
    in-group sibling that ignored the signal (or one that dies a moment
    after this function gives up) would make a value cached here already
    wrong by the time anyone reads it.
    """
    if process.poll() is not None:
        return None
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return None
    except OSError as exc:
        _log_error(f"could not resolve the process group id: {exc}")
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    except OSError as exc:
        # PermissionError (a subclass of OSError) is EPERM: no member of
        # the group is signalable by this uid. This is the expected
        # outcome for the one privileged action this console ships, not
        # a bug to let propagate into the caller.
        _log_error(f"could not send SIGTERM to the process group: {exc}")
        return False
    try:
        process.wait(grace)
        return True
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError as exc:
        _log_error(f"could not send SIGKILL to the process group: {exc}")
        return False
    try:
        process.wait(grace)
        return True
    except subprocess.TimeoutExpired:
        return False


def _group_is_dead(process: subprocess.Popen) -> bool:
    """A fresh, at-write-time check of whether the whole process group
    is gone -- not a flag frozen inside `_kill_group`'s own confirmation
    wait, which can already be stale by the time a row is written.

    `process.poll()` alone only confirms the direct child: a sibling in
    the same group that ignored the group's SIGTERM (`trap "" TERM`, or
    one simply not yet reaped) can still be alive after the direct
    child's own exit is observed. `os.killpg(pid, 0)` -- signal 0
    delivers nothing, it only checks whether the target exists and is
    signalable -- catches that: `ProcessLookupError` (ESRCH) means the
    group is entirely gone; success, or `PermissionError` (EPERM,
    meaning it exists but this process cannot touch it -- the expected
    case for `apt-get` still running under `sudo -n` at uid 0), both
    mean there is no evidence of death and this must not report one.
    """
    if process.poll() is None:
        return False
    try:
        # start_new_session=True at Popen time made this process's own
        # pid double as its process group id; no separate os.getpgid()
        # lookup is needed here, and after reaping one would risk
        # querying a pid the OS has since reused for something
        # unrelated.
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        # PermissionError (still exists, not ours to touch) or any
        # other unexpected error: neither is evidence of death.
        return False
    else:
        return False


class _OutputBuffer:
    """Bounds the memory a run's captured output can occupy.

    Keeps the first OUTPUT_HEAD_LINES and the last OUTPUT_TAIL_LINES,
    with a marker recording how many lines were dropped in between --
    the shape most useful for diagnosing a runaway action, without
    holding an unbounded amount of it in RAM first.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._head: list[str] = []
        self._tail: list[str] = []
        self._total = 0

    def append(self, line: str) -> None:
        with self._lock:
            self._total += 1
            if len(self._head) < OUTPUT_HEAD_LINES:
                self._head.append(line)
                return
            self._tail.append(line)
            if len(self._tail) > OUTPUT_TAIL_LINES:
                self._tail.pop(0)

    def render(self) -> str:
        with self._lock:
            dropped = self._total - len(self._head) - len(self._tail)
            parts = list(self._head)
            if dropped > 0:
                parts.append(f"[... {dropped} lines omitted ...]")
            parts.extend(self._tail)
            return "\n".join(parts)


@dataclass
class _Run:
    """State for the one run currently in flight.

    Shared between the caller's thread (start(), cancel()) and the
    worker thread (_run()). Scoping this per-run -- rather than as
    fields directly on ActionRunner, or a single shared "process
    started" flag -- is what makes cancel() unable to ever act on the
    wrong run: each run gets its own identity and its own
    cancel-requested flag, so a cancel() call that arrives before a
    process exists, or after this run has already finished, cannot be
    mistaken for a later, unrelated run.
    """

    run_id: str
    process: subprocess.Popen | None = None
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    timed_out: bool = False
    # True only once a signal was actually dispatched to a process that
    # was still alive at the time -- as opposed to `cancel_requested` or
    # `timed_out`, which record intent (the operator clicked cancel; the
    # watchdog fired) regardless of whether there was anything left to
    # kill. A cancel or a timeout that finds the process already exited
    # must not claim credit for ending it; this flag is what tells the
    # note-computation logic the difference.
    kill_attempted: bool = False


def _drain(stream, buffer: _OutputBuffer, on_event: Callable[[dict], None],
          run_id: str, stop_forwarding: threading.Event,
          event_order_lock: threading.Lock) -> None:
    """Read a process's combined stdout/stderr, line by line.

    Runs in its own thread for two reasons: a caller callback that
    raises (an SSE listener whose browser tab closed, mid-output) must
    not unwind this loop and leave the rest of the output undrained and
    the child unaccounted for; and the worker thread waiting on the
    process itself (see ActionRunner._run) must never be blocked on
    drainage of a pipe some descendant process still holds open --
    including, critically, when that thread later closes the stream:
    this loop may still be blocked inside a read on it, and that close()
    call blocks until the read returns (see _run's finally block).

    `stop_forwarding` is set once the run has committed to finalising --
    after which no further "output" event may reach `on_event`, or a
    line delivered by a lingering read (one this thread was still
    blocked in when the worker gave up waiting for it) could arrive
    after the "finished" event already has. Checking it and calling
    `on_event` is not enough on its own to guarantee that ordering,
    though: "check, then act" has a window between the two in which the
    finalising thread can run entirely (set the flag, emit "finished")
    before this thread's already-in-flight decision to call `on_event`
    executes. `event_order_lock`, held by both this check-then-call and
    the finalising thread's set-then-emit (see _run), closes that
    window: whichever side gets the lock first completes its whole
    step before the other can start.
    """
    event_failed = False
    try:
        for line in stream:
            line = line.rstrip("\n")
            buffer.append(line)
            with event_order_lock:
                if not event_failed and not stop_forwarding.is_set():
                    try:
                        on_event({"run_id": run_id, "phase": "output",
                                  "line": line})
                    except Exception:  # noqa: BLE001 -- caller's code
                        event_failed = True
                        _log_error(
                            f"on_event raised while streaming output "
                            f"for run {run_id}; no longer forwarding "
                            "its output events (still recording them "
                            "for the audit row)")
    except Exception:  # noqa: BLE001
        # Reading itself should not raise -- errors="replace" at Popen
        # time rules out UnicodeDecodeError -- but this loop must not be
        # the reason a run's audit row goes missing if something
        # unexpected does happen here.
        _log_error(f"reading output for run {run_id} raised unexpectedly")


class ActionRunner:
    """Runs one catalogue action at a time and audits the outcome.

    Only one run is ever in flight: `start()` takes a non-blocking lock
    and raises `ActionBusy` if another run holds it. Whatever happens to
    the child process -- it exits cleanly, it exits with an error, the
    binary does not exist, it outlives its timeout, or an operator
    cancels it -- exactly one `action_run` row is written before the
    lock is released, and neither a broken `on_event` callback nor a
    failing store write can prevent that release. See the module
    docstring for why that guarantee is the point of this class.
    """

    def __init__(self, store: Store, timeout_seconds: int = 1800,
                sudo: tuple[str, ...] = ("/usr/bin/sudo", "-n")) -> None:
        self._store = store
        self._timeout_seconds = timeout_seconds
        self._sudo = sudo
        self._lock = threading.Lock()
        self._current: _Run | None = None
        self._thread: threading.Thread | None = None

    def is_busy(self) -> bool:
        # Reflects whether the lock is held, not whether a process
        # object exists: the lock is only released once the audit row
        # has been written (or the write has failed and been logged) and
        # the "finished" event has been delivered, so "not busy" always
        # means the previous run is fully accounted for.
        acquired = self._lock.acquire(blocking=False)
        if acquired:
            self._lock.release()
        return not acquired

    def start(self, action: Action, source: str,
             on_event: Callable[[dict], None]) -> str:
        if not self._lock.acquire(blocking=False):
            raise ActionBusy(action.id)
        # Everything from here on, including building argv and the
        # Thread object -- not just thread.start() -- runs inside this
        # try: an exception anywhere in this window must still release
        # the lock, or it leaks permanently with nothing left to try
        # another run.
        try:
            run = _Run(run_id=secrets.token_hex(8))
            # Assigned synchronously, in the caller's thread, before the
            # worker thread exists: there is no window in which the
            # lock is held but self._current does not yet identify this
            # run. That is what lets cancel() act immediately and
            # correctly instead of guessing with a timed wait (see
            # cancel()).
            self._current = run
            argv = (list(self._sudo) + list(action.argv)) if action.root \
                else list(action.argv)
            thread = threading.Thread(
                target=self._run, name=f"action-{run.run_id}",
                args=(run, action, source, argv, on_event), daemon=True)
            thread.start()
        except Exception:
            self._current = None
            self._lock.release()
            raise
        self._thread = thread
        return run.run_id

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def cancel(self, run_id: str | None = None) -> None:
        """Terminate the run in flight, if any.

        Sets a per-run flag rather than reaching for a process directly
        and waiting to see if one shows up: the process may not exist
        yet (still inside Popen(), or about to raise OSError for a
        missing binary), and a timed wait for it to appear can only ever
        guess how long that takes. An earlier version of this method
        used a one-second wait, which was wrong twice over: it left a
        run uncancellable for up to a second if Popen() legitimately
        took longer than that, and -- worse -- if the run it was meant
        to cancel had already failed and finished within that second,
        the wait would wake up, find a *different*, newer run in
        self._current, and terminate that one instead. Passing run_id
        (when the caller has it) makes that misfire structurally
        impossible; the no-argument form keeps cancelling "whatever is
        current" for callers that do not.

        Never raises: `_kill_group` reports failure (a permission
        error, or a kill that could not be confirmed) through its
        return value rather than an exception, so a caller wiring this
        into an HTTP handler cannot turn "the operator clicked cancel"
        into a 500, and shutdown() can always reach its own wait().

        Blocks its caller for up to roughly 2 * KILL_GRACE_SECONDS (SIGTERM,
        wait, SIGKILL, wait again) when there is a live process to act
        on -- a few seconds, not instant. Whoever wires this into an
        HTTP handler (Task 6) should account for that latency rather
        than assume cancel() returns immediately.
        """
        run = self._current
        if run is None:
            return
        if run_id is not None and run.run_id != run_id:
            return
        run.cancel_requested.set()
        process = run.process
        if process is not None:
            result = _kill_group(process, KILL_GRACE_SECONDS)
            if result is not None:
                run.kill_attempted = True
        # If process is still None, Popen() has not returned yet (or is
        # about to raise). The worker checks cancel_requested itself,
        # immediately after assigning run.process, so this is not a
        # missed cancellation -- just one the worker finishes on our
        # behalf, the instant it can.

    def shutdown(self, timeout: float = 10.0) -> None:
        """Cancel any run in flight and wait for it to end.

        Must be called, and given time to complete, before the Store
        this runner writes to is closed. A write landing after
        Store.close() raises sqlite3.ProgrammingError; the guards in
        _run()'s finally block mean that no longer wedges the runner,
        but the row would still be lost, and a privileged child process
        would otherwise be left running past the console's own exit.
        Whoever wires this module into the server's shutdown path must
        call this before store.close().

        cancel() is not expected to raise (see its docstring), but this
        method reaches self.wait() unconditionally regardless -- a
        shutdown path exists precisely to run when something has
        already gone wrong, and it must not itself become the reason
        the store closes out from under a run still in flight.
        """
        try:
            self.cancel()
        except Exception:  # noqa: BLE001
            _log_error(
                "cancel() raised during shutdown; still waiting for the "
                "run in flight to end")
        self.wait(timeout)

    def _run(self, run: _Run, action: Action, source: str,
             argv: list[str], on_event: Callable[[dict], None]) -> None:
        start_wall = time.time()
        start_monotonic = time.monotonic()
        buffer = _OutputBuffer()
        exit_code: int | None = None
        note: str | None = None
        watchdog: threading.Timer | None = None
        process: subprocess.Popen | None = None
        reader_thread: threading.Thread | None = None
        stop_forwarding = threading.Event()
        event_order_lock = threading.Lock()
        try:
            self._emit(on_event, run.run_id, "started",
                      action_id=action.id)
            try:
                process = subprocess.Popen(
                    argv, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, shell=False,
                    # Decoding is this module's job, not the store's: a
                    # broken locale can make apt-get emit invalid UTF-8,
                    # and letting that raise out of the reader thread
                    # would lose the audit row for the run most worth
                    # recording. A mangled character beats a missing row.
                    errors="replace",
                    # Leader of its own process group, so a timeout or a
                    # cancel can reach every descendant that inherits
                    # stdout (see _kill_group), not just this direct
                    # child.
                    start_new_session=True)
            except OSError as exc:
                # The binary does not exist, or is not executable. This
                # is audited the same as any other outcome, not raised:
                # a catalogue entry pointing at a missing binary must
                # leave a trace, not vanish.
                buffer.append(str(exc))
                return

            run.process = process
            if run.cancel_requested.is_set():
                # cancel() ran before Popen() returned. Honour it the
                # instant the process exists, rather than leaving it to
                # run to its full timeout unrescued.
                if _kill_group(process, KILL_GRACE_SECONDS) is not None:
                    run.kill_attempted = True

            watchdog = threading.Timer(
                self._timeout_seconds, self._on_timeout, args=(run,))
            watchdog.daemon = True
            watchdog.start()

            assert process.stdout is not None
            reader_thread = threading.Thread(
                target=_drain, name=f"action-{run.run_id}-reader",
                args=(process.stdout, buffer, on_event, run.run_id,
                      stop_forwarding, event_order_lock),
                daemon=True)
            reader_thread.start()

            # The absolute, worst-case bound: even if nothing above ever
            # confirms anything, this thread -- and therefore the lock
            # and the audit write -- cannot outlive it.
            hard_deadline = (start_monotonic + self._timeout_seconds
                             + KILL_GRACE_SECONDS + READ_BOUND_SECONDS)
            try:
                returncode = process.wait(
                    max(0.0, hard_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                returncode = None
                # The child itself was never confirmed dead within the
                # full hard deadline; there is nothing left to wait for
                # beyond it.
                read_deadline = hard_deadline
            else:
                # The child has been reaped. From this moment, give the
                # reader READ_BOUND_SECONDS of its own to drain and see
                # EOF -- not whatever happens to be left of
                # timeout_seconds's own hard deadline. A run whose
                # direct child exits in milliseconds must not hold the
                # lock for the rest of a 30-minute timeout merely
                # because some unrelated descendant that escaped the
                # process group is still holding the pipe open: nothing
                # privileged is even alive at that point.
                read_deadline = min(
                    hard_deadline, time.monotonic() + READ_BOUND_SECONDS)
            reader_thread.join(max(0.0, read_deadline - time.monotonic()))

            if returncode is not None and returncode >= 0:
                # A genuine exit status was observed. It is the truth of
                # what happened and is never thrown away -- but if a
                # signal really was dispatched to a still-live process
                # (run.kill_attempted; see its field comment for why
                # this is not just cancel_requested/timed_out), that is
                # useful context worth keeping *alongside* the exit
                # code, not silently dropped: a child that traps SIGTERM
                # and exits 0 in response to a timeout kill is not the
                # same event as one that simply finished on its own.
                exit_code = returncode
                if run.kill_attempted:
                    if run.cancel_requested.is_set():
                        note = (f"exited {returncode} after an operator "
                                "cancel sent a kill signal")
                    elif run.timed_out:
                        note = (
                            f"exited {returncode} after the "
                            f"{self._timeout_seconds} s timeout sent a "
                            "kill signal")
            else:
                # No genuine exit status. Whether a cancel or a timeout
                # gets to claim the outcome is decided by
                # run.kill_attempted -- a signal actually reached a
                # still-live process -- not by the bare intent flags: a
                # cancel() or a watchdog firing after the process had
                # already exited finds nothing left to kill and must not
                # be credited with ending it. "Confirmed" is recomputed
                # fresh, right here, from the process group's actual
                # state -- never trusted from a flag _kill_group froze
                # a couple of seconds earlier, which only ever reflected
                # the direct child and can already be wrong by now (see
                # _group_is_dead).
                confirmed = process is not None and _group_is_dead(process)
                if run.cancel_requested.is_set() and run.kill_attempted:
                    note = ("terminated by operator cancel" if confirmed
                            else "operator cancel attempted, but the "
                            "process could not be confirmed stopped")
                elif run.timed_out and run.kill_attempted:
                    note = (
                        f"killed after the {self._timeout_seconds} s "
                        "timeout" if confirmed else
                        "kill attempted after the "
                        f"{self._timeout_seconds} s timeout, but the "
                        "process could not be confirmed stopped")
                elif returncode is not None and returncode < 0:
                    note = f"killed by signal {-returncode}"
                elif confirmed:
                    note = ("the process exited, but its exit status "
                            "could not be observed")
                else:
                    note = "the process could not be confirmed stopped"
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if reader_thread is not None and reader_thread.is_alive():
                # Still blocked reading -- almost certainly a descendant
                # that escaped the process group and still holds the
                # pipe open, since a well-behaved run's reader finishes
                # within moments of the process dying. Do NOT close
                # process.stdout here: TextIOWrapper.close() takes the
                # same buffer lock a blocked readline() holds and waits
                # for it to return, which would block *this* thread for
                # as long as the reader stays blocked -- reinstating the
                # exact wedge this method exists to prevent. Leave the
                # fd to the daemon thread; the OS reclaims it when this
                # process exits, or the thread finishes on its own if
                # the escapee eventually produces EOF.
                pass
            elif process is not None and process.stdout is not None:
                try:
                    process.stdout.close()
                except Exception:  # noqa: BLE001
                    pass
            if note is not None:
                buffer.append(f"[{note}]")
            duration_ms = int((time.monotonic() - start_monotonic) * 1000)
            try:
                try:
                    self._store.write_action_run(
                        run.run_id, int(start_wall), action.id, source,
                        exit_code, duration_ms, buffer.render())
                except Exception:  # noqa: BLE001
                    # A write failure (disk full, or the store closing
                    # under us during shutdown) must never wedge the
                    # runner: the lock is released below regardless.
                    # Losing this one row is a real loss; a runner that
                    # can never start another action afterwards would be
                    # worse -- indistinguishable, from then on, from an
                    # action stuck forever.
                    _log_error(
                        f"failed to write the action_run row for "
                        f"run {run.run_id}")
                with event_order_lock:
                    # Set and emit under the same lock _drain checks and
                    # calls under: whichever side gets here first
                    # completes its whole step (check-then-call, or
                    # this set-then-emit) before the other can start, so
                    # a straggling "output" event can never be delivered
                    # after "finished" already has.
                    stop_forwarding.set()
                    self._emit(on_event, run.run_id, "finished",
                              exit_code=exit_code)
            finally:
                # Whatever happened above -- including a bug in this
                # very block -- the lock must still be released. This is
                # the property Critical 1 exists to restore: a run that
                # cannot be audited must still not wedge the next one.
                self._current = None
                self._lock.release()

    @staticmethod
    def _emit(on_event: Callable[[dict], None], run_id: str,
              phase: str, **fields) -> None:
        # `on_event` is a callback into code this module does not
        # control -- an SSE handler writing to a socket whose other end
        # may have gone away. Letting an exception from it escape here
        # would, depending on where it happened, either skip the audit
        # write entirely or leave the lock held forever (see the
        # module's "started"/"finished" call sites). Neither is
        # acceptable, so it is always swallowed and logged instead.
        try:
            on_event({"run_id": run_id, "phase": phase, **fields})
        except Exception:  # noqa: BLE001
            _log_error(
                f"on_event raised while emitting '{phase}' for "
                f"run {run_id}; ignoring it")

    def _on_timeout(self, run: _Run) -> None:
        process = run.process
        if process is None or process.poll() is not None:
            return
        run.timed_out = True
        if _kill_group(process, KILL_GRACE_SECONDS) is not None:
            run.kill_attempted = True
