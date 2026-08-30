import ast
import os
import random
import signal
import subprocess
import sys
import textwrap
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import healthconsole.runner as runner_module
from healthconsole.actions import Action, Risk
from healthconsole.runner import ActionBusy, ActionRunner
from healthconsole.store import Store

TRUE = Action("t.true", ("/bin/true",), root=False, risk=Risk.SAFE)
FALSE = Action("t.false", ("/bin/false",), root=False, risk=Risk.SAFE)
ECHO = Action("t.echo", ("/bin/echo", "hello"), root=False, risk=Risk.SAFE)
SLEEP = Action("t.sleep", ("/bin/sleep", "30"), root=False, risk=Risk.SAFE)


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = Store(Path(self.dir.name) / "db.sqlite3")
        self.addCleanup(self.store.close)
        self.events = []
        self.runner = ActionRunner(self.store)

    def run_to_completion(self, action, timeout=10):
        self.runner.start(action, "127.0.0.1", self.events.append)
        self.runner.wait(timeout)


class TestSuccess(RunnerCase):
    def test_a_successful_run_is_audited_with_exit_code_zero(self):
        self.run_to_completion(TRUE)
        row = self.store.read_action_runs()[0]
        self.assertEqual(row["exit_code"], 0)
        self.assertEqual(row["action_id"], "t.true")
        self.assertEqual(row["source"], "127.0.0.1")

    def test_output_is_captured_and_streamed(self):
        self.run_to_completion(ECHO)
        self.assertIn("hello", self.store.read_action_runs()[0]["output"])
        lines = [e["line"] for e in self.events if e["phase"] == "output"]
        self.assertIn("hello", "".join(lines))

    def test_the_event_sequence_brackets_the_run(self):
        self.run_to_completion(ECHO)
        phases = [event["phase"] for event in self.events]
        self.assertEqual(phases[0], "started")
        self.assertEqual(phases[-1], "finished")
        self.assertEqual(self.events[-1]["exit_code"], 0)

    def test_every_event_carries_the_run_id(self):
        self.run_to_completion(ECHO)
        ids = {event["run_id"] for event in self.events}
        self.assertEqual(len(ids), 1)


class TestFailure(RunnerCase):
    def test_a_failing_run_is_audited_with_its_exit_code(self):
        self.run_to_completion(FALSE)
        self.assertEqual(self.store.read_action_runs()[0]["exit_code"], 1)

    def test_a_missing_binary_is_audited_rather_than_raising(self):
        # A catalogue entry pointing at a binary this machine does not have
        # must be recorded, not lost: "nothing happened and nothing was
        # written" is the worst outcome for an audit log.
        missing = Action("t.missing", ("/nonexistent/binary",),
                         root=False, risk=Risk.SAFE)
        self.run_to_completion(missing)
        row = self.store.read_action_runs()[0]
        self.assertIsNone(row["exit_code"])
        self.assertTrue(row["output"])


class TestTimeout(RunnerCase):
    def test_a_run_past_its_timeout_is_killed_and_audited(self):
        runner = ActionRunner(self.store, timeout_seconds=1)
        runner.start(SLEEP, "127.0.0.1", self.events.append)
        runner.wait(20)
        row = self.store.read_action_runs()[0]
        self.assertIsNone(row["exit_code"])
        self.assertFalse(runner.is_busy())


class TestLock(RunnerCase):
    def test_a_second_run_is_refused_while_one_is_in_flight(self):
        self.runner.start(SLEEP, "127.0.0.1", self.events.append)
        try:
            with self.assertRaises(ActionBusy):
                self.runner.start(TRUE, "127.0.0.1", self.events.append)
        finally:
            self.runner.cancel()
            self.runner.wait(20)

    def test_the_lock_is_released_after_a_failure(self):
        self.run_to_completion(FALSE)
        self.assertFalse(self.runner.is_busy())
        self.run_to_completion(TRUE)
        self.assertEqual(len(self.store.read_action_runs()), 2)


class TestOnEventExceptionsNeverWedgeTheRunner(RunnerCase):
    """Critical 1: on_event is a caller's callback -- an SSE handler
    writing to a socket whose other end may have gone away. It must
    never be able to prevent the lock from being released, or the
    runner is stuck refusing every future run for the life of the
    process."""

    def test_an_on_event_exception_on_finished_does_not_wedge_the_runner(
            self):
        def flaky(event):
            if event["phase"] == "finished":
                raise BrokenPipeError("client gone")
            self.events.append(event)

        self.runner.start(TRUE, "127.0.0.1", flaky)
        self.runner.wait(10)
        self.assertFalse(
            self.runner.is_busy(),
            "on_event raising on the 'finished' event left the lock "
            "held forever")

        # The runner must still be usable for a later run.
        self.run_to_completion(FALSE)
        self.assertEqual(len(self.store.read_action_runs()), 2)


class TestTimeoutReclaimsTheWholeProcessGroup(RunnerCase):
    """Critical 2: a killed direct child is not a killed run. A shell
    that backgrounds a job and inherits-shares its process group can die
    to a plain SIGTERM while its child keeps running and keeps the
    stdout pipe open, so the read loop never sees EOF."""

    def test_a_backgrounded_grandchild_does_not_survive_the_timeout(self):
        leaky = Action(
            "t.leaky", ("/bin/sh", "-c", "sleep 40 & wait"),
            root=False, risk=Risk.SAFE)
        runner = ActionRunner(self.store, timeout_seconds=1)
        runner.start(leaky, "127.0.0.1", self.events.append)
        runner.wait(15)
        self.assertFalse(
            runner.is_busy(),
            "the run was still alive 15s after a 1s timeout -- a "
            "backgrounded grandchild is still holding the pipe open")
        row = self.store.read_action_runs()[0]
        self.assertIsNone(row["exit_code"])


class TestCancelNeverMisfiresOntoALaterRun(RunnerCase):
    """Important 3: cancel() must act on the run it was called for (or
    "whichever run is current" for the no-argument form used by tests
    and shutdown), never on a run that started after it."""

    def test_a_cancel_left_over_from_a_finished_run_does_not_reach_a_new_one(
            self):
        missing = Action(
            "t.missing_race", ("/nonexistent/binary-race",),
            root=False, risk=Risk.SAFE)
        self.runner.start(missing, "127.0.0.1", self.events.append)

        # Fire the cancel concurrently, aimed at whatever is "current"
        # right now -- the about-to-fail run. A correct cancel() acts
        # (or determines there is nothing to act on) immediately; a
        # buggy, timed one can still be waiting when the next run starts.
        canceller = threading.Thread(target=self.runner.cancel)
        canceller.start()

        # The missing binary fails in well under a second; this leaves
        # ample margin before any timed wait inside a buggy cancel()
        # could still be pending.
        self.runner.wait(5)
        self.assertFalse(self.runner.is_busy())

        self.runner.start(SLEEP, "127.0.0.1", self.events.append)
        try:
            # Give a buggy, timed cancel() every chance to wake up and
            # misfire onto this new run before checking it.
            canceller.join(3)
            time.sleep(0.5)
            self.assertTrue(
                self.runner.is_busy(),
                "a cancel() call meant for the earlier, already-"
                "finished run killed this later one instead")
        finally:
            self.runner.cancel()
            self.runner.wait(20)

    def test_cancel_by_run_id_ignores_a_run_that_is_no_longer_current(self):
        run_id_a = self.runner.start(TRUE, "127.0.0.1", self.events.append)
        self.runner.wait(5)
        self.assertFalse(self.runner.is_busy())

        self.runner.start(SLEEP, "127.0.0.1", self.events.append)
        try:
            # A's run_id is stale: A already finished. Cancelling it by
            # id must not touch the run that is current now.
            self.runner.cancel(run_id_a)
            time.sleep(0.5)
            self.assertTrue(self.runner.is_busy())
        finally:
            self.runner.cancel()
            self.runner.wait(20)


class TestOnEventExceptionMidOutputDoesNotOrphanTheChild(RunnerCase):
    """Important 4: an on_event exception while output is streaming must
    not end the run "on paper" (audit row written, lock released) while
    its child process keeps running, unreachable and unaudited."""

    def test_the_child_does_not_outlive_a_broken_output_listener(self):
        pid_file = Path(self.dir.name) / "pid"
        talkative = Action(
            "t.talkative", ("/bin/sh", "-c",
                            f"echo $$ > {pid_file}; echo first; sleep 33"),
            root=False, risk=Risk.SAFE)

        def flaky(event):
            if event["phase"] == "output":
                raise RuntimeError("listener gone")
            self.events.append(event)

        runner = ActionRunner(self.store, timeout_seconds=5)
        runner.start(talkative, "127.0.0.1", flaky)
        runner.wait(15)

        pid = int(pid_file.read_text().strip())

        def force_kill():
            # `pid` is the /bin/sh itself, not the `sleep` it spawned as
            # a plain (non-backgrounded) child of the same process
            # group. Killing only that one pid would leave `sleep`
            # behind if the group kill this test is exercising ever
            # regressed -- killpg is the cleanup that actually matches
            # what this test is trying to confirm.
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        self.addCleanup(force_kill)

        self.assertFalse(runner.is_busy())
        with self.assertRaises(
                ProcessLookupError,
                msg="the child outlived the run that was supposed to "
                    "have ended it"):
            os.kill(pid, 0)


class TestStdoutCloseDoesNotBlockTheWorker(RunnerCase):
    """Critical A / Item 1: closing process.stdout while the reader
    thread is still blocked inside a read on it blocks the closer too --
    the exact wedge Critical 2's fix was supposed to eliminate,
    reintroduced by the fix itself. A descendant that escapes the
    process group (here, via `setsid`) and still holds the pipe open
    must not be able to block the worker thread's own finalisation, even
    though the direct child it was actually running exits in
    milliseconds.

    Critically, the bound on how long that can take must not scale with
    `timeout_seconds`: a run whose direct child is long gone must not
    hold the lock for a large fraction of a 30-minute timeout merely
    because an unrelated descendant is still holding a pipe open. This
    is exercised with the *shipped default* timeout (1800s) precisely
    so a regression that ties the read bound to the remaining hard
    deadline -- rather than restarting it from the moment the child is
    reaped -- cannot hide behind a short `timeout_seconds` in the test.
    """

    def test_a_fast_exit_with_a_pipe_holding_escapee_is_not_bound_by_the_timeout(
            self):
        pid_file = Path(self.dir.name) / "escapee_pid"
        escapee = Action(
            "t.escapee",
            ("/bin/sh", "-c",
             f"setsid sleep 600 & echo $! > {pid_file}; exit 0"),
            root=False, risk=Risk.SAFE)
        # The shipped default. A regression that bounds the reader join
        # by the remaining hard deadline (timeout_seconds + grace +
        # read_bound) rather than by READ_BOUND_SECONDS from the moment
        # the child is reaped would make this test take ~1807s instead
        # of ~5s -- a difference no reasonable wait() bound below can
        # mistake for a pass.
        runner = ActionRunner(self.store, timeout_seconds=1800)

        start = time.monotonic()
        runner.start(escapee, "127.0.0.1", self.events.append)

        # The direct child (the shell) exits in milliseconds; only the
        # detached `sleep 600` -- outside this runner's process group
        # entirely -- keeps the pipe open. This must finish in a few
        # seconds, not "eventually": bound the wait well under even a
        # generous multiple of READ_BOUND_SECONDS, and nowhere near
        # timeout_seconds, so a regression here fails fast.
        runner.wait(10)
        elapsed = time.monotonic() - start

        def cleanup():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGKILL)
            except (FileNotFoundError, ValueError, ProcessLookupError):
                pass

        self.addCleanup(cleanup)

        self.assertFalse(
            runner.is_busy(),
            "the worker is still busy 10s after the direct child exited "
            "in milliseconds, with timeout_seconds=1800 -- the read "
            "bound is most likely scaling with the run's own timeout "
            "instead of restarting once the child was reaped")
        self.assertLess(
            elapsed, 10,
            f"took {elapsed:.2f}s to finish a run whose direct child "
            "exited in milliseconds")
        row = self.store.read_action_runs()[0]
        self.assertEqual(row["exit_code"], 0)
        self.assertLess(
            row["duration_ms"], 10_000,
            f"duration_ms={row['duration_ms']} for a child that exited "
            "in milliseconds -- the recorded duration is measuring how "
            "long the escapee's pipe stayed open, not how long the "
            "action actually ran")


class TestExitCodeSurvivesALateCancel(RunnerCase):
    """Important A: a cancel (or a timeout) that arrives after the
    process has already exited on its own must not overwrite its real
    exit code with None, nor claim in the output that a cancel or a
    timeout was what ended the run."""

    def test_a_cancel_after_the_process_already_exited_leaves_its_exit_code_alone(
            self):
        pid_file = Path(self.dir.name) / "escapee_pid2"
        action = Action(
            "t.late_exit",
            ("/bin/sh", "-c",
             f"setsid sleep 3 & echo $! > {pid_file}; exit 7"),
            root=False, risk=Risk.SAFE)
        runner = ActionRunner(self.store, timeout_seconds=30)
        runner.start(action, "127.0.0.1", self.events.append)

        # The shell exits (with code 7) in milliseconds; give it a
        # moment to actually do so before "cancelling" a run that, by
        # then, has nothing left to cancel. The detached `sleep 3` keeps
        # the pipe open for a few more seconds regardless, so the run
        # itself is not yet finalised when the cancel lands.
        time.sleep(0.3)
        runner.cancel()
        runner.wait(15)

        def cleanup():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGKILL)
            except (FileNotFoundError, ValueError, ProcessLookupError):
                pass

        self.addCleanup(cleanup)

        row = self.store.read_action_runs()[0]
        self.assertEqual(
            row["exit_code"], 7,
            "a cancel() arriving after the process had already exited "
            "must not erase its real exit code")
        self.assertNotIn(
            "cancel", row["output"],
            "the output must not claim a cancel ended a run that had "
            "already exited on its own")


class TestATrappedKillKeepsBothTheExitCodeAndTheReason(RunnerCase):
    """Item 2: a genuine exit code must not silently erase *why* the
    process ended. A child that traps SIGTERM and deliberately exits 0
    in response to a timeout kill is not the same event as one that
    simply ran to completion on its own -- discarding the reason (by
    setting note = None whenever a real return code is observed, as an
    earlier version of this method did) makes the two indistinguishable
    in the one place an operator can tell them apart."""

    def test_a_trapped_termination_records_both_the_exit_code_and_the_timeout(
            self):
        trapping = Action(
            "t.trapping",
            ("/bin/sh", "-c", 'trap "exit 0" TERM; echo hi; sleep 30'),
            root=False, risk=Risk.SAFE)
        runner = ActionRunner(self.store, timeout_seconds=2)
        runner.start(trapping, "127.0.0.1", self.events.append)
        runner.wait(15)

        row = self.store.read_action_runs()[0]
        self.assertEqual(
            row["exit_code"], 0,
            "the trapped exit's own genuine status must survive")
        self.assertIn(
            "timeout", row["output"],
            "a real exit code must not erase the fact that a timeout "
            "kill was what actually triggered it")


class TestABlockedListenerCannotHoldTheRunLock(RunnerCase):
    """The defect this module has now produced four times over, in four
    different places: something the caller controls, or something that
    waits on the child's output, ends up on the path between the audit
    write and the release of the run lock.

    Round 1 put a blocking read there. Round 2 put a close() that waits
    on that read there. Round 3 put a lock there that `_drain` holds
    across the caller's own `on_event`. Each was a correct fix for the
    finding in front of it and a new instance of the same shape.

    A listener that never returns is the general case of all of them:
    an SSE handler writing to a client that has stopped reading its
    socket. It may stall its own event stream for as long as it likes.
    It may not stop the child being reaped, the row being written, or
    the next action ever running.
    """

    # timeout_seconds=5 makes this module's own worst case
    # 5 + KILL_GRACE_SECONDS (2) + READ_BOUND_SECONDS (5) = 12s. The
    # assertion is against that bound, not against "eventually": the
    # whole point is that the deadline belongs to this module and not
    # to the callback.
    TIMEOUT_SECONDS = 5
    WORST_CASE_SECONDS = 12.0

    def busy_until(self, runner, deadline):
        """Wall-clock seconds until is_busy() goes false, or None."""
        start = time.monotonic()
        while time.monotonic() < deadline:
            if not runner.is_busy():
                return time.monotonic() - start
            time.sleep(0.02)
        return None

    def blocking_listener(self, phase):
        """A listener that parks forever on `phase`, and the release
        switch that lets it go again during cleanup."""
        release = threading.Event()
        reached = threading.Event()

        def listener(event):
            self.events.append(event)
            if event["phase"] == phase:
                reached.set()
                release.wait(120)

        return listener, reached, release

    def test_a_listener_blocked_on_an_output_event_releases_the_lock(self):
        listener, reached, release = self.blocking_listener("output")
        runner = ActionRunner(self.store, timeout_seconds=self.TIMEOUT_SECONDS)

        def unblock():
            release.set()
            runner.wait(15)

        self.addCleanup(unblock)

        start = time.monotonic()
        runner.start(ECHO, "127.0.0.1", listener)
        self.assertTrue(
            reached.wait(10), "the listener never received an output event")

        freed = self.busy_until(
            runner, start + self.WORST_CASE_SECONDS)
        self.assertIsNotNone(
            freed,
            f"still busy {self.WORST_CASE_SECONDS}s after starting a run "
            "whose only problem is a listener that has not returned from "
            "an 'output' event -- the caller's callback is on the path "
            "between the audit write and the lock release")
        self.assertEqual(
            len(self.store.read_action_runs()), 1,
            "the audit row must be written even though the listener is "
            "still parked inside an output event")

        # And the runner is genuinely reusable, not merely reporting
        # itself free: a second run must start and finish while the
        # first run's listener is still blocked.
        second = []
        runner.start(TRUE, "127.0.0.1", second.append)
        runner.wait(10)
        self.assertEqual(len(self.store.read_action_runs()), 2)

    def test_a_listener_blocked_on_the_finished_event_releases_the_lock(self):
        # The same shape one step later: the "finished" emission itself
        # sits between the audit write and the lock release, so a
        # listener that parks there wedges the runner just as surely as
        # one that parks on output.
        listener, reached, release = self.blocking_listener("finished")
        runner = ActionRunner(self.store, timeout_seconds=self.TIMEOUT_SECONDS)

        def unblock():
            release.set()
            runner.wait(15)

        self.addCleanup(unblock)

        start = time.monotonic()
        runner.start(TRUE, "127.0.0.1", listener)
        self.assertTrue(
            reached.wait(10), "the listener never received a finished event")

        freed = self.busy_until(runner, start + self.WORST_CASE_SECONDS)
        self.assertIsNotNone(
            freed,
            f"still busy {self.WORST_CASE_SECONDS}s after starting a run "
            "whose only problem is a listener that has not returned from "
            "the 'finished' event")
        self.assertEqual(len(self.store.read_action_runs()), 1)

    def test_every_event_reaches_the_caller_on_one_dedicated_thread(self):
        # The structural half of the same invariant: `on_event` is
        # called from exactly one place in the module, on a thread that
        # is neither the worker (which owns the run lock and the audit
        # write) nor the caller's own. If a future change calls it from
        # the worker again, that thread is back on the caller's leash
        # and this fails.
        idents = []

        def listener(event):
            idents.append(threading.get_ident())
            self.events.append(event)

        self.runner.start(ECHO, "127.0.0.1", listener)
        self.runner.wait(10)

        phases = [event["phase"] for event in self.events]
        self.assertEqual(phases[0], "started")
        self.assertEqual(phases[-1], "finished")
        self.assertEqual(
            len(set(idents)), 1,
            f"events were delivered from {len(set(idents))} different "
            "threads; a caller's on_event must be serialised onto one")
        self.assertNotIn(
            threading.get_ident(), idents,
            "events must not be delivered on the caller's own thread")


class TestTheKillReasonIsRecordedBeforeTheSignal(RunnerCase):
    """The reason a run ended must be recorded before the signal that
    ends it goes out, not after the kill call returns.

    `_kill_group` returns the instant the child dies -- and that death
    is the same event that wakes the worker's `process.wait()`. Setting
    `kill_attempted` from the return value therefore races the audit
    write: the worker can reach the note computation first and record a
    trapped `exit 0` as an ordinary clean exit, losing the only trace
    that a timeout kill was what triggered it. The margin that usually
    hides this is `Popen.wait()`'s polling granularity, which is an
    accident of the standard library rather than any ordering
    guarantee, so this test removes it by widening the gap on purpose.
    """

    def test_a_slow_kill_path_does_not_lose_the_timeout_reason(self):
        trapping = Action(
            "t.trap_slow",
            ("/bin/sh", "-c", 'trap "exit 0" TERM; echo hi; sleep 30'),
            root=False, risk=Risk.SAFE)

        real_kill_group = runner_module._kill_group

        def slow_kill_group(*args, **kwargs):
            # Everything the real kill path does, then a delay standing
            # in for any bookkeeping done after it returns. A reason
            # recorded before the signal is unaffected by this; one
            # recorded afterwards loses the race to the audit write.
            result = real_kill_group(*args, **kwargs)
            time.sleep(0.25)
            return result

        patcher = mock.patch.object(
            runner_module, "_kill_group", slow_kill_group)
        patcher.start()
        self.addCleanup(patcher.stop)

        runner = ActionRunner(self.store, timeout_seconds=2)
        runner.start(trapping, "127.0.0.1", self.events.append)
        runner.wait(15)

        row = self.store.read_action_runs()[0]
        self.assertEqual(
            row["exit_code"], 0,
            "the trapped exit's own genuine status must survive")
        self.assertIn(
            "timeout", row["output"],
            "the timeout that triggered the kill was recorded only "
            "after the kill call returned, so the audit row lost it -- "
            "a trapped SIGTERM exit 0 now reads as an ordinary clean "
            "exit")



class TestNoOutputEventArrivesAfterFinished(RunnerCase):
    """The ordering guarantee the removed `event_order_lock` was added
    for. It is a real requirement, not an artefact of that lock: a
    listener told a run has finished must not then be handed more of
    its output.

    That lock enforced it by making the reader's "check the flag, then
    call on_event" and the worker's "set the flag, then emit finished"
    mutually exclusive -- which is also what put the caller's callback
    on the worker's path to the run lock. The queue enforces it
    instead: one thread delivers events FIFO, and closing the stream
    appends "finished" and refuses every later event in the same atomic
    step, so there is no check-then-act left to widen.

    Reproduced the way the race actually happens. The direct child exits
    at once, so the worker finalises READ_BOUND_SECONDS later while a
    `setsid` escapee outside the process group is still streaming into
    the pipe it inherited. The listener is deliberately slower than the
    escapee, so there is still a backlog of undelivered events when
    "finished" is queued -- without that, a late line simply arrives
    after the delivery thread has already run dry, and the test passes
    whether or not anything actually refuses it. (Confirmed by
    mutation: with the post-close check removed from `_EventStream.emit`
    this test fails; without the backlog it did not.)
    """

    def test_a_still_streaming_escapee_cannot_deliver_after_finished(self):
        pid_file = Path(self.dir.name) / "streamer_pid"
        streaming = Action(
            "t.streamer",
            ("/bin/sh", "-c",
             "setsid sh -c 'i=0; while [ $i -lt 200 ]; do echo line$i; "
             f"i=$((i+1)); sleep 0.03; done' & echo $! > {pid_file}; "
             "exit 0"),
            root=False, risk=Risk.SAFE)

        def cleanup():
            try:
                os.killpg(int(pid_file.read_text().strip()), signal.SIGKILL)
            except (FileNotFoundError, ValueError, ProcessLookupError,
                    PermissionError):
                pass

        self.addCleanup(cleanup)

        phases = []
        lock = threading.Lock()

        def listener(event):
            with lock:
                phases.append(event["phase"])
            if event["phase"] == "output":
                # Slower than the escapee produces, so undelivered
                # events are still queued when the run finalises.
                time.sleep(0.05)

        runner = ActionRunner(self.store, timeout_seconds=1800)
        runner.start(streaming, "127.0.0.1", listener)
        runner.wait(30)
        self.assertFalse(runner.is_busy())

        with lock:
            seen = list(phases)
        self.assertIn(
            "finished", seen,
            "the finished event was never delivered within the bound")
        self.assertEqual(
            seen[-1], "finished",
            f"{len(seen) - 1 - seen.index('finished')} event(s) reached "
            "the listener after it had been told the run finished: "
            f"{seen[seen.index('finished') + 1:]}")


class TestTheEventStreamRefusesEventsAfterItCloses(unittest.TestCase):
    """The same guarantee as the test above, taken down to the unit
    that owns it and made deterministic rather than timed.

    `close()` appends the final event and refuses every later one under
    one mutex, so "an output event races finished" has exactly two
    outcomes and no third: the emit got the mutex first, and the event
    is queued ahead of "finished" and delivered before it; or it got
    the mutex second, and it is refused. This exercises the second --
    the only branch that can go wrong -- with the delivery thread
    deliberately parked inside the caller's callback, so there is a
    backlog for a wrongly-accepted event to be delivered behind.
    """

    def test_an_event_emitted_after_close_is_never_delivered(self):
        delivered = []
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def listener(event):
            delivered.append(event["phase"])
            if event["phase"] == "started":
                entered.set()
                release.wait(30)

        stream = runner_module._EventStream(listener, "unit-run")
        stream.start()

        stream.emit("started", action_id="t.unit")
        self.assertTrue(
            entered.wait(5), "the delivery thread never called the listener")

        # Queued while the listener is parked: this one is legitimate
        # and must arrive, before "finished".
        stream.emit("output", line="in time")
        stream.close("finished", exit_code=0)
        # Emitted after the stream closed -- a line a lingering reader
        # thread only now produced. It must never reach the listener,
        # and there is a backlog in front of it, so a wrongly-accepted
        # event would genuinely be delivered rather than merely queued
        # behind a delivery thread that has already run dry.
        stream.emit("output", line="too late")

        release.set()
        stream.join(10)
        self.assertFalse(
            stream._thread.is_alive(), "the delivery thread did not finish")
        self.assertEqual(
            delivered, ["started", "output", "finished"],
            "an event emitted after the stream closed reached the "
            "listener after it had been told the run finished")


# The repository root, so a child process can import healthconsole.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

# Prelude for the stalled-stderr children below: stderr is dup2'd onto
# the write end of a pipe nobody reads, pre-filled to its capacity, so
# the very next write to it blocks forever. Results go to a saved
# duplicate of the real stdout.
_STALL_STDERR = """
import os, sys, threading, time
sys.path.insert(0, {root!r})
out = os.fdopen(os.dup(1), "w", buffering=1)
_r, _w = os.pipe()
os.set_blocking(_w, False)
try:
    while True:
        os.write(_w, b"x" * 4096)
except BlockingIOError:
    pass
os.set_blocking(_w, True)
os.dup2(_w, 2)
from healthconsole.actions import Action, Risk
from healthconsole.runner import ActionBusy, ActionRunner

def report(runner, budget):
    t0 = time.monotonic()
    freed = None
    while time.monotonic() - t0 < budget:
        if not runner.is_busy():
            freed = time.monotonic() - t0
            break
        time.sleep(0.02)
    print("FREED " + (str(round(freed, 3)) if freed is not None else "NEVER"),
          file=out)
    os._exit(0)
"""


class StalledStderrCase(unittest.TestCase):
    """This module's own logging is I/O, and I/O is an unbounded wait.

    A write to stderr blocks forever once the far end stops draining --
    a 64 KiB pipe nobody reads, a full disk, a terminal under flow
    control. That makes `_log` and `_log_error` exactly as dangerous on
    the path to the run lock as the caller's `on_event` is, and it is
    the category three rounds of review and one of my own passes all
    failed to check: I concluded "structural for the caller, convention
    for the pipe" and never looked at the logging at all.

    Both children below run out-of-process, because dup2'ing stderr
    onto a full pipe inside the test runner would swallow its output
    and hang the suite if the wedge came back.
    """

    def run_child(self, body, budget=45):
        script = _STALL_STDERR.format(root=_REPO_ROOT) + textwrap.dedent(body)
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=budget + 30, cwd=_REPO_ROOT)
        line = [l for l in done.stdout.splitlines() if l.startswith("FREED")]
        self.assertTrue(
            line, f"the child produced no result; stdout={done.stdout!r}")
        return line[0].split()[1]


class TestLoggingUnderTheQueueMutexCannotWedgeTheRunner(StalledStderrCase):
    """Instance four. `_drop_one_locked` ran with `_cv` held and logged
    the first drop from inside that block. The thread that triggers
    drops is the reader, so a stalled stderr left the reader holding
    the one mutex the worker must take in `events.close()` before it
    can release the run lock -- the exact shape the _EventStream rework
    existed to eliminate, reintroduced by that rework's own logging.
    """

    def test_a_stalled_stderr_on_the_drop_path_still_frees_the_lock(self):
        # timeout_seconds=5 puts this module's own worst case at
        # 5 + KILL_GRACE_SECONDS (2) + READ_BOUND_SECONDS (5) = 12s.
        freed = self.run_child("""
            chatty = Action("t.chatty", ("/bin/sh", "-c", "seq 1 20000"),
                            root=False, risk=Risk.SAFE)
            reached, release = threading.Event(), threading.Event()
            def listener(event):
                if event["phase"] == "output" and not reached.is_set():
                    reached.set(); release.wait(300)

            class Store:
                def write_action_run(self, *a, **k): pass

            runner = ActionRunner(Store(), timeout_seconds=5)
            runner.start(chatty, "127.0.0.1", listener)
            reached.wait(10)
            report(runner, 40)
        """)
        self.assertNotEqual(
            freed, "NEVER",
            "the run lock was never released: the reader is blocked "
            "writing to a stalled stderr while holding the event "
            "queue's mutex, and the worker cannot take it to emit "
            "'finished'")
        self.assertLess(
            float(freed), 15.0,
            f"took {freed}s to free the lock, past this module's own "
            "worst case of 12s")


class TestLoggingAFailedAuditWriteCannotWedgeTheRunner(StalledStderrCase):
    """The same root cause, one step later and narrower: the worker's
    own `_log_error` for a failed store write sat inside the `try`
    whose `finally` releases the run lock. It needs the write to have
    failed first, but a stalled stderr then wedges the runner just as
    completely.
    """

    def test_a_stalled_stderr_after_a_failed_write_still_frees_the_lock(self):
        # A quiet action, so no queue drops: this isolates the worker's
        # own logging from the reader's.
        freed = self.run_child("""
            TRUE = Action("t.true", ("/bin/true",), root=False, risk=Risk.SAFE)

            class BrokenStore:
                def write_action_run(self, *a, **k):
                    raise RuntimeError("disk full")

            runner = ActionRunner(BrokenStore(), timeout_seconds=5)
            runner.start(TRUE, "127.0.0.1", lambda event: None)
            report(runner, 40)
        """)
        self.assertNotEqual(
            freed, "NEVER",
            "the run lock was never released: the worker is blocked "
            "writing a failed-audit-write message to a stalled stderr, "
            "between the audit write and the lock release")
        self.assertLess(float(freed), 15.0, f"took {freed}s to free the lock")


class TestCloseIsAtomicAgainstAConcurrentEmit(unittest.TestCase):
    """The property the whole _EventStream design rests on, which until
    now was pinned by nothing: a mutant that splits `close()` into two
    critical sections -- appending "finished" and setting `_closed`
    non-atomically -- passed the entire runner suite.

    `TestTheEventStreamRefusesEventsAfterItCloses` carries the refusal
    half and kills a delete-the-check mutant in milliseconds, but it
    calls `close()` and then `emit()` sequentially, so `_closed` is
    already true either way and it cannot see atomicity at all.

    So: race them. An `output` emit and `close("finished")` are
    released from a shared barrier with jitter, with the delivery
    thread parked inside the caller's callback so a backlog exists and
    a wrongly-accepted event is genuinely handed over rather than
    merely queued behind a thread that has run dry. Exactly two
    outcomes are legal -- the emit took the mutex first and is
    delivered before "finished", or it took it second and is refused --
    and this asserts both occur (otherwise nothing was actually racing)
    and that a third never does.

    Resolution, measured, because this test is easy to over-trust: it
    kills a non-atomic close whose gap is 1 ms in 232 of 400 trials. It
    does NOT kill one whose gap is a bare mutex release and reacquire
    -- 0 of 400, for two separate mutant shapes -- because CPython's
    uncontended reacquire beats every waiter to the lock, even with
    four threads hammering it and the switch interval at 1 us. No
    pure-Python racer can resolve that window. The guard against it is
    the module docstring's rule and the test below that enforces it by
    parsing the source, not this one.
    """

    TRIALS = 400

    def test_a_racing_emit_is_either_before_finished_or_refused(self):
        rng = random.Random(20260830)
        before = refused = 0
        violations = []

        for trial in range(self.TRIALS):
            delivered = []
            entered, release = threading.Event(), threading.Event()

            def listener(event):
                delivered.append(event["phase"])
                if event["phase"] == "started":
                    entered.set()
                    release.wait(30)

            stream = runner_module._EventStream(listener, f"race-{trial}")
            stream.start()
            stream.emit("started", action_id="t.race")
            self.assertTrue(entered.wait(5), "the pump never started")
            # A backlog, so an accepted racer really is delivered.
            stream.emit("output", line="backlog")

            barrier = threading.Barrier(2)
            emit_jitter, close_jitter = rng.uniform(0, 4e-4), rng.uniform(0, 4e-4)

            def emitter():
                barrier.wait()
                time.sleep(emit_jitter)
                stream.emit("output", line="racer")

            def closer():
                barrier.wait()
                time.sleep(close_jitter)
                stream.close("finished", exit_code=0)

            threads = [threading.Thread(target=emitter),
                       threading.Thread(target=closer)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            release.set()
            stream.join(10)

            self.assertIn("finished", delivered, f"trial {trial}: {delivered}")
            tail = delivered[delivered.index("finished") + 1:]
            if tail:
                violations.append((trial, delivered))
            elif delivered.count("output") == 2:
                before += 1
            else:
                refused += 1

        self.assertEqual(
            len(violations), 0,
            f"{len(violations)} of {self.TRIALS} trials delivered an event "
            "after 'finished'; first offending sequence: "
            f"{violations[0][1] if violations else None}")
        self.assertGreater(
            before, 0,
            "no trial delivered the racing emit before 'finished' -- the "
            "two calls are not actually racing, so this test proves nothing")
        self.assertGreater(
            refused, 0,
            "no trial refused the racing emit -- the two calls are not "
            "actually racing, so this test proves nothing")


class TestTheModuleObeysItsOwnIORule(unittest.TestCase):
    """The module docstring's property 3, enforced by parsing the source
    instead of by asking a reader to notice:

        No I/O of any kind -- including this module's own logging --
        under `_cv` or `_OutputBuffer._lock`, and none between the audit
        write and `self._lock.release()`.

    Four rounds of fixes each reasoned their way to a correct answer for
    the instance in front of them and missed the next one. The fourth
    was this module's own `_log` under `_cv`, which no test and no
    reviewer caught until it was measured. A grep is cheaper than a
    fifth round of reasoning.
    """

    BANNED = {"_log", "_log_error", "print"}

    def source(self):
        path = Path(runner_module.__file__)
        return ast.parse(path.read_text()), path

    def banned_calls_in(self, node):
        found = []
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name in self.BANNED or name == "_on_event":
                found.append((name, inner.lineno))
        return found

    def test_no_io_runs_under_either_mutex(self):
        tree, path = self.source()
        offences = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Anything named *_locked is called with a mutex already
            # held (that is what the suffix is for), so its whole body
            # counts as inside the block.
            if node.name.endswith("_locked"):
                offences += [(node.name, n, ln)
                             for n, ln in self.banned_calls_in(node)]
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.With):
                    continue
                held = {ast.unparse(item.context_expr) for item in inner.items}
                if not held & {"self._cv", "self._lock", "self._cv.notify"}:
                    continue
                offences += [(node.name, n, ln)
                             for n, ln in self.banned_calls_in(inner)]
        self.assertEqual(
            offences, [],
            f"{path.name} performs I/O while holding a mutex its own "
            f"threads contend on: {offences}. A write to a stalled "
            "stderr never returns, so this hands the run lock to "
            "whoever is reading that fd.")

    def test_no_io_sits_between_the_audit_write_and_the_lock_release(self):
        tree, path = self.source()
        run = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_run")
        writes = [n.lineno for n in ast.walk(run) if isinstance(n, ast.Call)
                  and ast.unparse(n.func).endswith("write_action_run")]
        releases = [n.lineno for n in ast.walk(run) if isinstance(n, ast.Call)
                    and ast.unparse(n.func) == "self._lock.release"]
        self.assertEqual(len(writes), 1, "expected one audit write in _run")
        self.assertEqual(len(releases), 1, "expected one lock release in _run")
        trapped = [(name, line) for name, line in self.banned_calls_in(run)
                   if writes[0] < line < releases[0]]
        self.assertEqual(
            trapped, [],
            f"{path.name}:_run performs I/O between the audit write "
            f"(line {writes[0]}) and the lock release (line "
            f"{releases[0]}): {trapped}. Log it after the release.")
