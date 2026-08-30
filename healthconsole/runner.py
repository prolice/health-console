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
   stopped. When that cannot be confirmed -- permission denied, or a
   descendant that escaped the group entirely -- the row says so rather
   than claiming a clean kill it never verified.
3. The worker thread that owns (1) can never be blocked by drainage of
   the child's output: not the reading itself (a separate thread), and
   not closing the pipe afterwards (skipped while that thread is still
   using it). A property this module claimed once and had to re-learn:
   closing a text stream while another thread is blocked reading it
   blocks the closer too.
4. `is_busy()` can say "free" only once (1) is already true for the
   previous run -- otherwise a second run's row could land before the
   first's, and the log would no longer describe the order things
   actually happened in.
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

# Extra headroom, beyond timeout_seconds + KILL_GRACE_SECONDS, that the
# worker thread waits for the child itself to be confirmed dead before
# giving up on it. Killing the whole process group (see _kill_group)
# should make every descendant that inherited the pipe exit well within
# this window; it exists only so a pathological escapee -- a
# grandchild that double-forked out of the group, or one this process
# lacks permission to signal at all -- cannot keep the worker thread,
# and therefore the lock and the audit write, alive indefinitely.
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


def _kill_group(process: subprocess.Popen, grace: float) -> bool:
    """Terminate a process and everything in its process group.

    `start_new_session=True` at Popen time makes `process.pid` the
    leader of its own process group, so this reaches grandchildren that
    inherited stdout (apt-get's helper processes, a shell's backgrounded
    jobs) -- not just the direct child. Signalling only the direct child
    can leave those descendants holding the pipe open forever: the
    direct child exits, but a read loop waiting for EOF never sees it.

    Returns True only once the target is actually confirmed dead (or
    was already gone) -- never merely because a signal "was not
    refused". Two situations must not be reported as an ordinary,
    successful kill: `os.killpg` can fail outright with `PermissionError`
    (the expected case for `apt-get` running under `sudo -n` at uid 0,
    since this process does not), and a target can survive even SIGKILL
    within the confirmation window (a process wedged in an
    uninterruptible kernel wait). The caller decides how to record
    either -- this function's only job is to not paper over them.
    """
    if process.poll() is not None:
        return True
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return True  # already gone
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
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
    # Set when a kill was attempted (by cancel() or the timeout
    # watchdog) but _kill_group could not confirm the target actually
    # died -- see the module docstring's guarantee (2).
    kill_unconfirmed: bool = False


def _drain(stream, buffer: _OutputBuffer, on_event: Callable[[dict], None],
          run_id: str, stop_forwarding: threading.Event) -> None:
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
    after the "finished" event already has, breaking the ordering every
    caller of this module is entitled to rely on.
    """
    event_failed = False
    try:
        for line in stream:
            line = line.rstrip("\n")
            buffer.append(line)
            if not event_failed and not stop_forwarding.is_set():
                try:
                    on_event({"run_id": run_id, "phase": "output",
                              "line": line})
                except Exception:  # noqa: BLE001 -- caller's code, not ours
                    event_failed = True
                    _log_error(
                        f"on_event raised while streaming output for "
                        f"run {run_id}; no longer forwarding its output "
                        "events (still recording them for the audit row)")
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
        """
        run = self._current
        if run is None:
            return
        if run_id is not None and run.run_id != run_id:
            return
        run.cancel_requested.set()
        process = run.process
        if process is not None and not _kill_group(process,
                                                    KILL_GRACE_SECONDS):
            run.kill_unconfirmed = True
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
                if not _kill_group(process, KILL_GRACE_SECONDS):
                    run.kill_unconfirmed = True

            watchdog = threading.Timer(
                self._timeout_seconds, self._on_timeout, args=(run,))
            watchdog.daemon = True
            watchdog.start()

            assert process.stdout is not None
            reader_thread = threading.Thread(
                target=_drain, name=f"action-{run.run_id}-reader",
                args=(process.stdout, buffer, on_event, run.run_id,
                      stop_forwarding),
                daemon=True)
            reader_thread.start()

            # Bounded so this thread -- and therefore the lock and the
            # audit write -- cannot outlive the timeout even if some
            # descendant still holds the pipe open after _kill_group has
            # done everything it can (see READ_BOUND_SECONDS).
            hard_deadline = (start_monotonic + self._timeout_seconds
                             + KILL_GRACE_SECONDS + READ_BOUND_SECONDS)
            try:
                returncode = process.wait(
                    max(0.0, hard_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                returncode = None
                note = "gave up waiting for the process to exit"
            reader_thread.join(max(0.0, hard_deadline - time.monotonic()))

            if returncode is not None and returncode >= 0:
                # A genuine exit status was observed. Even if a cancel
                # or a timeout was also in flight, the process must have
                # already been exiting -- or already exited -- by the
                # time either landed: that status is the truth of what
                # happened, and a `note` claiming credit for ending the
                # run must never be allowed to throw it away (an
                # operator cancelling a run that had, unknown to them,
                # already finished must still see its real result).
                exit_code = returncode
                note = None
            elif note is None:
                if run.cancel_requested.is_set():
                    note = ("operator cancel attempted, but the process "
                            "could not be confirmed stopped"
                            if run.kill_unconfirmed
                            else "terminated by operator cancel")
                elif run.timed_out:
                    note = (
                        f"kill attempted after the {self._timeout_seconds} "
                        "s timeout, but the process could not be "
                        "confirmed stopped"
                        if run.kill_unconfirmed
                        else f"killed after the {self._timeout_seconds} "
                        "s timeout")
                elif returncode is not None and returncode < 0:
                    note = f"killed by signal {-returncode}"
            # Any remaining case -- a negative returncode with no
            # recorded cause, or "gave up waiting" left standing from
            # above -- has no real exit status to report; exit_code
            # stays None and `note` records why.
        finally:
            if watchdog is not None:
                watchdog.cancel()
            # Once this point is reached, the run has committed to
            # finalising: no further "output" event may reach on_event,
            # or a line the reader thread was mid-read on could be
            # delivered after "finished" already has.
            stop_forwarding.set()
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
        if not _kill_group(process, KILL_GRACE_SECONDS):
            run.kill_unconfirmed = True
