import os
import signal
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
