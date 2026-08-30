"""Running a catalogue action, once at a time, and recording what happened.

Separate from actions.py on purpose: the catalogue is data an auditor
reads, this is the machinery that acts on it.
"""

from __future__ import annotations

import secrets
import subprocess
import threading
import time
from typing import Callable

from healthconsole.actions import Action
from healthconsole.store import Store

# Grace period between SIGTERM and SIGKILL for a run that outlived its
# timeout: enough time for a well-behaved process to unwind, short enough
# that a stuck one is not left running for long.
KILL_GRACE_SECONDS = 2.0


class ActionBusy(Exception):
    """Raised by start() when a run is already in flight."""


class ActionRunner:
    """Runs one catalogue action at a time and audits the outcome.

    Only one run is ever in flight: `start()` takes a non-blocking lock and
    raises `ActionBusy` if another run holds it. Whatever happens to the
    child process -- it exits cleanly, it exits with an error, the binary
    does not exist, or it outlives its timeout and is killed -- exactly one
    `action_run` row is written, in a `finally`, before the lock is released.
    A run that leaves no row would make the audit log indistinguishable
    from nothing having happened, which is the one failure this class must
    never produce.
    """

    def __init__(self, store: Store, timeout_seconds: int = 1800,
                sudo: tuple[str, ...] = ("/usr/bin/sudo", "-n")) -> None:
        self._store = store
        self._timeout_seconds = timeout_seconds
        self._sudo = sudo
        self._lock = threading.Lock()
        self._current: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        # Set once the worker thread has assigned self._current for the
        # run presently holding the lock. Lets cancel() distinguish "no
        # run is in flight" from "a run is in flight but hasn't reached
        # Popen() yet" -- see cancel().
        self._process_started = threading.Event()

    def is_busy(self) -> bool:
        # Reflects whether the lock is held, not whether a process object
        # exists: the lock is only released in the worker's `finally`,
        # after the audit row is written, so "not busy" always means the
        # row for the previous run is already there.
        acquired = self._lock.acquire(blocking=False)
        if acquired:
            self._lock.release()
        return not acquired

    def start(self, action: Action, source: str,
             on_event: Callable[[dict], None]) -> str:
        if not self._lock.acquire(blocking=False):
            raise ActionBusy(action.id)
        self._process_started.clear()
        run_id = secrets.token_hex(8)
        argv = (list(self._sudo) + list(action.argv)) if action.root \
            else list(action.argv)
        thread = threading.Thread(
            target=self._run, name=f"action-{run_id}",
            args=(run_id, action, source, argv, on_event), daemon=True)
        self._thread = thread
        thread.start()
        return run_id

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def cancel(self) -> None:
        # Terminates the run in flight, if any. Used by the busy-refusal
        # test to release the lock, and by shutdown so an action still
        # running when the console stops is terminated rather than
        # orphaned.
        process = self._current
        if process is None and self.is_busy():
            # The lock is held but the worker hasn't assigned
            # self._current yet -- it is still inside (or about to
            # enter) Popen(). Without this wait, cancel() would silently
            # do nothing to a run that, from the caller's point of view,
            # has already started, leaving its child process to run to
            # completion unbounded. A real Popen() of a catalogue binary
            # takes microseconds, so one second is generous headroom,
            # not a real delay on the common "nothing to cancel" path
            # (which never reaches this branch, since is_busy() is false
            # there).
            self._process_started.wait(1.0)
            process = self._current
        if process is not None and process.poll() is None:
            process.terminate()

    def _run(self, run_id: str, action: Action, source: str,
             argv: list[str], on_event: Callable[[dict], None]) -> None:
        start_ts = time.time()
        lines: list[str] = []
        exit_code: int | None = None
        watchdog: threading.Timer | None = None
        process: subprocess.Popen | None = None
        try:
            on_event({"run_id": run_id, "phase": "started",
                      "action_id": action.id})
            try:
                process = subprocess.Popen(
                    argv, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, shell=False,
                    # Decoding is this module's job, not the store's: a
                    # broken locale can make apt-get emit invalid UTF-8,
                    # and letting that raise out of this thread would lose
                    # the audit row for the very run most worth recording.
                    # A mangled character is a far smaller cost than a
                    # silently missing row.
                    errors="replace")
            except OSError as exc:
                # The binary does not exist, or is not executable. This is
                # audited the same as any other outcome, not raised: a
                # catalogue entry pointing at a missing binary must leave
                # a trace, not vanish.
                lines.append(str(exc))
                return

            self._current = process
            self._process_started.set()
            watchdog = threading.Timer(
                self._timeout_seconds, self._on_timeout, args=(process,))
            watchdog.daemon = True
            watchdog.start()

            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\n")
                lines.append(line)
                on_event({"run_id": run_id, "phase": "output",
                          "line": line})
            returncode = process.wait()
            # A negative return code means the process died to a signal
            # rather than exiting on its own -- our own timeout/cancel
            # enforcement below, or something external. Either way there
            # is no exit status to report, so this records None rather
            # than a fabricated negative "exit code" nobody chose.
            exit_code = returncode if returncode >= 0 else None
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if process is not None and process.stdout is not None:
                # The reader loop above stops at EOF but never closes the
                # pipe's file object itself; leaving it open trips a
                # ResourceWarning and, over many runs, leaks descriptors.
                process.stdout.close()
            self._current = None
            duration_ms = int((time.time() - start_ts) * 1000)
            output = "\n".join(lines)
            # The audit row is written before the lock is released and
            # before the "finished" event fires: a caller that sees
            # is_busy() go false, or receives the finished event, must
            # already be able to find this run in the log.
            self._store.write_action_run(
                run_id, int(start_ts), action.id, source, exit_code,
                duration_ms, output)
            # The "finished" event fires before the lock is released, so
            # that an SSE listener seeing that event, or a caller checking
            # is_busy() right after, are never out of step: both channels
            # agree the run is over at the same instant.
            on_event({"run_id": run_id, "phase": "finished",
                      "exit_code": exit_code})
            self._lock.release()

    def _on_timeout(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
