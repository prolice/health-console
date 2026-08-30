# Action Catalogue B1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the machinery that lets the browser run a declared privileged command on this machine — catalogue, execution lock, timeout, audit, live output, confirmation, sudoers packaging — carrying exactly one action, `apt.refresh`.

**Architecture:** A frozen catalogue of `Action` records (data, no logic) in `healthconsole/actions.py`; an `ActionRunner` in `healthconsole/runner.py` that owns the process-wide lock, the subprocess, the timeout and the audit write; three HTTP routes; `action` events on the existing SSE channel; and a third "Actions" tab in the front end holding the action list, the live output and the audit log. The console never runs as root: it renders a `sudoers` rule for the operator to install.

**Tech Stack:** Python standard library (`subprocess`, `threading`, `sqlite3`), no new dependency. Front end is native ES modules on the already-vendored Bootstrap 5.3.8.

**Spec:** [`docs/superpowers/specs/2026-08-30-action-catalogue-b1-design.md`](../specs/2026-08-30-action-catalogue-b1-design.md)

## Global Constraints

Every task's requirements implicitly include this section.

- **The browser sends an identifier, never a command fragment.** `argv` is a frozen tuple, `shell=False`, and no text from the network enters an argument list. There is no route that runs a supplied command.
- **No parameterised actions in B1.** The injection surface is nil by construction, not by validation.
- **The console never runs as root and never writes to `/etc`.** No flag exists that makes it do so.
- **No new dependency**, no build step, no `package.json`.
- **Machine-readable error codes only** in API responses — `{"error": "...", "detail": "..."}`, never user-facing prose. The browser localises.
- **Every user-facing string comes from `web/i18n/*.json`.** Both locales carry identical key and placeholder sets; every new key goes in `REQUIRED_UI_KEYS` in `tests/test_i18n.py`.
- **Colour never carries information alone**; every risk level shows an icon and a word. Touch targets ≥ 44 px.
- **CSP is `default-src 'self'; img-src 'self' data:`** — no `style="…"` in markup, no `setAttribute("style", …)`; both are dropped silently.
- **`web/js/*.js` under 81,920 bytes** (currently ~62,900).
- **`Store` serialises every connection access on its internal `RLock`.** Any new store method must hold it for the whole cursor lifecycle.
- Run the suite with `./run-tests`; the JavaScript units run inside it via `tests/test_js_units.py`.
- Code, comments, documentation and commit messages in English.

---

## Task 1: Store — write and read the audit log

The `action_run` table and its retention already exist. Nothing writes to it.

**Files:**
- Modify: `healthconsole/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Store.write_action_run(run_id: str, ts: int, action_id: str, source: str, exit_code: int | None, duration_ms: int, output: str) -> None`
  - `Store.read_action_runs(limit: int = 50) -> list[dict]` — newest first, each `{"id", "ts", "action_id", "source", "exit_code", "duration_ms", "output"}`

- [ ] **Step 1: Write the failing test**

```python
class TestActionAudit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = Store(Path(self.dir.name) / "db.sqlite3")
        self.addCleanup(self.store.close)

    def test_a_run_is_written_and_read_back_whole(self):
        self.store.write_action_run(
            "r1", 1000, "apt.refresh", "127.0.0.1", 0, 2140, "Hit:1 …\nDone\n")
        rows = self.store.read_action_runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "r1")
        self.assertEqual(rows[0]["action_id"], "apt.refresh")
        self.assertEqual(rows[0]["exit_code"], 0)
        self.assertEqual(rows[0]["duration_ms"], 2140)
        self.assertIn("Done", rows[0]["output"])

    def test_a_timed_out_run_records_a_null_exit_code(self):
        # A killed process has no exit code of its own. Storing 0 would
        # make a timeout indistinguishable from a success in the log --
        # the one place a reader looks to find out what happened.
        self.store.write_action_run(
            "r2", 1001, "apt.refresh", "127.0.0.1", None, 1_800_000, "")
        self.assertIsNone(self.store.read_action_runs()[0]["exit_code"])

    def test_runs_come_back_newest_first(self):
        for index, ts in enumerate((1000, 3000, 2000)):
            self.store.write_action_run(
                f"r{index}", ts, "apt.refresh", "127.0.0.1", 0, 1, "")
        self.assertEqual([row["ts"] for row in self.store.read_action_runs()],
                         [3000, 2000, 1000])

    def test_the_limit_is_honoured(self):
        for index in range(5):
            self.store.write_action_run(
                f"r{index}", 1000 + index, "apt.refresh", "127.0.0.1", 0, 1, "")
        self.assertEqual(len(self.store.read_action_runs(limit=2)), 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k TestActionAudit -v`
Expected: FAIL — `Store` has no attribute `write_action_run`.

- [ ] **Step 3: Write the implementation**

In `healthconsole/store.py`, alongside the other read/write methods:

```python
    def write_action_run(self, run_id: str, ts: int, action_id: str,
                         source: str, exit_code: int | None,
                         duration_ms: int, output: str) -> None:
        # exit_code is None for a run that was killed: a timeout has no
        # exit status of its own, and recording 0 would make it read as a
        # success in the one place someone looks to find out what happened.
        with self._lock:
            self.conn.execute(
                "INSERT INTO action_run"
                "(id, ts, action_id, source, exit_code, duration_ms, output) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, ts, action_id, source, exit_code, duration_ms, output))
            self.conn.commit()

    def read_action_runs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            cursor = self.conn.execute(
                "SELECT id, ts, action_id, source, exit_code, duration_ms, "
                "output FROM action_run ORDER BY ts DESC, rowid DESC LIMIT ?",
                (limit,))
            rows = cursor.fetchall()
        return [{"id": row[0], "ts": int(row[1]), "action_id": row[2],
                 "source": row[3],
                 "exit_code": None if row[4] is None else int(row[4]),
                 "duration_ms": int(row[5]), "output": row[6]}
                for row in rows]
```

Note `rowid DESC` in the tiebreak: two runs can share a second, and the log must still read in the order they happened.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run-tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add healthconsole/store.py tests/test_store.py
git commit -m "Read and write the action audit log

The action_run table and its 365-day retention have existed since the
foundation with nothing to write to them. A killed run records a null exit
code rather than 0, so a timeout cannot read as a success in the one place
someone looks to find out what happened."
```

---

## Task 2: The catalogue

**Files:**
- Create: `healthconsole/actions.py`
- Test: `tests/test_actions.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Risk` — `Enum` with `SAFE = "safe"`, `MEDIUM = "medium"`, `SENSITIVE = "sensitive"`
  - `Action` — frozen dataclass `(id: str, argv: tuple[str, ...], root: bool, risk: Risk)`
  - `CATALOGUE: dict[str, Action]`
  - `ACTION_IDS: frozenset[str]`
  - `UnknownAction(Exception)`
  - `lookup(action_id: str) -> Action` — raises `UnknownAction`

- [ ] **Step 1: Write the failing test**

```python
import unittest

from healthconsole.actions import (
    ACTION_IDS, CATALOGUE, Risk, UnknownAction, lookup,
)

SHELL_METACHARACTERS = set(";|&$><`\n\\\"'*?[]{}()!~")


class TestCatalogueIsData(unittest.TestCase):
    """The catalogue is what someone reads to find out what this console
    can run on their machine. It must stay trivially auditable."""

    def test_every_argv_is_a_frozen_tuple(self):
        for action in CATALOGUE.values():
            self.assertIsInstance(action.argv, tuple, action.id)

    def test_every_argv_starts_with_an_absolute_path(self):
        # A bare name would resolve through PATH, which is attacker-
        # influenceable in ways an absolute path is not.
        for action in CATALOGUE.values():
            self.assertTrue(action.argv[0].startswith("/"), action.id)

    def test_no_argument_contains_a_shell_metacharacter(self):
        # shell=False means these would be harmless, but an argument that
        # needs quoting is a sign the catalogue is drifting towards being
        # a command line rather than a fixed argument list.
        for action in CATALOGUE.values():
            for argument in action.argv:
                self.assertFalse(
                    SHELL_METACHARACTERS & set(argument),
                    f"{action.id}: {argument!r}")

    def test_the_id_matches_its_key(self):
        for key, action in CATALOGUE.items():
            self.assertEqual(key, action.id)

    def test_b1_ships_exactly_one_action(self):
        # B1 is deliberately a thin slice: eleven of the design document's
        # thirteen actions have no inputs until their probes exist. If this
        # fails, either a probe landed and the spec needs updating, or an
        # action was added without one.
        self.assertEqual(set(CATALOGUE), {"apt.refresh"})

    def test_an_action_is_immutable(self):
        action = CATALOGUE["apt.refresh"]
        with self.assertRaises(Exception):
            action.argv = ("/bin/true",)


class TestLookup(unittest.TestCase):
    def test_a_known_id_returns_its_action(self):
        self.assertIs(lookup("apt.refresh"), CATALOGUE["apt.refresh"])

    def test_an_unknown_id_raises(self):
        with self.assertRaises(UnknownAction):
            lookup("rm.everything")

    def test_action_ids_matches_the_catalogue(self):
        self.assertEqual(ACTION_IDS, frozenset(CATALOGUE))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k TestCatalogueIsData -v`
Expected: FAIL — no module named `healthconsole.actions`.

- [ ] **Step 3: Write `healthconsole/actions.py`**

```python
"""The action catalogue: what this console is allowed to run.

Data, never logic. Someone auditing what the console can do to their
machine reads this file and nothing else, so it must stay readable in
thirty seconds. Execution lives in runner.py.

The browser sends an identifier. `argv` is a frozen tuple, `shell=False`,
and no text arriving from the network is ever interpolated into it. That
is the whole of what separates an action catalogue from a remote shell.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Risk(Enum):
    SAFE = "safe"
    MEDIUM = "medium"
    SENSITIVE = "sensitive"


class UnknownAction(Exception):
    """An identifier that is not in the catalogue."""


@dataclass(frozen=True)
class Action:
    id: str
    argv: tuple[str, ...]
    root: bool
    risk: Risk


CATALOGUE: dict[str, Action] = {
    "apt.refresh": Action(
        id="apt.refresh",
        # Refreshes the package lists. Changes nothing on the system beyond
        # /var/lib/apt/lists, which is why it is the one action B1 carries.
        argv=("/usr/bin/apt-get", "update"),
        root=True,
        risk=Risk.SAFE,
    ),
}

ACTION_IDS: frozenset[str] = frozenset(CATALOGUE)


def lookup(action_id: str) -> Action:
    try:
        return CATALOGUE[action_id]
    except KeyError:
        raise UnknownAction(action_id) from None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run-tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add healthconsole/actions.py tests/test_actions.py
git commit -m "Declare the action catalogue

One entry. The tests assert what makes the file auditable rather than what
it currently contains: absolute paths, frozen tuples, no argument that
would need quoting. An argument needing quotes is a sign the catalogue is
drifting towards being a command line."
```

---

## Task 3: The runner

**Files:**
- Create: `healthconsole/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `Action`, `Risk`, `lookup`, `UnknownAction` from `actions.py`; `Store.write_action_run` from Task 1.
- Produces:
  - `ActionBusy(Exception)`
  - `ActionRunner(store, timeout_seconds: int = 1800, sudo: tuple[str, ...] = ("/usr/bin/sudo", "-n"))`
  - `ActionRunner.start(action: Action, source: str, on_event) -> str` — returns a `run_id`; raises `ActionBusy`. `on_event(dict)` is called with the SSE payloads.
  - `ActionRunner.is_busy() -> bool`
  - `ActionRunner.wait(timeout: float | None = None) -> None` — for tests

- [ ] **Step 1: Write the failing test**

```python
import tempfile
import threading
import time
import unittest
from pathlib import Path

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k TestSuccess -v`
Expected: FAIL — no module named `healthconsole.runner`.

- [ ] **Step 3: Write `healthconsole/runner.py`**

Implement to satisfy the tests. The shape:

```python
"""Running a catalogue action, once at a time, and recording what happened.

Separate from actions.py on purpose: the catalogue is data an auditor
reads, this is the machinery that acts on it.
"""
```

- One `threading.Lock` guarding a `_current` slot; `start()` acquires without blocking and raises `ActionBusy` if taken.
- `run_id` from `secrets.token_hex(8)`.
- `subprocess.Popen(argv, stdout=PIPE, stderr=STDOUT, text=True, shell=False)`. Prefix `argv` with the `sudo` tuple when `action.root` is true.
- A worker thread reads stdout line by line, calling `on_event` per line and appending to a buffer.
- A watchdog enforces `timeout_seconds`: `terminate()`, then `kill()` after a short grace.
- **The audit row is written in a `finally`**, so success, non-zero exit, missing binary and timeout all produce a row. `exit_code` is `None` when the process was killed or never started.
- The lock is released in the same `finally`, after the audit write.
- `cancel()` terminates the current run; it exists for the tests and for shutdown.

Two things the tests pin and the implementation must honour: a missing binary is caught (`OSError`) and audited with the error text as output, and `is_busy()` is false again once the audit row exists — not before, or a caller could start a second run whose row races the first.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run-tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add healthconsole/runner.py tests/test_runner.py
git commit -m "Run one catalogue action at a time, and record what happened

The audit row is written in a finally: success, non-zero exit, missing
binary and timeout all leave a trace. A run that produces no row is the
worst outcome for a log whose only job is to say what the console did to
the machine.

Tested against /bin/true, /bin/false, a nonexistent path and sleep, so the
timeout and the lock are exercised without touching anything privileged."
```

---

## Task 4: Message catalogue keys

Before the routes and the interface, so both locales move in one commit.

**Files:**
- Modify: `web/i18n/en.json`, `web/i18n/fr.json`
- Modify: `tests/test_i18n.py`
- Test: `tests/test_i18n.py`

**Interfaces:**
- Produces: the keys consumed by Task 8.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_i18n.py`, alongside `REQUIRED_UI_KEYS`:

```python
from healthconsole.actions import ACTION_IDS


class TestActionCatalogueWording(unittest.TestCase):
    def test_every_action_has_a_label_a_description_and_a_confirmation(self):
        for locale in locales():
            catalogue = load(locale)
            for action_id in sorted(ACTION_IDS):
                for suffix in ("label", "description", "confirm"):
                    key = f"action.{action_id}.{suffix}"
                    self.assertIn(key, catalogue, f"{locale} lacks {key}")

    def test_every_risk_level_has_a_word_and_an_icon(self):
        # Colour never carries the state alone.
        from healthconsole.actions import Risk
        for locale in locales():
            catalogue = load(locale)
            for risk in Risk:
                for suffix in ("word", "icon"):
                    key = f"risk.{risk.value}.{suffix}"
                    self.assertIn(key, catalogue, f"{locale} lacks {key}")
```

And extend `REQUIRED_UI_KEYS` with:

```python
    # Actions tab
    "ui.mode.actions", "ui.actions.available", "ui.actions.audit",
    "ui.actions.none", "ui.actions.run", "ui.actions.running",
    "ui.actions.output", "ui.actions.confirm.title",
    "ui.actions.confirm.cancel", "ui.actions.confirm.go",
    "ui.actions.result.ok", "ui.actions.result.failed",
    "ui.actions.result.killed",
    "ui.actions.audit.when", "ui.actions.audit.what",
    "ui.actions.audit.source", "ui.actions.audit.outcome",
    "ui.actions.audit.empty",
    "ui.error.action.busy", "ui.error.action.refused",
    "ui.error.action.failed",
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k "TestActionCatalogueWording or test_required_ui_keys" -v`
Expected: FAIL — `en lacks action.apt.refresh.label`.

- [ ] **Step 3: Add the keys to `web/i18n/en.json`**

```json
  "ui.mode.actions": "Actions",
  "ui.actions.available": "Available actions",
  "ui.actions.audit": "What has been run",
  "ui.actions.none": "No action is available right now.",
  "ui.actions.run": "Run",
  "ui.actions.running": "Running…",
  "ui.actions.output": "Output",
  "ui.actions.confirm.title": "Run this action?",
  "ui.actions.confirm.cancel": "Cancel",
  "ui.actions.confirm.go": "Run it",
  "ui.actions.result.ok": "Finished in {seconds} s.",
  "ui.actions.result.failed": "Failed with code {code} after {seconds} s.",
  "ui.actions.result.killed": "Stopped after {seconds} s without finishing.",
  "ui.actions.audit.when": "When",
  "ui.actions.audit.what": "Action",
  "ui.actions.audit.source": "From",
  "ui.actions.audit.outcome": "Outcome",
  "ui.actions.audit.empty": "Nothing has been run yet.",

  "risk.safe.word": "Safe",
  "risk.safe.icon": "●",
  "risk.medium.word": "Changes the system",
  "risk.medium.icon": "⚠",
  "risk.sensitive.word": "Interrupts the machine",
  "risk.sensitive.icon": "✖",

  "action.apt.refresh.label": "Refresh the list of available updates",
  "action.apt.refresh.description": "Asks the software sources what versions they now offer. Nothing on this computer is changed or installed.",
  "action.apt.refresh.confirm": "This contacts your software sources and updates the local list of available versions. It installs nothing.",

  "ui.error.action.busy": "Another action is already running. Wait for it to finish.",
  "ui.error.action.refused": "Actions can only be run from this computer.",
  "ui.error.action.failed": "The action could not be started.",
```

- [ ] **Step 4: Add the matching keys to `web/i18n/fr.json`**

```json
  "ui.mode.actions": "Actions",
  "ui.actions.available": "Actions disponibles",
  "ui.actions.audit": "Ce qui a été lancé",
  "ui.actions.none": "Aucune action n'est disponible pour le moment.",
  "ui.actions.run": "Lancer",
  "ui.actions.running": "En cours…",
  "ui.actions.output": "Sortie",
  "ui.actions.confirm.title": "Lancer cette action ?",
  "ui.actions.confirm.cancel": "Annuler",
  "ui.actions.confirm.go": "Lancer",
  "ui.actions.result.ok": "Terminé en {seconds} s.",
  "ui.actions.result.failed": "Échec avec le code {code} après {seconds} s.",
  "ui.actions.result.killed": "Interrompu après {seconds} s sans avoir abouti.",
  "ui.actions.audit.when": "Quand",
  "ui.actions.audit.what": "Action",
  "ui.actions.audit.source": "Depuis",
  "ui.actions.audit.outcome": "Résultat",
  "ui.actions.audit.empty": "Rien n'a encore été lancé.",

  "risk.safe.word": "Sans risque",
  "risk.safe.icon": "●",
  "risk.medium.word": "Modifie le système",
  "risk.medium.icon": "⚠",
  "risk.sensitive.word": "Interrompt la machine",
  "risk.sensitive.icon": "✖",

  "action.apt.refresh.label": "Actualiser la liste des mises à jour disponibles",
  "action.apt.refresh.description": "Demande aux sources de logiciels quelles versions elles proposent désormais. Rien n'est modifié ni installé sur cet ordinateur.",
  "action.apt.refresh.confirm": "Cette action contacte vos sources de logiciels et met à jour la liste locale des versions disponibles. Elle n'installe rien.",

  "ui.error.action.busy": "Une autre action est déjà en cours. Attendez qu'elle se termine.",
  "ui.error.action.refused": "Les actions ne peuvent être lancées que depuis cet ordinateur.",
  "ui.error.action.failed": "L'action n'a pas pu être lancée.",
```

Note the risk words say what happens, not how alarming it is: "Changes the system" is checkable, "Medium risk" is not.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./run-tests -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/i18n tests/test_i18n.py
git commit -m "Add the message-catalogue keys the Actions tab needs

Both locales in one commit: the suite requires an identical key set, so
adding them piecemeal would leave it red in between.

The risk words say what an action does -- 'Changes the system' -- rather
than how alarming it is. A reader can check the first against what they
see; 'medium risk' asks them to trust a number they did not choose."
```

---

## Task 5: Reading routes

**Files:**
- Modify: `healthconsole/server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `CATALOGUE`, `Risk` from `actions.py`; `Store.read_action_runs` from Task 1.
- Produces: `GET /api/actions` → `[{"id", "risk", "available"}]`; `GET /api/actions/runs` → `{"runs": [...]}`.

- [ ] **Step 1: Write the failing test**

```python
class TestActionReadRoutes(ServerCase):
    def test_the_catalogue_lists_every_action_with_its_risk(self):
        body = self.get_json("/api/actions")
        self.assertEqual([entry["id"] for entry in body], ["apt.refresh"])
        self.assertEqual(body[0]["risk"], "safe")
        self.assertTrue(body[0]["available"])

    def test_the_catalogue_carries_no_prose(self):
        # The API is locale-neutral: ids and risk levels only, so one
        # response serves both languages and switching needs no round trip.
        raw = self.get_raw("/api/actions")
        for word in ("Refresh", "Actualiser", "Safe", "Sans risque"):
            self.assertNotIn(word, raw)

    def test_the_audit_log_is_empty_before_anything_runs(self):
        self.assertEqual(self.get_json("/api/actions/runs")["runs"], [])

    def test_the_audit_log_returns_what_the_store_holds(self):
        self.store.write_action_run(
            "r1", 1000, "apt.refresh", "127.0.0.1", 0, 2140, "Done\n")
        runs = self.get_json("/api/actions/runs")["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["action_id"], "apt.refresh")
```

Follow the existing `tests/test_server.py` fixture pattern for starting a
server against a real `Store` and `Scheduler`; add `get_json`/`get_raw`
helpers if the file has none.

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k TestActionReadRoutes -v`
Expected: FAIL — 404 `unknown_route`.

- [ ] **Step 3: Add the routes**

In `do_GET`'s routing block, before the final 404:

```python
            if parsed.path == "/api/actions":
                return self._json(200, [
                    # `available` is always true in B1: every gate that
                    # could make it false reads probe data that does not
                    # exist yet. It ships anyway so the front end honours
                    # it from the start rather than being retrofitted.
                    {"id": action.id, "risk": action.risk.value,
                     "available": True}
                    for action in CATALOGUE.values()], extra)
            if parsed.path == "/api/actions/runs":
                return self._json(
                    200, {"runs": scheduler.store.read_action_runs()}, extra)
```

Wrap the store read the way `_history` does, so a `sqlite3.Error` during
shutdown returns a 503 rather than a traceback.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run-tests -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add healthconsole/server.py tests/test_server.py
git commit -m "Serve the action catalogue and the audit log

Both read-only and both locale-neutral: ids and risk levels, never prose,
so one response serves English and French and switching language needs no
round trip. A test asserts the absence of translated words rather than the
presence of the right ones -- the failure it guards against is prose
leaking in, not prose going missing."
```

---

## Task 6: `POST /api/actions/<id>` and the SSE events

**Files:**
- Modify: `healthconsole/server.py`
- Modify: `healthconsole/cli.py` (construct the `ActionRunner`, pass it to `make_server`)
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `ActionRunner.start`, `ActionBusy` from Task 3; `lookup`, `UnknownAction` from Task 2.
- Produces: `POST /api/actions/<id>` → `202 {"run_id": "..."}`; `action` events on `/api/stream`.

- [ ] **Step 1: Write the failing test**

```python
class TestActionPost(ServerCase):
    def test_an_unknown_action_is_refused(self):
        status, body = self.post("/api/actions/rm.everything")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "unknown_action")

    def test_a_known_action_is_accepted_and_returns_a_run_id(self):
        status, body = self.post("/api/actions/t.true")
        self.assertEqual(status, 202)
        self.assertTrue(body["run_id"])

    def test_a_second_action_while_one_runs_is_refused(self):
        self.post("/api/actions/t.sleep")
        status, body = self.post("/api/actions/t.true")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "action_busy")

    def test_an_action_from_off_loopback_is_refused_by_default(self):
        # Reading and acting do not carry the same cost when you get it
        # wrong: a token is enough to read, never enough to act.
        status, body = self.post("/api/actions/t.true",
                                 client_ip="192.168.0.3")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "remote_actions_refused")

    def test_off_loopback_is_allowed_when_configured(self):
        self.cfg = replace(self.cfg, allow_remote_actions=True)
        status, _ = self.post("/api/actions/t.true", client_ip="192.168.0.3")
        self.assertEqual(status, 202)

    def test_get_on_the_action_route_is_not_a_way_to_run_it(self):
        # A route that acts must not be reachable by a link, a prefetch or
        # a crawler.
        self.assertEqual(self.get_status("/api/actions/t.true"), 404)
```

Inject a test catalogue rather than shelling out to `apt-get`: give
`make_server` the runner, and build the runner in the test over a catalogue
containing `/bin/true` and `/bin/sleep`.

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k TestActionPost -v`
Expected: FAIL — 501, the base class rejects POST.

- [ ] **Step 3: Implement `do_POST`**

```python
        def do_POST(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            authorised, handoff = self._authorised(query)
            if not authorised:
                return self._error(401, "token_required", "")
            extra = ({"Set-Cookie": session_cookie_header(handoff)}
                     if handoff else None)

            prefix = "/api/actions/"
            if not parsed.path.startswith(prefix):
                return self._error(404, "unknown_route", parsed.path)

            # Reading is open to the LAN behind a token; acting is not.
            # A token proves who you are, not that you are sitting at this
            # machine, and the two do not carry the same cost when wrong.
            if not is_loopback(self.client_address[0]) \
                    and not cfg.allow_remote_actions:
                return self._error(403, "remote_actions_refused", "")

            try:
                action = lookup(parsed.path[len(prefix):])
            except UnknownAction:
                return self._error(404, "unknown_action", "")
            try:
                run_id = runner.start(action, self.client_address[0],
                                      broadcast_action)
            except ActionBusy:
                return self._error(409, "action_busy", "")
            return self._json(202, {"run_id": run_id}, extra)
```

`broadcast_action` appends the event to a bounded deque that `_stream`
drains alongside its state ticks, emitting `event: action`. Keep the deque
small — a run's output is already being written to the audit row, and the
stream is a live view, not a transcript.

**Do not add the action path to `do_HEAD`.** `do_HEAD = do_GET` is
deliberate; a route that acts must never answer a HEAD.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run-tests -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add healthconsole/server.py healthconsole/cli.py tests/test_server.py
git commit -m "Accept an action by identifier, stream its output

POST returns a run_id immediately and the output arrives over the SSE
channel that already exists: apt-get update takes anywhere from two to
forty seconds depending on the mirror, and a blind spinner is what the
design document set out to avoid.

Acting is refused off-loopback even with a valid token. A token proves who
you are, not that you are sitting at the machine, and reading and acting do
not carry the same cost when you get it wrong."
```

---

## Task 7: The sudoers rule and the `sudoers` command

**Files:**
- Create: `healthconsole/sudoers.py`
- Create: `packaging/.gitkeep`
- Modify: `healthconsole/cli.py`
- Test: `tests/test_sudoers.py`

**Interfaces:**
- Consumes: `CATALOGUE` from Task 2.
- Produces: `render(user: str) -> str`; `cmd_sudoers(cfg, config_path, rotate) -> int`; `COMMANDS["sudoers"]`.

- [ ] **Step 1: Write the failing test**

```python
class TestSudoersRule(unittest.TestCase):
    def test_it_names_only_binaries_the_catalogue_uses(self):
        text = render("prolice")
        for action in CATALOGUE.values():
            if action.root:
                self.assertIn(" ".join(action.argv), text)

    def test_it_contains_no_wildcard_and_never_all(self):
        text = render("prolice")
        self.assertNotIn("*", text)
        self.assertNotIn("ALL=(ALL", text)
        self.assertNotIn("NOPASSWD: ALL", text)

    def test_it_carries_the_transitional_warning(self):
        # Whoever reads /etc/sudoers.d/health-console in two years must
        # find out from the file itself that naming a login account was a
        # stepping stone, not the design.
        self.assertIn("system user", render("prolice"))

    def test_visudo_accepts_it(self):
        visudo = shutil.which("visudo") or "/usr/sbin/visudo"
        if not Path(visudo).exists():
            self.skipTest("visudo not installed")
        with tempfile.NamedTemporaryFile("w", suffix=".sudoers") as handle:
            handle.write(render("prolice"))
            handle.flush()
            result = subprocess.run([visudo, "-c", "-f", handle.name],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0,
                             result.stdout + result.stderr)

    def test_the_user_is_substituted_not_left_as_a_placeholder(self):
        self.assertIn("prolice", render("prolice"))
        self.assertNotIn("<user>", render("prolice"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k TestSudoersRule -v`
Expected: FAIL — no module named `healthconsole.sudoers`.

- [ ] **Step 3: Write `healthconsole/sudoers.py`**

`render(user)` builds the file from `CATALOGUE`, emitting one `NOPASSWD`
line per action with `root=True`, arguments included verbatim, and a header
comment carrying the transitional warning from §5 of the spec. It must
never emit a wildcard and never `ALL` as a command.

- [ ] **Step 4: Add the `sudoers` command to the CLI**

`cmd_sudoers` writes the rendered file to `packaging/sudoers.d/health-console`,
runs `visudo -c -f` on it, **refuses to print the install line if that check
fails**, and otherwise prints the content, the destination, and:

```
sudo install -m 0440 -o root -g root \
  packaging/sudoers.d/health-console /etc/sudoers.d/health-console
```

Add `"sudoers": cmd_sudoers` to `COMMANDS`. `argparse`'s `choices` reads
from `sorted(COMMANDS)`, so the help updates itself. There is **no flag
that writes to `/etc`**.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./run-tests -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add healthconsole/sudoers.py healthconsole/cli.py tests/test_sudoers.py packaging
git commit -m "Render the sudoers rule; never install it

The console has no code path that writes to /etc, not even one it declines
to take: an install mode that exists but goes unused is still an install
mode someone will eventually use. It renders the file, checks it with
visudo -c, and hands the operator the one command to run.

The file carries its own warning that naming a login account is
transitional. A NOPASSWD rule is granted to an account, not to this
program -- anything running as that user inherits it -- and whoever reads
the file in two years should learn that from the file."
```

---

## Task 8: The Actions tab

**Files:**
- Create: `web/js/actions.js`
- Modify: `web/index.html`, `web/js/app.js`, `web/style.css`
- Modify: `tests/test_web_assets.py`
- Test: `web/js/tests/actions.test.js`, `tests/test_web_assets.py`

**Interfaces:**
- Consumes: `translate`, `formatNumber` from `i18n.js`; `el`, `setText`, `clear` from `dom.js`.
- Produces: `renderActions()`, `paintActionsChrome()`, `onActionEvent(event)`.

- [ ] **Step 1: Write the failing test**

```python
class TestActionsTab(unittest.TestCase):
    def setUp(self):
        self.html = read("index.html")

    def test_a_third_tab_exists_and_is_wired_to_its_panel(self):
        self.assertIn('id="mode-actions"', self.html)
        self.assertIn('aria-controls="actions"', self.html)
        self.assertIn('aria-labelledby="mode-actions"', self.html)
        self.assertEqual(self.html.count('role="tabpanel"'), 3)

    def test_the_action_containers_are_present(self):
        for element_id in ("action-list", "action-output", "audit-table"):
            self.assertIn(f'id="{element_id}"', self.html)

    def test_risk_is_never_colour_alone(self):
        source = js("actions.js")
        self.assertIn("risk.${", source)
        self.assertIn(".icon", source)
        self.assertIn(".word", source)
```

The existing `test_tabs_are_associated_with_their_panels` asserts exactly
two `role="tabpanel"`. **Update it to three; do not delete it.**

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k TestActionsTab -v`
Expected: FAIL — `id="mode-actions"` not found.

- [ ] **Step 3: Add the tab to `index.html`**

A third `<button role="tab" id="mode-actions" aria-controls="actions">` in
`#mode-group`, and a third `<main id="actions" role="tabpanel"
aria-labelledby="mode-actions" hidden>` containing `#action-list`,
`#action-output` and `#audit-table`, plus a Bootstrap modal for the
confirmation.

- [ ] **Step 4: Write `web/js/actions.js`**

- `renderActions()` fetches `/api/actions`, builds one row per action: label, description, risk badge carrying **icon and word**, and a Run button disabled when `available` is false.
- Clicking Run opens the confirmation dialogue carrying `action.<id>.confirm`. B1 confirms every action; a comment must say why, and that the decision reopens when the catalogue holds more than one risk level.
- Confirming `POST`s and disables every Run button until the run finishes.
- `onActionEvent(event)` appends output lines to `#action-output` and, on `finished`, renders the outcome from `ui.actions.result.*` and refreshes the audit table.
- A `409` renders `ui.error.action.busy`, a `403` renders `ui.error.action.refused`, anything else renders `ui.error.action.failed`. **Never an empty panel** — a button that appears to do nothing is worse than one that says it failed.
- The audit table renders from `/api/actions/runs` with translated column headers.

Wire the tab into `switchMode` in `app.js` alongside Simple and Expert, and subscribe `onActionEvent` to the SSE `action` events.

- [ ] **Step 5: Run the tests and check the page**

Run: `./run-tests -v` and `node --test web/js/tests/*.test.js` → PASS.

Then `./bin/health-console run`, open the Actions tab, and confirm the list
renders and the audit table says it is empty. **Running the action needs the
sudoers rule installed** (Task 7); if it is not, the run will fail and the
audit row will record that — which is itself worth seeing once.

- [ ] **Step 6: Commit**

```bash
git add web tests/test_web_assets.py
git commit -m "Add the Actions tab

A third tab holding maintenance actions and the audit log. Actions that fix
a specific finding will belong on that finding's card in Simple mode when
the probes land -- this tab is not where action buttons go, or 'corrective
actions one click away' stops meaning anything.

A failed run says so. A button that appears to do nothing is worse than one
that reports a failure."
```

---

## Task 9: Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-29-health-console-design.md` (§8)

- [ ] **Step 1: Record the state of the catalogue in §8**

Add a note at the head of §8 that the section describes the destination,
that B1 implements the machinery plus `apt.refresh`, and that the remaining
twelve actions wait on probes that do not exist — pointing at the B1 spec.
**Extend, do not rewrite:** §8 is not wrong about where the project is
going, only about what exists.

- [ ] **Step 2: Document the `sudoers` command in the README**

Add it to the command list, and add a Security note saying plainly that a
`NOPASSWD` rule is granted to a user account rather than to the console, so
anything running as that user inherits the same privilege.

- [ ] **Step 3: Run the suite and commit**

```bash
./run-tests
git add README.md docs/superpowers/specs/2026-08-29-health-console-design.md
git commit -m "Say what the action catalogue actually contains

§8 describes thirteen actions; one exists. The section is right about the
destination and wrong about the present, so it gains a note rather than a
rewrite.

The README gains the sudoers command and the sentence that matters most
about it: the rule is granted to an account, not to this program."
```

---

## Self-review

**Spec coverage.** §1 slice rationale → Task 2's `test_b1_ships_exactly_one_action`. §2.1 no root path → Task 7. §2.2 the tab → Task 8. §3.1 catalogue → Task 2. §3.2 runner → Task 3. §4 routes → Tasks 5, 6. §5 the real cost → Tasks 7, 9. §6 interface → Task 8. §6.1 universal confirmation → Task 8. §7 packaging → Task 7. §8 testing → every task. §9 out of scope — nothing to build.

**Type consistency.** `write_action_run`'s parameter order is fixed in Task 1 and used unchanged in Task 3. `ActionRunner.start(action, source, on_event) -> str` is defined in Task 3 and called with that signature in Task 6. `Risk` values are the lowercase strings `"safe"`, `"medium"`, `"sensitive"` in Task 2, serialised as such in Task 5, and used as catalogue key fragments `risk.<value>.word` in Tasks 4 and 8.

**Known ordering constraint.** Task 4 precedes Task 8 because both catalogues must move in one commit; Task 7 precedes any real run of `apt.refresh`, but not Task 8, which renders and reports a failure honestly without it.
