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
3. THE RULE, in the only form that has survived a review:

       No I/O of any kind -- including this module's own logging --
       under `_cv` or `_OutputBuffer._lock`, and none between the audit
       write and `self._lock.release()`.

   Read that as a grep, not as an argument. It is checkable by looking
   at five lines. Every earlier wording of it required reasoning about
   four threads at once, and four successive rounds of fixes each got
   that reasoning right for the case in front of them and wrong
   somewhere new:

   - a blocking read on the worker thread;
   - then a `close()` that waits on that read;
   - then a lock held across the caller's own `on_event` which the
     worker had to acquire before it could release the run lock;
   - then this module's *own* `_log` call, under `_cv`, on the
     reader's queue-drop path. A write to a stderr nobody is draining
     is an unbounded wait -- a full pipe blocks forever -- so that left
     the reader holding the very mutex the worker takes in `close()`.

   Each is the same thing: an unbounded wait reachable from the thread
   that owes the world an audit row. Only three categories can produce
   one here -- the caller's `on_event`, reads of the child's pipe, and
   I/O this module performs itself -- and all three are now off that
   path. `on_event` is called from exactly one place,
   `_EventStream._pump`, on a thread of its own that holds no lock
   while it calls and is neither the worker nor the reader. The pipe is
   read only by `_drain`, and every wait on it is bounded. Logging
   happens only outside both mutexes, and in the worker only after
   `self._lock.release()`. `tests/test_runner.py` enforces the rule
   mechanically, by parsing this file, rather than by asking a reader
   to notice.
4. `is_busy()` can say "free" only once (1) is already true for the
   previous run -- otherwise a second run's row could land before the
   first's, and the log would no longer describe the order things
   actually happened in. Events keep their own ordering guarantee: an
   "output" event can never reach a caller after "finished" has,
   because they travel the same FIFO queue and "finished" is appended
   and the queue closed to later events in one atomic step.
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
from collections import deque
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

# How many undelivered events a run's live stream will hold for a
# listener that is not keeping up before it starts dropping the oldest
# "output" events. Same reasoning as OUTPUT_HEAD/TAIL_LINES, one layer
# out: the audit row is the record, the event stream is a convenience
# for whoever is watching, and a watcher that has stopped reading must
# not be able to grow this process's memory without bound. Decoupling
# `on_event` from the worker (see _EventStream) is what makes a slow
# listener harmless; it must not make it expensive instead.
EVENT_QUEUE_MAX = 1000


class ActionBusy(Exception):
    """Raised by start() when a run is already in flight."""


def _log(message: str) -> None:
    # No logging framework exists elsewhere in this codebase (see
    # store.py's corruption handling) -- stderr, with the same prefix,
    # is the existing convention.
    print(f"health-console: {message}", file=sys.stderr)


def _log_error(message: str) -> None:
    """_log, plus the traceback of the exception being handled. Only
    valid from inside an `except` block; use _log elsewhere."""
    _log(message)
    traceback.print_exc(file=sys.stderr)


def _kill_group(process: subprocess.Popen, grace: float,
                run: _Run) -> None:
    """Terminate a process and everything in its process group.

    `start_new_session=True` at Popen time makes `process.pid` the
    leader of its own process group, so this reaches grandchildren that
    inherited stdout (apt-get's helper processes, a shell's backgrounded
    jobs) -- not just the direct child. Signalling only the direct child
    can leave those descendants holding the pipe open forever: the
    direct child exits, but a read loop waiting for EOF never sees it.

    Reports nothing back to its caller. Its one lasting effect besides
    the signals themselves is `run.kill_attempted`, and that is set
    here, *before* the first signal goes out, rather than by a caller
    inspecting a return value afterwards -- see the comment at the
    assignment for why the difference is load-bearing. Whether the kill
    is believed to have worked is not decided here at all: the row is
    written from a fresh probe of the group at audit-write time (see
    `_group_is_dead`), because `process.wait()` only ever confirms the
    direct child, and an in-group sibling that ignored the signal (or
    one that dies a moment after this function gives up waiting) would
    make anything cached here already wrong by the time anyone reads
    it. This function's own bounded waits exist only to give SIGTERM a
    grace period before SIGKILL.

    Never raises: a kill that cannot even be dispatched (EPERM, the
    expected outcome for the one privileged action this console ships)
    is logged, and the row says the process could not be confirmed
    stopped.
    """
    if process.poll() is not None:
        # Already gone. No signal is dispatched, so neither a cancel nor
        # a timeout can claim credit for having ended this run.
        return
    # Recorded before the signal goes out, and never after it returns.
    # The signal landing and the child dying are the same event that
    # wakes the worker's process.wait(), so a flag assigned once the
    # kill path has returned is in a race with the audit write that it
    # can lose: with the gap widened to 60 ms the reason vanished from
    # the row 10 times out of 10, turning a child that trapped SIGTERM
    # and exited 0 into an ordinary clean exit. The margin that usually
    # hid this came from Popen.wait()'s ~50 ms polling granularity --
    # an accident of the standard library, not a happens-before. Set
    # first and unset below on the one path that proves nothing was
    # delivered, and there is no window at all.
    run.kill_attempted = True
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        run.kill_attempted = False
        return
    except OSError as exc:
        # Not evidence that the process is gone, so the attempt stands
        # and the row will say it could not be confirmed stopped.
        _log_error(f"could not resolve the process group id: {exc}")
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        # The group vanished in the microseconds since the liveness
        # check above. Nothing was delivered, so nothing here ended the
        # run and the flag must not claim otherwise.
        run.kill_attempted = False
        return
    except OSError as exc:
        # PermissionError (a subclass of OSError) is EPERM: no member of
        # the group is signalable by this uid. This is the expected
        # outcome for the one privileged action this console ships, not
        # a bug to let propagate into the caller. The attempt was real
        # and the row should say so.
        _log_error(f"could not send SIGTERM to the process group: {exc}")
        return
    try:
        process.wait(grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError as exc:
        _log_error(f"could not send SIGKILL to the process group: {exc}")
        return
    try:
        process.wait(grace)
    except subprocess.TimeoutExpired:
        pass


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

    Its lock is one of the two this module's threads contend on -- the
    other is `_EventStream._cv` -- and the rule in the module
    docstring's property 3 names both: no I/O under either. It is held
    across a list append and a join of at most OUTPUT_HEAD_LINES +
    OUTPUT_TAIL_LINES strings, and must never be held across a read, a
    wait, a log call, or a call into the caller's code.
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


class _EventStream:
    """Delivers one run's events to the caller's `on_event`, from one
    dedicated thread that owns nothing.

    This class exists so that a single rule holds by construction
    rather than by remembering it at each call site:

        Nothing the caller controls, and nothing that waits on the
        child's output, may sit on the path between the audit write and
        the release of the run lock.

    Three separate fixes to this module each restored that rule where
    it had just been broken and re-broke it somewhere new: the reader
    loop ran on the worker thread, so a blocked read wedged it; then
    the worker closed the pipe out from under the reader, and
    `TextIOWrapper.close()` waits for the blocked read it shares a
    buffer lock with; then a lock taken to order events was held across
    `on_event` by the reader and had to be acquired by the worker
    before it could release the run lock. Different mechanisms, one
    shape: a wait the caller or the pipe controls, reachable from the
    thread that owes the world an audit row.

    So `on_event` is called from exactly one place in this module --
    `_pump` below -- on a thread that is not the worker, is not the
    reader, and holds no lock of this module's while it calls.
    Producers (`emit`, `close`) only append to a deque under `_cv`'s
    mutex, which is never held across anything that can block --
    including `_log`, which is why `_drop_one_locked` returns a flag
    for its caller to write out after the block rather than logging
    from inside it. A
    listener that never returns therefore stalls its own event stream
    and nothing else: the child is still reaped, the row is still
    written, the run lock is still released, and the next action can
    still start.

    Ordering -- an "output" event can never reach a caller after
    "finished" -- is a property of the queue rather than of a second
    lock papering over a check-then-act: events are delivered FIFO, and
    `close()` appends the final event and refuses every later one in
    the same atomic step.
    """

    def __init__(self, on_event: Callable[[dict], None],
                 run_id: str) -> None:
        self._on_event = on_event
        self._run_id = run_id
        self._cv = threading.Condition()
        self._pending: deque[dict] = deque()
        self._closed = False
        self._dropped = 0
        self._thread = threading.Thread(
            target=self._pump, name=f"action-{run_id}-events", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        """Wait for every queued event to have been delivered.

        Only for callers that have explicitly asked to block on
        delivery (`ActionRunner.wait`). Nothing inside this module's
        own run path may call this: doing so would put the caller's
        callback back on the worker's leash, which is the whole thing
        this class exists to prevent.
        """
        self._thread.join(timeout)

    def emit(self, phase: str, **fields) -> None:
        """Queue one event. Never blocks on the caller's callback."""
        first_drop = False
        with self._cv:
            if self._closed:
                # The run has already emitted "finished". A line that
                # a lingering reader thread is only now producing must
                # not be delivered after it.
                return
            if len(self._pending) >= EVENT_QUEUE_MAX:
                first_drop = self._drop_one_locked()
            self._pending.append(
                {"run_id": self._run_id, "phase": phase, **fields})
            self._cv.notify()
        # Logged out here, never inside the block above. Writing to
        # stderr is an unbounded wait -- a pipe nobody is reading blocks
        # forever once its 64 KiB buffer fills -- and the thread that
        # triggers drops is the reader. Logging under the mutex left the
        # reader holding the one lock the worker must take in close()
        # before it can release the run lock: measured, that wedged the
        # runner permanently (still busy at 60 s, every later start()
        # refused, shutdown(10) returning still busy). See the module
        # docstring's property 3: this module's own logging is I/O like
        # any other.
        if first_drop:
            _log(
                f"the event listener for run {self._run_id} is not "
                "keeping up; dropping the oldest output events from its "
                "live stream (the audit row still records all of them)")

    def close(self, phase: str | None = None, **fields) -> None:
        """Append a last event, if any, and refuse every later one.

        One atomic step, so no event queued after this can overtake the
        final one, and the pump thread finishes once the queue drains.
        Called from the worker's `finally` block: it must stay a deque
        append under a mutex, never a wait on delivery.
        """
        with self._cv:
            if self._closed:
                return
            if phase is not None:
                self._pending.append(
                    {"run_id": self._run_id, "phase": phase, **fields})
            self._closed = True
            self._cv.notify()

    def _drop_one_locked(self) -> bool:
        # Called with `self._cv` held, and therefore does no I/O of any
        # kind -- it returns whether this was the run's first drop and
        # leaves the caller to log it after releasing the mutex.
        #
        # Drops the oldest "output" event:
        # the live stream is worth less than the memory of an unbounded
        # backlog, and the audit row keeps the output regardless. In
        # practice the leftmost event is always an "output" one -- the
        # pump pops "started" before it calls, and "finished" closes
        # the queue -- so the scan below is an O(1) popleft except in a
        # startup ordering too narrow to rely on.
        for index, event in enumerate(self._pending):
            if event["phase"] == "output":
                del self._pending[index]
                break
        else:
            self._pending.popleft()
        self._dropped += 1
        return self._dropped == 1

    def _pump(self) -> None:
        forward_output = True
        while True:
            with self._cv:
                while not self._pending and not self._closed:
                    self._cv.wait()
                if not self._pending:
                    break
                event = self._pending.popleft()
            if event["phase"] == "output" and not forward_output:
                # This listener has already raised once on an output
                # event. Keep draining the queue so the thread still
                # finishes, but stop calling a callback that has shown
                # it is broken -- the output itself is still recorded
                # in the audit row either way.
                continue
            try:
                # The one call into code this module does not control,
                # made here and nowhere else, holding nothing.
                self._on_event(event)
            except Exception:  # noqa: BLE001 -- caller's code
                _log_error(
                    f"on_event raised while delivering "
                    f"'{event['phase']}' for run {self._run_id}")
                if event["phase"] == "output":
                    forward_output = False
        if self._dropped:
            _log(
                f"{self._dropped} output events were dropped from the "
                f"live stream for run {self._run_id}")


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
    # note-computation logic the difference. Written only by
    # `_kill_group`, and there only before the signal it describes goes
    # out -- a caller setting it from a return value is racing the
    # audit write, and loses (see the comment at the assignment).
    kill_attempted: bool = False


def _drain(stream, buffer: _OutputBuffer, events: _EventStream,
           run_id: str) -> None:
    """Read a process's combined stdout/stderr, line by line.

    Runs in its own thread so the worker thread waiting on the process
    itself (see ActionRunner._run) is never blocked on drainage of a
    pipe some descendant process still holds open -- including when
    that thread later closes the stream: this loop may still be blocked
    in a read on it, and `close()` waits for that read to return (see
    _run's finally block).

    Every line goes to two places, neither of which can block this
    loop: `buffer`, which is what the audit row is written from, and
    `events`, which only appends to a queue another thread delivers
    from. In particular this loop never calls the caller's `on_event`
    itself. An earlier version did, which meant a listener that raised
    could unwind the loop and leave the rest of the output undrained,
    and -- once a lock was added to order those calls against the
    "finished" event -- meant a listener that merely *blocked* could
    keep the worker from ever releasing the run lock.
    """
    try:
        for line in stream:
            line = line.rstrip("\n")
            buffer.append(line)
            events.emit("output", line=line)
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
    lock is released, and neither a broken `on_event` callback, nor one
    that blocks and never returns, nor a failing store write can
    prevent that release. See the module docstring for why that
    guarantee is the point of this class.
    """

    def __init__(self, store: Store, timeout_seconds: int = 1800,
                sudo: tuple[str, ...] = ("/usr/bin/sudo", "-n")) -> None:
        self._store = store
        self._timeout_seconds = timeout_seconds
        self._sudo = sudo
        self._lock = threading.Lock()
        self._current: _Run | None = None
        self._thread: threading.Thread | None = None
        self._events: _EventStream | None = None

    def is_busy(self) -> bool:
        # Reflects whether the lock is held, not whether a process
        # object exists: the lock is only released once the audit row
        # has been written (or the write has failed and been logged), so
        # "not busy" always means the previous run is fully accounted
        # for in the audit log -- which is the thing a second run's row
        # must not be able to land ahead of.
        #
        # It does *not* mean the previous run's "finished" event has
        # reached the caller. It deliberately cannot: delivery calls the
        # caller's own callback, and putting that on the path to this
        # release is exactly the defect the module docstring's property
        # 3 describes. So a caller polling this can see "free" while an
        # SSE listener has not yet been told the run ended, and a next
        # run's "started" can be delivered (on that run's own stream)
        # first. Every event carries its run_id for that reason; a
        # caller that needs delivery to have happened should use
        # wait(), which joins the event stream too.
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
        events: _EventStream | None = None
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
            # Created here, in the caller's thread, rather than inside
            # the worker: wait() must be able to join it the moment
            # start() has returned, or a caller that starts a run and
            # immediately waits for it could be told the run is over
            # before its "finished" event has been delivered.
            events = _EventStream(on_event, run.run_id)
            thread = threading.Thread(
                target=self._run, name=f"action-{run.run_id}",
                args=(run, action, source, argv, events), daemon=True)
            events.start()
            thread.start()
        except Exception:
            if events is not None:
                # Nothing will ever emit on this stream; let its thread
                # finish rather than leaving it parked on the condition.
                events.close()
            self._current = None
            self._lock.release()
            raise
        self._thread = thread
        self._events = events
        return run.run_id

    def wait(self, timeout: float | None = None) -> None:
        """Block until the last started run has finished and all of its
        events have been delivered.

        The second half is why `timeout` matters: event delivery calls
        the caller's own `on_event`, so a listener that never returns
        can hold this method for as long as it likes. That is safe --
        the run itself is over, its row is written and the lock
        released long before this returns -- but a caller that passes
        no timeout is choosing to wait on its own callback.
        """
        deadline = None if timeout is None else time.monotonic() + timeout

        def remaining() -> float | None:
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        thread = self._thread
        if thread is not None:
            thread.join(remaining())
        events = self._events
        if events is not None:
            events.join(remaining())

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

        Never raises: `_kill_group` logs a kill it could not even
        dispatch (a permission error) rather than letting it propagate,
        so a caller wiring this into an HTTP handler cannot turn "the
        operator clicked cancel" into a 500, and shutdown() can always
        reach its own wait().

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
            _kill_group(process, KILL_GRACE_SECONDS, run)
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

        `timeout` also covers delivering the run's remaining events to
        `on_event` (see wait()), so a listener that has stopped
        returning costs this method its timeout and nothing more: the
        row is already written and the lock already released by then.
        """
        try:
            self.cancel()
        except Exception:  # noqa: BLE001
            _log_error(
                "cancel() raised during shutdown; still waiting for the "
                "run in flight to end")
        self.wait(timeout)

    def _run(self, run: _Run, action: Action, source: str,
             argv: list[str], events: _EventStream) -> None:
        start_wall = time.time()
        start_monotonic = time.monotonic()
        buffer = _OutputBuffer()
        exit_code: int | None = None
        note: str | None = None
        watchdog: threading.Timer | None = None
        process: subprocess.Popen | None = None
        reader_thread: threading.Thread | None = None
        try:
            events.emit("started", action_id=action.id)
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
                _kill_group(process, KILL_GRACE_SECONDS, run)

            watchdog = threading.Timer(
                self._timeout_seconds, self._on_timeout, args=(run,))
            watchdog.daemon = True
            watchdog.start()

            assert process.stdout is not None
            reader_thread = threading.Thread(
                target=_drain, name=f"action-{run.run_id}-reader",
                args=(process.stdout, buffer, events, run.run_id),
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
            write_failure: str | None = None
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
                    #
                    # Only *formatted* here. Writing it to stderr is I/O
                    # -- an unbounded wait on whoever is reading that fd
                    # -- and nothing unbounded may sit between the audit
                    # write and the lock release. Logging here wedged
                    # the runner exactly as the drop-path log did:
                    # measured with stderr stalled, still busy at 60 s,
                    # every later start() refused, shutdown(10)
                    # returning still busy.
                    write_failure = traceback.format_exc()
                # Appends the "finished" event and refuses every later
                # one, atomically, so a line a lingering reader is only
                # now producing cannot be delivered after it. This is a
                # deque append under a mutex held across nothing that
                # can block -- the caller's own on_event runs on the
                # stream's thread, off this thread's path to the lock
                # release below. An earlier version called on_event
                # from right here, under a lock the reader also held
                # across on_event, which meant a listener that never
                # returned meant a run lock that was never released.
                events.close("finished", exit_code=exit_code)
            finally:
                # Whatever happened above -- including a bug in this
                # very block -- the lock must still be released. This is
                # the property Critical 1 exists to restore: a run that
                # cannot be audited must still not wedge the next one.
                self._current = None
                self._lock.release()
            if write_failure is not None:
                # After the release, deliberately. A stalled stderr can
                # now cost this worker thread (and a wait() that is
                # joining it, bounded by its own timeout) but no longer
                # the run lock: is_busy() is already false and the next
                # action can already start.
                _log(f"failed to write the action_run row for "
                     f"run {run.run_id}\n{write_failure.rstrip()}")

    def _on_timeout(self, run: _Run) -> None:
        process = run.process
        if process is None or process.poll() is not None:
            return
        # Both flags are set before anything that can end the process:
        # the moment a signal lands, the worker is free to write the
        # row, and a reason recorded after that point is a reason the
        # operator never sees. `kill_attempted` is set inside
        # `_kill_group` for the same reason.
        run.timed_out = True
        _kill_group(process, KILL_GRACE_SECONDS, run)
