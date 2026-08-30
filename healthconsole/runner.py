"""Running a catalogue action, once at a time, and recording what happened.

Separate from actions.py on purpose: the catalogue is data an auditor
reads, this is the machinery that acts on it.

Three properties this module exists to guarantee, in order of how bad it
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
   stopped.
3. `is_busy()` can say "free" only once (1) is already true for the
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
# kills, whether the timeout watchdog or an operator cancel triggered it:
# enough time for a well-behaved process to unwind, short enough that a
# stuck one is not left running for long.
KILL_GRACE_SECONDS = 2.0

# Extra headroom, beyond timeout_seconds + KILL_GRACE_SECONDS, that the
# worker thread waits for the child's stdout to reach EOF before giving
# up on it. Killing the whole process group (see _kill_group) should
# make every descendant that inherited the pipe exit well within this
# window; it exists only so a pathological escapee -- a grandchild that
# double-forked out of the group -- cannot keep the worker thread, and
# therefore the lock and the audit write, alive indefinitely.
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


def _kill_group(process: subprocess.Popen, grace: float) -> None:
    """Terminate a process and everything in its process group.

    `start_new_session=True` at Popen time makes `process.pid` the
    leader of its own process group, so this reaches grandchildren that
    inherited stdout (apt-get's helper processes, a shell's backgrounded
    jobs) -- not just the direct child. Signalling only the direct child
    can leave those descendants holding the pipe open forever: the
    direct child exits, but a read loop waiting for EOF never sees it.
    """
    if process.poll() is not None:
        return
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


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


def _drain(stream, buffer: _OutputBuffer,
          on_event: Callable[[dict], None], run_id: str) -> None:
    """Read a process's combined stdout/stderr, line by line.

    Runs in its own thread for two reasons: a caller callback that
    raises (an SSE listener whose browser tab closed, mid-output) must
    not unwind this loop and leave the rest of the output undrained and
    the child unaccounted for; and the worker thread waiting on the
    process itself (see ActionRunner._run) must never be blocked on
    drainage of a pipe some descendant process still holds open.
    """
    event_failed = False
    try:
        for line in stream:
            line = line.rstrip("\n")
            buffer.append(line)
            if not event_failed:
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
        run = _Run(run_id=secrets.token_hex(8))
        # Assigned synchronously, in the caller's thread, before the
        # worker thread exists: there is no window in which the lock is
        # held but self._current does not yet identify this run. That is
        # what lets cancel() act immediately and correctly instead of
        # guessing with a timed wait (see cancel()).
        self._current = run
        argv = (list(self._sudo) + list(action.argv)) if action.root \
            else list(action.argv)
        thread = threading.Thread(
            target=self._run, name=f"action-{run.run_id}",
            args=(run, action, source, argv, on_event), daemon=True)
        try:
            thread.start()
        except Exception:
            # thread.start() failing (e.g. the OS refusing to create a
            # new thread) must not leave the lock held with nothing
            # ever going to release it.
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
        """
        run = self._current
        if run is None:
            return
        if run_id is not None and run.run_id != run_id:
            return
        run.cancel_requested.set()
        process = run.process
        if process is not None:
            _kill_group(process, KILL_GRACE_SECONDS)
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
        """
        self.cancel()
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
                _kill_group(process, KILL_GRACE_SECONDS)

            watchdog = threading.Timer(
                self._timeout_seconds, self._on_timeout, args=(run,))
            watchdog.daemon = True
            watchdog.start()

            assert process.stdout is not None
            reader_thread = threading.Thread(
                target=_drain, name=f"action-{run.run_id}-reader",
                args=(process.stdout, buffer, on_event, run.run_id),
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

            if run.cancel_requested.is_set():
                note = "terminated by operator cancel"
            elif run.timed_out:
                note = (f"killed after the {self._timeout_seconds} s "
                        "timeout")
            elif returncode is not None and returncode < 0:
                note = f"killed by signal {-returncode}"

            if note is None and returncode is not None:
                exit_code = returncode
            # Any other outcome -- a negative returncode, a timeout, a
            # cancel, or giving up on a process that never confirmed it
            # exited -- has no real exit status to report; exit_code
            # stays None and `note` records why.
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if process is not None and process.stdout is not None:
                # If the reader thread above gave up on its join() still
                # blocked in a read (the pathological escapee
                # READ_BOUND_SECONDS guards against), closing the stream
                # here does not reliably unblock it on Linux -- the
                # syscall can keep waiting on the same fd regardless.
                # That leaked daemon thread is an accepted, narrow
                # residual cost of never letting drainage block this
                # method's own return.
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
        _kill_group(process, KILL_GRACE_SECONDS)
