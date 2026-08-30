# Health Console — Plan 1: Measuring Foundation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A running web console that measures the machine's vital signs live, interprets them, and renders them in Simple mode in English or French.

**Architecture:** A scheduler samples fast probes every 2 s into an in-memory ring buffer and writes one aggregated value to SQLite every 30 s. Probes produce only numbers; a pure rules engine turns them into findings and a score. Findings carry ids and parameters, never sentences — wording lives in JSON message catalogues resolved in the browser. A stdlib HTTP server serves the page and streams state over SSE.

**Tech Stack:** Python 3.14 (standard library only) · `psutil` 7.1 (apt package `python3-psutil`) · SQLite (`sqlite3`) · `tomllib` · tests with `unittest` · front end in HTML/CSS/native ES modules, no build step, no CDN.

**Spec:** `docs/superpowers/specs/2026-08-29-health-console-design.md`

## Global Constraints

These bind **every** task and are not repeated in each one.

- **No `pip` dependency, no virtualenv, no build step.** The system Python is
  `EXTERNALLY-MANAGED` (PEP 668). Only the standard library and `psutil` (already
  installed via apt, 7.1.0) are permitted.
- **Tests use stdlib `unittest`.** Single command: `python3 -m unittest discover -s tests -t . -v`. Never introduce pytest.
- **English everywhere in the repository**: code, identifiers, comments, docstrings,
  documentation and commit messages. User-facing wording is never written in the
  code — see the next constraint.
- **No user-facing string is hard-coded.** A `Finding` carries `id` and `params`;
  sentences live in `web/i18n/<locale>.json`. English is the default locale and the
  fallback; French ships alongside it.
- **No sensor value is displayed or stored without a plausibility check**
  (spec §7.6). An out-of-domain value becomes "incoherent", never a number.
- **The console is not allowed to lie** (spec §10.4): stale data is visibly marked,
  an unavailable probe says so, and no reassuring zero ever substitutes for a
  missing measurement.
- **Every threshold lives in `healthconsole/rules.py`**, nowhere else.
- **`collect()` measures, `evaluate()` judges.** `evaluate()` is pure: no system
  access, no subprocess, no clock. That is what makes it testable without hardware.
- **Budget**: < 60 MB RSS, < 2 % CPU on average, database ≈ 32 MB at default settings.
- **Accessibility** (spec §10.2): colour never carries information alone; every state
  has an icon and a word.
- **No outbound network access, no CDN.** Everything is served from disk.
- **Commit after every task**, imperative mood, English.

## File structure

| File | Single responsibility |
|---|---|
| `healthconsole/config.py` | Load, validate and cost the configuration |
| `healthconsole/plausibility.py` | Decide whether a sensor value is credible |
| `healthconsole/findings.py` | The `Finding` type, severities, and the id registry |
| `healthconsole/rules.py` | **All** thresholds, and nothing else |
| `healthconsole/verdict.py` | Explainable score and hysteresis |
| `healthconsole/ring.py` | In-memory ring buffer of the last 60 minutes |
| `healthconsole/store.py` | SQLite: schema, writes, reads, aggregation, retention |
| `healthconsole/probes/__init__.py` | Probe registry and shared contract |
| `healthconsole/probes/{cpu,memory,thermal,network,battery}.py` | One probe each |
| `healthconsole/scheduler.py` | The two cadences and their orchestration |
| `healthconsole/server.py` | HTTP routing, token, SSE |
| `healthconsole/cli.py` | `run`, `config`, `status`, `prune` |
| `web/{index.html,style.css,app.js}` | Simple mode and the locale switch |
| `web/i18n/{en,fr}.json` | Message catalogues |

---

### Task 1: Project skeleton, test harness and configuration

**Files:**
- Create: `healthconsole/__init__.py`, `healthconsole/config.py`
- Create: `tests/__init__.py`, `tests/test_config.py`
- Create: `run-tests`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `Retention`, `Sampling`, `Config` (frozen dataclasses), `ConfigError(ValueError)`, `load_config(path: Path | None) -> Config`, `estimate_db_bytes(cfg: Config, n_metrics: int) -> int`, `DEFAULT_CONFIG_PATH: Path`

- [ ] **Step 1: Create the tree and the test runner**

```bash
mkdir -p healthconsole/probes tests web/i18n bin
touch healthconsole/__init__.py healthconsole/probes/__init__.py tests/__init__.py
printf '#!/bin/sh\nexec python3 -m unittest discover -s tests -t . "$@"\n' > run-tests
chmod +x run-tests
```

- [ ] **Step 2: Write the failing tests**

File `tests/test_config.py`:

```python
import tempfile
import unittest
from pathlib import Path

from healthconsole.config import (
    Config, ConfigError, estimate_db_bytes, load_config,
)


def write_toml(text):
    path = Path(tempfile.mkdtemp()) / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestDefaults(unittest.TestCase):
    def test_absent_file_yields_defaults(self):
        cfg = load_config(Path("/nonexistent/config.toml"))
        self.assertEqual(cfg.retention.raw_days, 2)
        self.assertEqual(cfg.retention.aggregate_days, 90)
        self.assertEqual(cfg.sampling.live_seconds, 2)
        self.assertEqual(cfg.sampling.store_seconds, 30)
        self.assertEqual(cfg.port, 8787)
        self.assertFalse(cfg.allow_remote_actions)

    def test_partial_file_keeps_other_defaults(self):
        cfg = load_config(write_toml("[retention]\nraw_days = 5\n"))
        self.assertEqual(cfg.retention.raw_days, 5)
        self.assertEqual(cfg.retention.aggregate_days, 90)


class TestValidation(unittest.TestCase):
    def test_zero_days_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml("[retention]\nraw_days = 0\n"))
        self.assertIn("raw_days", str(ctx.exception))

    def test_raw_longer_than_aggregate_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml(
                "[retention]\nraw_days = 100\naggregate_days = 30\n"))
        self.assertIn("raw_days", str(ctx.exception))
        self.assertIn("aggregate_days", str(ctx.exception))

    def test_store_seconds_must_be_multiple_of_live(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml(
                "[sampling]\nlive_seconds = 2\nstore_seconds = 25\n"))
        self.assertIn("store_seconds", str(ctx.exception))

    def test_store_seconds_capped_at_300(self):
        with self.assertRaises(ConfigError):
            load_config(write_toml(
                "[sampling]\nlive_seconds = 2\nstore_seconds = 600\n"))

    def test_non_integer_days_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml('[retention]\nraw_days = "two"\n'))
        self.assertIn("raw_days", str(ctx.exception))

    def test_malformed_toml_names_the_file(self):
        path = write_toml("[retention\nraw_days = 2\n")
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn(str(path), str(ctx.exception))


class TestServerConfig(unittest.TestCase):
    def test_allow_remote_actions_string_false_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml('[server]\nallow_remote_actions = "false"\n'))
        self.assertIn("allow_remote_actions", str(ctx.exception))

    def test_allow_remote_actions_integer_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml('[server]\nallow_remote_actions = 1\n'))
        self.assertIn("allow_remote_actions", str(ctx.exception))

    def test_allow_remote_actions_boolean_true_loads(self):
        cfg = load_config(write_toml('[server]\nallow_remote_actions = true\n'))
        self.assertTrue(cfg.allow_remote_actions)

    def test_token_as_integer_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml('[server]\ntoken = 12345\n'))
        self.assertIn("token", str(ctx.exception))

    def test_bind_as_integer_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml('[server]\nbind = 42\n'))
        self.assertIn("bind", str(ctx.exception))

    def test_valid_server_section_loads(self):
        cfg = load_config(write_toml(
            '[server]\nbind = "127.0.0.1"\nport = 9000\n'
            'allow_remote_actions = false\ntoken = "secret"\n'))
        self.assertEqual(cfg.bind, "127.0.0.1")
        self.assertEqual(cfg.port, 9000)
        self.assertFalse(cfg.allow_remote_actions)
        self.assertEqual(cfg.token, "secret")


class TestEstimate(unittest.TestCase):
    def test_default_config_estimates_about_32_MB(self):
        size = estimate_db_bytes(Config(), n_metrics=25)
        self.assertGreater(size, 25_000_000)
        self.assertLess(size, 40_000_000)

    def test_estimate_grows_with_retention(self):
        from healthconsole.config import Retention
        small = estimate_db_bytes(Config(), n_metrics=25)
        big = estimate_db_bytes(
            Config(retention=Retention(raw_days=30, aggregate_days=365)),
            n_metrics=25)
        self.assertGreater(big, small * 5)

    def test_slower_store_rate_shrinks_the_database(self):
        from healthconsole.config import Sampling
        fast = estimate_db_bytes(Config(), n_metrics=25)
        slow = estimate_db_bytes(
            Config(sampling=Sampling(live_seconds=2, store_seconds=300)),
            n_metrics=25)
        self.assertLess(slow, fast)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.config'`

- [ ] **Step 4: Write the implementation**

File `healthconsole/config.py`:

```python
"""Configuration loading, validation and cost estimation.

An invalid configuration stops the service and names the offending field.
Starting up while silently ignoring a broken setting is a trap.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "health-console" / "config.toml"

# Average cost of one metric row including its index, key normalised to an int.
BYTES_PER_METRIC_ROW = 40
# Weight of the non-metric tables (snapshots, events, audit) in steady state.
OVERHEAD_BYTES = 3_000_000
AGGREGATE_PERIOD_SECONDS = 300
SECONDS_PER_DAY = 86_400


class ConfigError(ValueError):
    """Invalid configuration. The message always names the offending field."""


@dataclass(frozen=True)
class Retention:
    raw_days: int = 2
    aggregate_days: int = 90
    snapshot_days: int = 7
    event_days: int = 365
    audit_days: int = 365


@dataclass(frozen=True)
class Sampling:
    live_seconds: int = 2
    store_seconds: int = 30


@dataclass(frozen=True)
class Config:
    retention: Retention = field(default_factory=Retention)
    sampling: Sampling = field(default_factory=Sampling)
    bind: str = "0.0.0.0"
    port: int = 8787
    allow_remote_actions: bool = False
    token: str = ""


def _int_field(table: dict, name: str, default: int, section: str) -> int:
    value = table.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"[{section}] {name} must be an integer number of days, "
            f"got: {value!r}")
    return value


def _bool_field(table: dict, name: str, default: bool, section: str) -> bool:
    value = table.get(name, default)
    if not isinstance(value, bool):
        raise ConfigError(
            f"[{section}] {name} must be a boolean, "
            f"got: {value!r}")
    return value


def _str_field(table: dict, name: str, default: str, section: str) -> str:
    value = table.get(name, default)
    if not isinstance(value, str):
        raise ConfigError(
            f"[{section}] {name} must be a string, "
            f"got: {value!r}")
    return value


def load_config(path: Path | None = None) -> Config:
    path = DEFAULT_CONFIG_PATH if path is None else path
    data: dict = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: unreadable TOML file — {exc}") from exc

    retention_table = data.get("retention", {})
    retention = Retention(
        raw_days=_int_field(retention_table, "raw_days", Retention.raw_days, "retention"),
        aggregate_days=_int_field(
            retention_table, "aggregate_days", Retention.aggregate_days, "retention"),
        snapshot_days=_int_field(
            retention_table, "snapshot_days", Retention.snapshot_days, "retention"),
        event_days=_int_field(retention_table, "event_days", Retention.event_days, "retention"),
        audit_days=_int_field(retention_table, "audit_days", Retention.audit_days, "retention"),
    )
    sampling_table = data.get("sampling", {})
    sampling = Sampling(
        live_seconds=_int_field(
            sampling_table, "live_seconds", Sampling.live_seconds, "sampling"),
        store_seconds=_int_field(
            sampling_table, "store_seconds", Sampling.store_seconds, "sampling"),
    )
    server_table = data.get("server", {})
    cfg = Config(
        retention=retention,
        sampling=sampling,
        bind=_str_field(server_table, "bind", Config.bind, "server"),
        port=_int_field(server_table, "port", Config.port, "server"),
        allow_remote_actions=_bool_field(
            server_table, "allow_remote_actions", Config.allow_remote_actions, "server"),
        token=_str_field(server_table, "token", Config.token, "server"),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    retention, sampling = cfg.retention, cfg.sampling
    for name in ("raw_days", "aggregate_days", "snapshot_days",
                 "event_days", "audit_days"):
        if getattr(retention, name) < 1:
            raise ConfigError(
                f"[retention] {name} must be at least 1 day, "
                f"got: {getattr(retention, name)}")
    if retention.raw_days > retention.aggregate_days:
        raise ConfigError(
            f"[retention] raw_days ({retention.raw_days}) cannot exceed "
            f"aggregate_days ({retention.aggregate_days}): keeping fine-grained "
            f"samples longer than the averages makes no sense.")
    if sampling.live_seconds < 1:
        raise ConfigError(
            f"[sampling] live_seconds must be at least 1, "
            f"got: {sampling.live_seconds}")
    if (sampling.store_seconds < sampling.live_seconds
            or sampling.store_seconds % sampling.live_seconds):
        raise ConfigError(
            f"[sampling] store_seconds ({sampling.store_seconds}) must be a "
            f"multiple of live_seconds ({sampling.live_seconds})")
    if sampling.store_seconds > AGGREGATE_PERIOD_SECONDS:
        raise ConfigError(
            f"[sampling] store_seconds ({sampling.store_seconds}) cannot exceed "
            f"{AGGREGATE_PERIOD_SECONDS} seconds")
    if not 1 <= cfg.port <= 65535:
        raise ConfigError(f"[server] port out of range: {cfg.port}")


def estimate_db_bytes(cfg: Config, n_metrics: int) -> int:
    """Projected database size in bytes for this many metrics."""
    raw_rows_per_day = SECONDS_PER_DAY / cfg.sampling.store_seconds * n_metrics
    agg_rows_per_day = SECONDS_PER_DAY / AGGREGATE_PERIOD_SECONDS * n_metrics
    return int(
        cfg.retention.raw_days * raw_rows_per_day * BYTES_PER_METRIC_ROW
        + cfg.retention.aggregate_days * agg_rows_per_day * BYTES_PER_METRIC_ROW
        + OVERHEAD_BYTES)
```

- [ ] **Step 5: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 6: Commit**

```bash
git add healthconsole tests run-tests
git commit -m "Add project skeleton and validated configuration

Retention periods are expressed in days and validated at startup:
positive integer, raw/aggregate coherence, write cadence a multiple of
the display cadence. An invalid value stops the service and names the
offending field.

estimate_db_bytes prices the database the settings imply, so the cost
is announced rather than suffered."
```

---

### Task 2: Sensor plausibility checking

**Files:**
- Create: `healthconsole/plausibility.py`
- Create: `tests/test_plausibility.py`

**Interfaces:**
- Consumes: nothing
- Produces: `RANGES: dict[str, tuple[float, float]]`, `is_plausible(kind: str, value: float | None) -> bool`, `sane(kind: str, value: float | None) -> float | None`, `battery_capacity_is_coherent(now: float, full: float, design: float) -> bool`

- [ ] **Step 1: Write the failing tests**

File `tests/test_plausibility.py`:

```python
import unittest

from healthconsole.plausibility import (
    battery_capacity_is_coherent, is_plausible, sane,
)


class TestRanges(unittest.TestCase):
    def test_percent_domain(self):
        self.assertTrue(is_plausible("percent", 0))
        self.assertTrue(is_plausible("percent", 100))
        self.assertFalse(is_plausible("percent", -1))
        self.assertFalse(is_plausible("percent", 46700))

    def test_temperature_domain(self):
        self.assertTrue(is_plausible("celsius", 77.0))
        self.assertFalse(is_plausible("celsius", -273.0))
        self.assertFalse(is_plausible("celsius", 200.0))

    def test_frequency_domain_in_hertz(self):
        self.assertTrue(is_plausible("hertz", 3_400_000_000))
        self.assertFalse(is_plausible("hertz", 50_000_000))

    def test_none_is_never_plausible(self):
        self.assertFalse(is_plausible("percent", None))

    def test_unknown_kind_is_refused_rather_than_assumed_valid(self):
        self.assertFalse(is_plausible("unknown", 42))


class TestSane(unittest.TestCase):
    def test_returns_value_when_plausible(self):
        self.assertEqual(sane("percent", 42.0), 42.0)

    def test_returns_none_when_implausible(self):
        self.assertIsNone(sane("percent", 46700))


class TestBatteryCoherence(unittest.TestCase):
    def test_healthy_battery_is_coherent(self):
        self.assertTrue(battery_capacity_is_coherent(
            now=3_000_000, full=4_000_000, design=5_000_000))

    def test_slightly_over_full_is_tolerated(self):
        self.assertTrue(battery_capacity_is_coherent(
            now=4_100_000, full=4_000_000, design=5_000_000))

    def test_this_machine_reports_nonsense(self):
        # Real values from the target HP laptop: charge_now is 467 times
        # charge_full. The driver lies; we must detect it.
        self.assertFalse(battery_capacity_is_coherent(
            now=467_000, full=1_000, design=1_000))

    def test_full_above_design_is_incoherent(self):
        self.assertFalse(battery_capacity_is_coherent(
            now=1_000, full=6_000_000, design=5_000_000))

    def test_zero_design_is_incoherent(self):
        self.assertFalse(battery_capacity_is_coherent(
            now=1_000, full=1_000, design=0))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.plausibility'`

- [ ] **Step 3: Write the implementation**

File `healthconsole/plausibility.py`:

```python
"""Hardware lies. Not maliciously: sloppy drivers, careless ACPI firmware,
units that differ by vendor.

Displaying a wrong number with confidence is worse than admitting ignorance —
it is what destroys trust in a diagnostic tool. Every value passes through here
before being displayed or stored.
"""

from __future__ import annotations

# Validity domains. An unknown kind is refused rather than assumed valid:
# forgetting to declare a domain must not open a silent door.
RANGES: dict[str, tuple[float, float]] = {
    "percent": (0.0, 100.0),
    "celsius": (-20.0, 125.0),
    "hertz": (100_000_000.0, 10_000_000_000.0),
    "bytes": (0.0, 1e15),
    "bytes_per_second": (0.0, 1e12),
    "seconds": (0.0, 1e10),
    "count": (0.0, 1e9),
    "load": (0.0, 1024.0),
}

# A driver may report slightly more than full capacity right after a complete
# charge. Five percent of tolerance, no more.
CAPACITY_TOLERANCE = 1.05


def is_plausible(kind: str, value: float | None) -> bool:
    if value is None:
        return False
    bounds = RANGES.get(kind)
    if bounds is None:
        return False
    low, high = bounds
    return low <= value <= high


def sane(kind: str, value: float | None) -> float | None:
    """The value if credible, otherwise None. Never a fallback to zero:
    a zero reads as a measurement, whereas None reads as an absence."""
    return value if is_plausible(kind, value) else None


def battery_capacity_is_coherent(now: float, full: float, design: float) -> bool:
    """Cross-check the three battery capacities.

    On the target machine the driver reports charge_now = 467000 against
    charge_full = 1000. Without this check the console would display
    "0 % wear, charged to 46,700 %".
    """
    if design <= 0 or full <= 0 or now < 0:
        return False
    if full > design * CAPACITY_TOLERANCE:
        return False
    if now > full * CAPACITY_TOLERANCE:
        return False
    return True
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 5: Commit**

```bash
git add healthconsole/plausibility.py tests/test_plausibility.py
git commit -m "Add sensor plausibility checking

Every value passes a validity domain before being displayed or stored.
An undeclared quantity kind is refused rather than assumed valid.

The battery capacity cross-check detects the target machine's real
case, whose driver reports a charge 467 times its full capacity."
```

---

### Task 3: Findings, thresholds and explainable score

**Files:**
- Create: `healthconsole/findings.py`, `healthconsole/rules.py`, `healthconsole/verdict.py`
- Create: `tests/test_verdict.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Severity` (`OK`/`INFO`/`ATTENTION`/`URGENT`), `FINDING_PARAMS: dict[str, frozenset[str]]`, `FINDING_IDS: frozenset[str]`, `Finding(id, severity, params, detail, action=None)` (frozen), `UnknownFindingId(ValueError)`, `UnknownFindingParam(ValueError)`, `PENALTIES: dict[Severity, int]`, `score(findings) -> int`, `score_breakdown(findings) -> list[tuple[str, int]]`, `worst(findings) -> Severity`, and in `rules.py` the threshold constants `DISK_*`, `CPU_TEMP_*`, `CPU_USAGE_*`, `MEM_*`, `BATTERY_*`, `LOAD_*`, `PENALTY_*`

**Why findings carry no prose:** a probe that builds the sentence "The battery has lost 35 % of its capacity" cannot be translated without duplicating the probe. `Finding` therefore carries an id and parameters; the browser composes the sentence from the active catalogue (Task 13).

- [ ] **Step 1: Write the failing tests**

File `tests/test_verdict.py`:

```python
import unittest

from healthconsole.findings import (
    FINDING_IDS, FINDING_PARAMS, Finding, Severity, UnknownFindingId,
    UnknownFindingParam,
)
from healthconsole.verdict import score, score_breakdown, worst


def make(finding_id, severity):
    return Finding(id=finding_id, severity=severity, params={}, detail="raw")


class TestFindingShape(unittest.TestCase):
    def test_known_id_is_accepted(self):
        self.assertIn("cpu.usage_high", FINDING_IDS)
        make("cpu.usage_high", Severity.INFO)

    def test_unknown_id_is_refused(self):
        # A finding whose id has no catalogue entry would render as a blank
        # card. Refusing it here turns that into a loud failure.
        with self.assertRaises(UnknownFindingId):
            make("cpu.invented", Severity.INFO)

    def test_finding_is_immutable(self):
        finding = make("cpu.usage_high", Severity.INFO)
        with self.assertRaises(Exception):
            finding.severity = Severity.URGENT

    def test_no_prose_fields_exist(self):
        # Wording lives in the catalogues, never on the finding.
        finding = make("cpu.usage_high", Severity.INFO)
        for forbidden in ("title", "why", "titre", "pourquoi", "message"):
            self.assertFalse(hasattr(finding, forbidden),
                             f"{forbidden} must not exist on Finding")

    def test_params_carry_numbers_for_the_catalogue(self):
        finding = Finding(id="battery.wear", severity=Severity.ATTENTION,
                          params={"wear_pct": 35.2}, detail="raw")
        self.assertEqual(finding.params["wear_pct"], 35.2)

    def test_undeclared_param_is_refused(self):
        # A catalogue template can only interpolate what the probe emits. If a
        # probe invents a parameter, the template that would use it was never
        # written, so the value would be silently dropped.
        with self.assertRaises(UnknownFindingParam):
            Finding(id="battery.wear", severity=Severity.ATTENTION,
                    params={"wear": 35.2}, detail="raw")

    def test_finding_ids_are_exactly_the_registry_keys(self):
        self.assertEqual(FINDING_IDS, frozenset(FINDING_PARAMS))

    def test_every_declared_id_has_a_param_set(self):
        for finding_id, params in FINDING_PARAMS.items():
            self.assertIsInstance(params, frozenset, finding_id)


class TestScore(unittest.TestCase):
    def test_no_finding_is_exactly_100(self):
        # Not 94 "to look serious": an unexplained score is a lie.
        self.assertEqual(score([]), 100)

    def test_ok_findings_cost_nothing(self):
        self.assertEqual(score([make("cpu.usage_high", Severity.OK)]), 100)

    def test_info_costs_two(self):
        self.assertEqual(score([make("cpu.usage_high", Severity.INFO)]), 98)

    def test_attention_costs_eight(self):
        self.assertEqual(score([make("cpu.usage_high", Severity.ATTENTION)]), 92)

    def test_urgent_costs_twentyfive(self):
        self.assertEqual(score([make("cpu.usage_high", Severity.URGENT)]), 75)

    def test_penalties_accumulate(self):
        findings = [make("cpu.usage_high", Severity.URGENT),
                    make("memory.pressure", Severity.ATTENTION),
                    make("battery.wear", Severity.INFO)]
        self.assertEqual(score(findings), 100 - 25 - 8 - 2)

    def test_score_never_goes_below_zero(self):
        findings = [make(i, Severity.URGENT) for i in sorted(FINDING_IDS)]
        findings = findings * 10
        self.assertEqual(score(findings), 0)


class TestBreakdown(unittest.TestCase):
    def test_every_lost_point_is_traceable(self):
        findings = [make("thermal.high", Severity.ATTENTION),
                    make("battery.wear", Severity.INFO)]
        detail = score_breakdown(findings)
        self.assertEqual(detail, [("thermal.high", 8), ("battery.wear", 2)])
        self.assertEqual(100 - sum(points for _, points in detail),
                         score(findings))

    def test_breakdown_is_empty_when_perfect(self):
        self.assertEqual(score_breakdown([]), [])

    def test_breakdown_is_sorted_by_cost(self):
        findings = [make("battery.wear", Severity.INFO),
                    make("thermal.critical", Severity.URGENT)]
        self.assertEqual([fid for fid, _ in score_breakdown(findings)],
                         ["thermal.critical", "battery.wear"])


class TestWorst(unittest.TestCase):
    def test_worst_of_nothing_is_ok(self):
        self.assertIs(worst([]), Severity.OK)

    def test_worst_wins_over_milder(self):
        findings = [make("battery.wear", Severity.INFO),
                    make("thermal.critical", Severity.URGENT)]
        self.assertIs(worst(findings), Severity.URGENT)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.findings'`

- [ ] **Step 3: Write `healthconsole/findings.py`**

```python
"""The finding: unit of interpretation.

A finding carries an identifier and parameters, never a sentence. Baking prose
into a probe would make the console untranslatable without duplicating every
probe, so the wording lives in the message catalogues and the id selects it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Severity(IntEnum):
    OK = 0
    INFO = 1
    ATTENTION = 2
    URGENT = 3


class UnknownFindingId(ValueError):
    """A finding id with no catalogue entry would render as a blank card."""


class UnknownFindingParam(ValueError):
    """A parameter no catalogue template can interpolate."""


# Every id the code may emit, with the parameters its templates may use.
# This registry is what makes the catalogues testable: each locale must
# translate exactly these ids, using only these placeholders.
FINDING_PARAMS: dict[str, frozenset[str]] = {
    "cpu.usage_high": frozenset({"usage_pct", "sustain_minutes"}),
    "memory.pressure": frozenset({"available_bytes", "available_pct",
                                  "swap_used_bytes"}),
    "thermal.high": frozenset({"temperature_c", "sustain_minutes"}),
    "thermal.critical": frozenset({"temperature_c"}),
    "battery.wear": frozenset({"wear_pct"}),
    "battery.incoherent": frozenset(),
}

FINDING_IDS: frozenset[str] = frozenset(FINDING_PARAMS)


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    severity: Severity
    # Numbers and identifiers the catalogue interpolates, formatted by Intl in
    # the active locale.
    params: dict[str, float | int | str] = field(default_factory=dict)
    # Raw technical string for Expert mode. Deliberately untranslated: it is
    # diagnostic material, and translating it would make reports harder to
    # compare.
    detail: str = ""
    # Action catalogue reference, or None.
    action: str | None = None

    def __post_init__(self) -> None:
        allowed = FINDING_PARAMS.get(self.id)
        if allowed is None:
            raise UnknownFindingId(
                f"{self.id!r} is not declared in FINDING_PARAMS; add it there "
                f"and to every catalogue under web/i18n/")
        unknown = set(self.params) - allowed
        if unknown:
            raise UnknownFindingParam(
                f"{self.id!r} emits {sorted(unknown)}, which no catalogue "
                f"template can interpolate; declare them in FINDING_PARAMS")
```

- [ ] **Step 4: Write `healthconsole/rules.py`**

```python
"""Every threshold in the project, and nothing else.

They live here so that adjusting one does not require reading the project.
Each "high" threshold opens a finding and each "clear" threshold closes it:
the gap between them is the hysteresis that prevents flapping.
"""

# Storage — spec §7.5
DISK_ATTENTION_PCT = 80.0
DISK_ATTENTION_CLEAR_PCT = 75.0
DISK_URGENT_PCT = 92.0
DISK_URGENT_FREE_BYTES = 3 * 1024 ** 3

# CPU temperature
CPU_TEMP_ATTENTION_C = 85.0
CPU_TEMP_ATTENTION_CLEAR_C = 80.0
CPU_TEMP_URGENT_C = 95.0
CPU_TEMP_SUSTAIN_SECONDS = 300

# CPU usage
CPU_USAGE_ATTENTION_PCT = 90.0
CPU_USAGE_ATTENTION_CLEAR_PCT = 70.0
CPU_USAGE_SUSTAIN_SECONDS = 300

# Memory
MEM_ATTENTION_AVAILABLE_PCT = 15.0
MEM_ATTENTION_CLEAR_PCT = 25.0
MEM_SWAP_ACTIVE_BYTES = 64 * 1024 ** 2

# Battery
BATTERY_WEAR_ATTENTION_PCT = 30.0
BATTERY_WEAR_URGENT_PCT = 50.0
BATTERY_LOW_PCT = 10.0

# System load, relative to the core count
LOAD_ATTENTION_RATIO = 1.5
LOAD_ATTENTION_CLEAR_RATIO = 1.0

# Score penalties — spec §7.2
PENALTY_INFO = 2
PENALTY_ATTENTION = 8
PENALTY_URGENT = 25
```

- [ ] **Step 5: Write `healthconsole/verdict.py`**

```python
"""Explainable score.

The score is a consequence of the findings, never an oracle. Every lost point
traces back to a named finding: this is the explicit refusal of the antivirus
magic number that nobody can explain.
"""

from __future__ import annotations

from healthconsole import rules
from healthconsole.findings import Finding, Severity

PENALTIES: dict[Severity, int] = {
    Severity.OK: 0,
    Severity.INFO: rules.PENALTY_INFO,
    Severity.ATTENTION: rules.PENALTY_ATTENTION,
    Severity.URGENT: rules.PENALTY_URGENT,
}


def score_breakdown(findings: list[Finding]) -> list[tuple[str, int]]:
    """(finding id, points removed), most expensive first."""
    detail = [(f.id, PENALTIES[f.severity]) for f in findings
              if PENALTIES[f.severity] > 0]
    detail.sort(key=lambda pair: pair[1], reverse=True)
    return detail


def score(findings: list[Finding]) -> int:
    lost = sum(points for _, points in score_breakdown(findings))
    return max(0, 100 - lost)


def worst(findings: list[Finding]) -> Severity:
    return max((f.severity for f in findings), default=Severity.OK)
```

- [ ] **Step 6: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 7: Commit**

```bash
git add healthconsole/findings.py healthconsole/rules.py healthconsole/verdict.py tests/test_verdict.py
git commit -m "Add findings, centralised thresholds and the score

A finding carries an id and parameters, never a sentence: wording lives
in the message catalogues so the console can be translated without
duplicating any probe. FINDING_PARAMS declares which parameters each
finding may emit, and constructing a finding with an unregistered id or
an undeclared parameter raises rather than rendering a blank card or
silently dropping a value.

The score starts at 100 and every lost point traces back to a named
finding through score_breakdown. No findings gives exactly 100."
```

---

### Task 4: Hysteresis and event tracking

**Files:**
- Modify: `healthconsole/verdict.py` (append at end of file)
- Create: `tests/test_hysteresis.py`

**Interfaces:**
- Consumes: nothing beyond `verdict.py` itself
- Produces: `HysteresisTracker` with `update(key: str, value: float | None, high: float, low: float, now: float, sustain_seconds: float = 0.0) -> bool`, `is_open(key) -> bool`, `opened_at(key) -> float | None`, `snapshot() -> dict[str, float]`

- [ ] **Step 1: Write the failing tests**

File `tests/test_hysteresis.py`:

```python
import unittest

from healthconsole.verdict import HysteresisTracker


class TestFlapping(unittest.TestCase):
    def setUp(self):
        self.tracker = HysteresisTracker()

    def test_brief_spike_does_not_open(self):
        # A compile loading the CPU for three seconds must not turn the
        # console red; it would never be trusted again.
        self.assertFalse(self.tracker.update(
            "cpu", 95, 90, 70, now=0, sustain_seconds=300))
        self.assertFalse(self.tracker.update(
            "cpu", 95, 90, 70, now=3, sustain_seconds=300))
        self.assertFalse(self.tracker.update(
            "cpu", 10, 90, 70, now=4, sustain_seconds=300))

    def test_sustained_breach_opens(self):
        self.assertFalse(self.tracker.update(
            "cpu", 95, 90, 70, now=0, sustain_seconds=300))
        self.assertFalse(self.tracker.update(
            "cpu", 95, 90, 70, now=299, sustain_seconds=300))
        self.assertTrue(self.tracker.update(
            "cpu", 95, 90, 70, now=300, sustain_seconds=300))

    def test_without_sustain_opens_immediately(self):
        self.assertTrue(self.tracker.update("disk", 85, 80, 75, now=0))

    def test_stays_open_between_low_and_high(self):
        self.tracker.update("disk", 85, 80, 75, now=0)
        self.assertTrue(self.tracker.update("disk", 78, 80, 75, now=10))

    def test_closes_only_below_low(self):
        self.tracker.update("disk", 85, 80, 75, now=0)
        self.assertFalse(self.tracker.update("disk", 74, 80, 75, now=10))

    def test_stays_closed_between_low_and_high(self):
        self.assertFalse(self.tracker.update("disk", 78, 80, 75, now=0))

    def test_sustain_timer_resets_when_value_drops(self):
        self.tracker.update("cpu", 95, 90, 70, now=0, sustain_seconds=300)
        self.tracker.update("cpu", 50, 90, 70, now=100, sustain_seconds=300)
        self.tracker.update("cpu", 95, 90, 70, now=200, sustain_seconds=300)
        self.assertFalse(self.tracker.update(
            "cpu", 95, 90, 70, now=450, sustain_seconds=300))
        self.assertTrue(self.tracker.update(
            "cpu", 95, 90, 70, now=500, sustain_seconds=300))


class TestUnavailableValues(unittest.TestCase):
    def test_none_never_opens(self):
        tracker = HysteresisTracker()
        self.assertFalse(tracker.update("bat", None, 30, 25, now=0))

    def test_none_does_not_close_an_open_state(self):
        # Losing the measurement is not proof the problem went away.
        tracker = HysteresisTracker()
        tracker.update("bat", 40, 30, 25, now=0)
        self.assertTrue(tracker.update("bat", None, 30, 25, now=10))


class TestIntrospection(unittest.TestCase):
    def test_opened_at_records_the_moment(self):
        tracker = HysteresisTracker()
        tracker.update("disk", 85, 80, 75, now=1234)
        self.assertEqual(tracker.opened_at("disk"), 1234)

    def test_opened_at_is_none_when_closed(self):
        self.assertIsNone(HysteresisTracker().opened_at("disk"))

    def test_snapshot_lists_open_keys_with_their_start(self):
        tracker = HysteresisTracker()
        tracker.update("disk", 85, 80, 75, now=100)
        tracker.update("cpu", 10, 90, 70, now=100)
        self.assertEqual(tracker.snapshot(), {"disk": 100})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `ImportError: cannot import name 'HysteresisTracker'`

- [ ] **Step 3: Append the implementation to `healthconsole/verdict.py`**

```python
class HysteresisTracker:
    """Prevents findings from flapping.

    A threshold must be crossed *and held* to open a finding, and the value
    must fall below a distinct lower threshold to close it. Without this, a
    three-second load spike would turn the console red and nobody would
    believe it again.
    """

    def __init__(self) -> None:
        self._open: dict[str, float] = {}           # key -> opening instant
        self._breach_since: dict[str, float] = {}   # key -> breach start

    def update(self, key: str, value: float | None, high: float, low: float,
               now: float, sustain_seconds: float = 0.0) -> bool:
        if value is None:
            # Losing the measurement is not proof the problem went away: keep
            # the state, and never open one on an absence.
            return key in self._open

        if key in self._open:
            if value < low:
                del self._open[key]
                self._breach_since.pop(key, None)
            return key in self._open

        if value >= high:
            since = self._breach_since.setdefault(key, now)
            if now - since >= sustain_seconds:
                self._open[key] = now
                self._breach_since.pop(key, None)
        else:
            self._breach_since.pop(key, None)
        return key in self._open

    def is_open(self, key: str) -> bool:
        return key in self._open

    def opened_at(self, key: str) -> float | None:
        return self._open.get(key)

    def snapshot(self) -> dict[str, float]:
        return dict(self._open)
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 5: Commit**

```bash
git add healthconsole/verdict.py tests/test_hysteresis.py
git commit -m "Add hysteresis to stop findings from flapping

A threshold must be crossed and held to open a finding, and the value
must fall below a distinct lower threshold to close it.

A missing measurement can neither open nor close a finding: losing the
measurement is not proof the problem went away."
```

---

### Task 5: In-memory ring buffer

**Files:**
- Create: `healthconsole/ring.py`
- Create: `tests/test_ring.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Ring(window_seconds: int = 3600, live_seconds: int = 2)` with `push(key, value, ts) -> None`, `series(key) -> list[tuple[float, float]]`, `aggregate(key, since) -> tuple[float, float, float] | None`, `keys() -> list[str]`, `capacity: int`

- [ ] **Step 1: Write the failing tests**

File `tests/test_ring.py`:

```python
import unittest

from healthconsole.ring import Ring


class TestCapacity(unittest.TestCase):
    def test_capacity_matches_window_over_resolution(self):
        self.assertEqual(Ring(window_seconds=3600, live_seconds=2).capacity, 1800)

    def test_oldest_points_are_dropped(self):
        ring = Ring(window_seconds=10, live_seconds=2)  # 5 points
        for i in range(8):
            ring.push("cpu", float(i), ts=float(i))
        self.assertEqual([v for _, v in ring.series("cpu")], [3, 4, 5, 6, 7])

    def test_rejects_zero_resolution(self):
        with self.assertRaises(ValueError):
            Ring(window_seconds=3600, live_seconds=0)


class TestSeries(unittest.TestCase):
    def test_unknown_key_yields_empty_series(self):
        self.assertEqual(Ring().series("nonexistent"), [])

    def test_keys_lists_what_was_pushed(self):
        ring = Ring()
        ring.push("cpu", 1.0, ts=0)
        ring.push("mem", 2.0, ts=0)
        self.assertEqual(sorted(ring.keys()), ["cpu", "mem"])


class TestAggregate(unittest.TestCase):
    def test_returns_avg_min_max(self):
        ring = Ring()
        for i, value in enumerate([10.0, 20.0, 30.0]):
            ring.push("cpu", value, ts=float(i))
        self.assertEqual(ring.aggregate("cpu", since=0.0), (20.0, 10.0, 30.0))

    def test_only_considers_points_since(self):
        ring = Ring()
        for i, value in enumerate([10.0, 20.0, 30.0]):
            ring.push("cpu", value, ts=float(i))
        self.assertEqual(ring.aggregate("cpu", since=1.0), (25.0, 20.0, 30.0))

    def test_empty_window_returns_none(self):
        # None, not zero: a zero would read as a measurement.
        ring = Ring()
        ring.push("cpu", 10.0, ts=0.0)
        self.assertIsNone(ring.aggregate("cpu", since=100.0))

    def test_unknown_key_returns_none(self):
        self.assertIsNone(Ring().aggregate("nonexistent", since=0.0))


class TestMemoryFootprint(unittest.TestCase):
    def test_stays_bounded_under_sustained_push(self):
        ring = Ring(window_seconds=3600, live_seconds=2)
        for i in range(50_000):
            ring.push("cpu", float(i), ts=float(i))
        self.assertEqual(len(ring.series("cpu")), 1800)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.ring'`

- [ ] **Step 3: Write `healthconsole/ring.py`**

```python
"""In-memory ring buffer covering the last 60 minutes.

Displaying finely and keeping long are two distinct needs, and conflating them
makes the database explode. The 2-second live view lives here — roughly 360 KiB
for 25 metrics — and only a value aggregated every 30 seconds reaches the disk.
"""

from __future__ import annotations

from collections import deque


class Ring:
    def __init__(self, window_seconds: int = 3600, live_seconds: int = 2) -> None:
        if live_seconds < 1:
            raise ValueError("live_seconds must be at least 1")
        self.window_seconds = window_seconds
        self.live_seconds = live_seconds
        self.capacity = max(1, window_seconds // live_seconds)
        self._data: dict[str, deque[tuple[float, float]]] = {}

    def push(self, key: str, value: float, ts: float) -> None:
        buffer = self._data.get(key)
        if buffer is None:
            buffer = self._data[key] = deque(maxlen=self.capacity)
        buffer.append((ts, value))

    def series(self, key: str) -> list[tuple[float, float]]:
        return list(self._data.get(key, ()))

    def keys(self) -> list[str]:
        return list(self._data)

    def aggregate(self, key: str, since: float
                  ) -> tuple[float, float, float] | None:
        """(average, minimum, maximum) since `since`, or None if no point.

        None rather than zero: a zero would read as a real measurement.
        """
        buffer = self._data.get(key)
        if not buffer:
            return None
        values = [value for ts, value in buffer if ts >= since]
        if not values:
            return None
        return (sum(values) / len(values), min(values), max(values))
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 5: Commit**

```bash
git add healthconsole/ring.py tests/test_ring.py
git commit -m "Add the in-memory ring buffer for the live view

The 2-second live view lives in memory over a 60-minute window and
never reaches the disk. That is what lets us serve sparklines without
writing 80 MB per day.

Aggregating an empty window returns None rather than zero: a zero would
read as a measurement."
```

---

### Task 6: Database — schema, writes, reads

**Files:**
- Create: `healthconsole/store.py`
- Create: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Store(path)` with `close()`, `key_id(key) -> int`, `write_metrics(ts: int, rows: list[tuple[str, float, float, float]]) -> None`, `read_series(key, since, until, table="metric") -> list[tuple[int, float]]`, `count_rows(table) -> int`, `distinct_metric_count() -> int`, `oldest_ts(table) -> int | None`, `db_bytes() -> int`, attribute `conn`

- [ ] **Step 1: Write the failing tests**

File `tests/test_store.py`:

```python
import unittest

from healthconsole.store import Store


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()


class TestSchema(StoreCase):
    def test_tables_exist(self):
        names = {row[0] for row in self.store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual(
            {"metric_key", "metric", "metric_5m", "snapshot", "event",
             "action_run"}, names)

    def test_creating_the_schema_again_is_idempotent(self):
        self.store._create_schema()
        self.assertEqual(self.store.count_rows("metric"), 0)


class TestKeyNormalisation(StoreCase):
    def test_same_key_yields_same_id(self):
        self.assertEqual(self.store.key_id("cpu.usage"),
                         self.store.key_id("cpu.usage"))

    def test_different_keys_yield_different_ids(self):
        self.assertNotEqual(self.store.key_id("cpu.usage"),
                            self.store.key_id("mem.used"))

    def test_ids_are_cached_without_extra_rows(self):
        for _ in range(10):
            self.store.key_id("cpu.usage")
        self.assertEqual(self.store.count_rows("metric_key"), 1)


class TestWriteRead(StoreCase):
    def test_written_points_come_back(self):
        self.store.write_metrics(1000, [("cpu.usage", 20.0, 10.0, 30.0)])
        self.assertEqual(self.store.read_series("cpu.usage", 0, 2000),
                         [(1000, 20.0)])

    def test_range_is_inclusive_and_bounded(self):
        for ts in (100, 200, 300):
            self.store.write_metrics(ts, [("cpu.usage", float(ts), 0.0, 0.0)])
        self.assertEqual(
            [ts for ts, _ in self.store.read_series("cpu.usage", 200, 300)],
            [200, 300])

    def test_results_are_ordered_by_time(self):
        for ts in (300, 100, 200):
            self.store.write_metrics(ts, [("cpu.usage", float(ts), 0.0, 0.0)])
        self.assertEqual(
            [ts for ts, _ in self.store.read_series("cpu.usage", 0, 999)],
            [100, 200, 300])

    def test_unknown_key_yields_empty_series(self):
        self.assertEqual(self.store.read_series("nonexistent", 0, 999), [])

    def test_unknown_table_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.read_series("cpu.usage", 0, 999, table="sqlite_master")

    def test_batch_write_is_one_transaction(self):
        self.store.write_metrics(1000, [
            ("cpu.usage", 1.0, 1.0, 1.0),
            ("mem.available", 2.0, 2.0, 2.0)])
        self.assertEqual(self.store.count_rows("metric"), 2)

    def test_empty_batch_writes_nothing(self):
        self.store.write_metrics(1000, [])
        self.assertEqual(self.store.count_rows("metric"), 0)


class TestIntrospection(StoreCase):
    def test_distinct_metric_count(self):
        self.store.write_metrics(1, [("a", 1.0, 1.0, 1.0), ("b", 1.0, 1.0, 1.0)])
        self.store.write_metrics(2, [("a", 1.0, 1.0, 1.0)])
        self.assertEqual(self.store.distinct_metric_count(), 2)

    def test_oldest_ts_is_none_when_empty(self):
        self.assertIsNone(self.store.oldest_ts("metric"))

    def test_oldest_ts_reports_the_first_point(self):
        self.store.write_metrics(500, [("a", 1.0, 1.0, 1.0)])
        self.store.write_metrics(100, [("a", 1.0, 1.0, 1.0)])
        self.assertEqual(self.store.oldest_ts("metric"), 100)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.store'`

- [ ] **Step 3: Write `healthconsole/store.py`**

```python
"""SQLite persistence.

Metric keys are normalised to integers: storing the string
"net.enp0s25.rx_bps" on every row would cost more than the measurement itself.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_key (
    id  INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS metric (
    ts INTEGER NOT NULL, key_id INTEGER NOT NULL,
    avg REAL NOT NULL, min REAL NOT NULL, max REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_5m (
    ts INTEGER NOT NULL, key_id INTEGER NOT NULL,
    avg REAL NOT NULL, min REAL NOT NULL, max REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot (
    ts INTEGER NOT NULL, probe TEXT NOT NULL, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY, finding_id TEXT NOT NULL, severity TEXT NOT NULL,
    opened_ts INTEGER NOT NULL, closed_ts INTEGER
);
CREATE TABLE IF NOT EXISTS action_run (
    id TEXT PRIMARY KEY, ts INTEGER NOT NULL, action_id TEXT NOT NULL,
    source TEXT NOT NULL, exit_code INTEGER, duration_ms INTEGER, output TEXT
);
CREATE INDEX IF NOT EXISTS metric_key_ts ON metric(key_id, ts);
CREATE INDEX IF NOT EXISTS metric_5m_key_ts ON metric_5m(key_id, ts);
CREATE INDEX IF NOT EXISTS snapshot_ts ON snapshot(ts);
"""

METRIC_TABLES = ("metric", "metric_5m")
ALL_TABLES = METRIC_TABLES + ("metric_key", "snapshot", "event", "action_run")


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._key_cache: dict[str, int] = {}
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def key_id(self, key: str) -> int:
        cached = self._key_cache.get(key)
        if cached is not None:
            return cached
        row = self.conn.execute(
            "SELECT id FROM metric_key WHERE key = ?", (key,)).fetchone()
        if row is None:
            cursor = self.conn.execute(
                "INSERT INTO metric_key(key) VALUES (?)", (key,))
            self.conn.commit()
            key_id = int(cursor.lastrowid)
        else:
            key_id = int(row[0])
        self._key_cache[key] = key_id
        return key_id

    def write_metrics(self, ts: int,
                      rows: list[tuple[str, float, float, float]]) -> None:
        if not rows:
            return
        payload = [(ts, self.key_id(key), avg, low, high)
                   for key, avg, low, high in rows]
        self.conn.executemany(
            "INSERT INTO metric(ts, key_id, avg, min, max) VALUES (?,?,?,?,?)",
            payload)
        self.conn.commit()

    def read_series(self, key: str, since: int, until: int,
                    table: str = "metric") -> list[tuple[int, float]]:
        if table not in METRIC_TABLES:
            raise ValueError(f"unknown table: {table}")
        cursor = self.conn.execute(
            f"SELECT ts, avg FROM {table} "
            "WHERE key_id = (SELECT id FROM metric_key WHERE key = ?) "
            "AND ts BETWEEN ? AND ? ORDER BY ts",
            (key, since, until))
        return [(int(ts), float(value)) for ts, value in cursor.fetchall()]

    def count_rows(self, table: str) -> int:
        if table not in ALL_TABLES:
            raise ValueError(f"unknown table: {table}")
        return int(self.conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def distinct_metric_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(DISTINCT key_id) FROM metric").fetchone()[0])

    def oldest_ts(self, table: str) -> int | None:
        if table not in METRIC_TABLES:
            raise ValueError(f"unknown table: {table}")
        row = self.conn.execute(f"SELECT MIN(ts) FROM {table}").fetchone()
        return None if row[0] is None else int(row[0])

    def db_bytes(self) -> int:
        if self.path == ":memory:":
            pages = self.conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
            return int(pages) * int(page_size)
        return sum(
            Path(self.path + suffix).stat().st_size
            for suffix in ("", "-wal", "-shm")
            if Path(self.path + suffix).exists())
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 5: Commit**

```bash
git add healthconsole/store.py tests/test_store.py
git commit -m "Add the SQLite schema and metric writes

Metric keys are normalised to integers through metric_key: storing the
string on every row would cost more than the measurement.

WAL journal, synchronous NORMAL, idempotent schema, and table names
validated against an allow-list before interpolation."
```

---

### Task 7: Database — aggregation, configurable retention, pruning

**Files:**
- Modify: `healthconsole/store.py` (append methods to `Store`)
- Create: `tests/test_retention.py`

**Interfaces:**
- Consumes: `healthconsole.config.Config`, `Store` from Task 6
- Produces: on `Store` — `aggregate_5m(now: int, period: int = 300) -> int`, `prune(cfg: Config, now: int) -> dict[str, int]`, `available_depth_seconds(table: str, now: int) -> int`, `vacuum() -> None`

- [ ] **Step 1: Write the failing tests**

File `tests/test_retention.py`:

```python
import unittest

from healthconsole.config import Config, Retention
from healthconsole.store import Store

DAY = 86_400


class RetentionCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def fill_days(self, days, now, key="cpu.usage", step=3600):
        """One point per hour over `days` days, ending at `now`."""
        ts = now - days * DAY
        while ts <= now:
            self.store.write_metrics(ts, [(key, 50.0, 40.0, 60.0)])
            ts += step


class TestAggregation(RetentionCase):
    def test_raw_points_become_five_minute_buckets(self):
        now = 10 * DAY
        for i in range(10):
            self.store.write_metrics(
                now + i * 30, [("cpu.usage", float(i), 0.0, 9.0)])
        written = self.store.aggregate_5m(now=now + 600)
        self.assertGreater(written, 0)
        rows = self.store.read_series(
            "cpu.usage", 0, now + 600, table="metric_5m")
        self.assertEqual(len(rows), 1)

    def test_bucket_average_is_correct(self):
        now = 10 * DAY
        for i, value in enumerate([10.0, 20.0, 30.0]):
            self.store.write_metrics(
                now + i * 30, [("cpu.usage", value, value, value)])
        self.store.aggregate_5m(now=now + 600)
        rows = self.store.read_series(
            "cpu.usage", 0, now + 600, table="metric_5m")
        self.assertAlmostEqual(rows[0][1], 20.0)

    def test_aggregation_is_idempotent(self):
        now = 10 * DAY
        for i in range(6):
            self.store.write_metrics(now + i * 30, [("cpu.usage", 1.0, 1.0, 1.0)])
        self.store.aggregate_5m(now=now + 600)
        before = self.store.count_rows("metric_5m")
        self.store.aggregate_5m(now=now + 600)
        self.assertEqual(self.store.count_rows("metric_5m"), before)


class TestPrune(RetentionCase):
    def test_raw_older_than_raw_days_is_removed(self):
        now = 100 * DAY
        self.fill_days(10, now)
        self.store.prune(Config(retention=Retention(raw_days=2)), now=now)
        self.assertGreaterEqual(self.store.oldest_ts("metric"), now - 2 * DAY)

    def test_prune_reports_what_it_deleted(self):
        now = 100 * DAY
        self.fill_days(10, now)
        deleted = self.store.prune(
            Config(retention=Retention(raw_days=2)), now=now)
        self.assertIn("metric", deleted)
        self.assertGreater(deleted["metric"], 0)

    def test_reducing_retention_purges_more(self):
        now = 100 * DAY
        self.fill_days(30, now)
        self.store.prune(Config(retention=Retention(raw_days=20)), now=now)
        after_twenty = self.store.count_rows("metric")
        self.store.prune(Config(retention=Retention(raw_days=2)), now=now)
        self.assertLess(self.store.count_rows("metric"), after_twenty)

    def test_increasing_retention_resurrects_nothing(self):
        # History restarts from the date of the change. The console must show
        # the depth actually available, never the one requested.
        now = 100 * DAY
        self.fill_days(30, now)
        self.store.prune(Config(retention=Retention(raw_days=2)), now=now)
        remaining = self.store.count_rows("metric")
        self.store.prune(Config(retention=Retention(raw_days=90)), now=now)
        self.assertEqual(self.store.count_rows("metric"), remaining)

    def test_nothing_is_deleted_when_everything_is_recent(self):
        now = 100 * DAY
        self.fill_days(1, now)
        deleted = self.store.prune(
            Config(retention=Retention(raw_days=30)), now=now)
        self.assertEqual(deleted["metric"], 0)


class TestAvailableDepth(RetentionCase):
    def test_depth_is_zero_when_empty(self):
        self.assertEqual(
            self.store.available_depth_seconds("metric", now=DAY), 0)

    def test_depth_reflects_what_is_actually_stored(self):
        now = 100 * DAY
        self.fill_days(3, now)
        depth = self.store.available_depth_seconds("metric", now=now)
        self.assertAlmostEqual(depth / DAY, 3.0, places=1)

    def test_depth_shrinks_after_prune(self):
        now = 100 * DAY
        self.fill_days(30, now)
        self.store.prune(Config(retention=Retention(raw_days=2)), now=now)
        self.assertLessEqual(
            self.store.available_depth_seconds("metric", now=now),
            2 * DAY + 3600)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'aggregate_5m'`

- [ ] **Step 3: Append the methods to the `Store` class**

```python
    # --- Aggregation and retention ------------------------------------

    def aggregate_5m(self, now: int, period: int = 300) -> int:
        """Fold raw points into buckets of `period` seconds.

        Idempotent: a bucket already written is not written again. Only fully
        elapsed buckets are processed, so no partial average is frozen in.
        """
        boundary = (now // period) * period
        rows = self.conn.execute(
            "SELECT (ts / ?) * ? AS bucket, key_id, AVG(avg), MIN(min), MAX(max) "
            "FROM metric WHERE ts < ? GROUP BY bucket, key_id",
            (period, period, boundary)).fetchall()
        written = 0
        for bucket, key_id, avg, low, high in rows:
            exists = self.conn.execute(
                "SELECT 1 FROM metric_5m WHERE ts = ? AND key_id = ?",
                (bucket, key_id)).fetchone()
            if exists:
                continue
            self.conn.execute(
                "INSERT INTO metric_5m(ts, key_id, avg, min, max) "
                "VALUES (?,?,?,?,?)", (bucket, key_id, avg, low, high))
            written += 1
        self.conn.commit()
        return written

    def prune(self, cfg, now: int) -> dict[str, int]:
        """Apply the retention periods. Returns rows deleted per table.

        Raising a period resurrects nothing: deletion is permanent, and the
        console will report the depth actually available rather than the one
        requested.
        """
        day = 86_400
        cutoffs = {
            "metric": now - cfg.retention.raw_days * day,
            "metric_5m": now - cfg.retention.aggregate_days * day,
            "snapshot": now - cfg.retention.snapshot_days * day,
            "action_run": now - cfg.retention.audit_days * day,
        }
        deleted: dict[str, int] = {}
        for table, cutoff in cutoffs.items():
            cursor = self.conn.execute(
                f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            deleted[table] = max(0, cursor.rowcount)
        cursor = self.conn.execute(
            "DELETE FROM event WHERE closed_ts IS NOT NULL AND closed_ts < ?",
            (now - cfg.retention.event_days * day,))
        deleted["event"] = max(0, cursor.rowcount)
        self.conn.commit()
        return deleted

    def available_depth_seconds(self, table: str, now: int) -> int:
        oldest = self.oldest_ts(table)
        return 0 if oldest is None else max(0, now - oldest)

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")
        self.conn.commit()
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 5: Commit**

```bash
git add healthconsole/store.py tests/test_retention.py
git commit -m "Add 5-minute aggregation and retention pruning

The clock is injected into aggregate_5m and prune, so no test waits
48 hours.

The tests assert that raising a retention period resurrects nothing and
that available_depth_seconds reflects what is actually stored rather
than what the configuration asks for."
```

---

### Task 8: Probe contract, CPU and memory probes

**Files:**
- Modify: `healthconsole/probes/__init__.py`
- Create: `healthconsole/probes/cpu.py`, `healthconsole/probes/memory.py`
- Create: `tests/test_probes_cpu.py`, `tests/test_probes_memory.py`

**Interfaces:**
- Consumes: `healthconsole.findings`, `healthconsole.rules`, `healthconsole.plausibility`
- Produces: in `probes/__init__.py` — `FAST = "fast"`, `SLOW = "slow"`, `EvalContext(sustained: dict[str, bool], cores: int)`, `load_probes(cadence: str | None = None) -> list`, `PROBE_MODULES: tuple[str, ...]`, `unavailable(reason: str) -> dict`. Each probe exposes `NAME: str`, `CADENCE: str`, `collect() -> dict`, `metrics(sample: dict) -> dict[str, float]`, `evaluate(sample: dict, ctx: EvalContext) -> list[Finding]`

**Shared contract, binding on every probe in the project:**
- `collect()` measures and never judges. It always returns a dict containing
  `status`, one of `"ok"`, `"unavailable"` (with `reason`) or `"incoherent"`.
- `metrics()` extracts only the numeric values worth historising.
- `evaluate()` is **pure**: no clock, no filesystem, no subprocess. Everything it
  needs arrives through `sample` and `ctx`.
- Findings carry ids and `params`; no probe ever builds a user-facing sentence.

- [ ] **Step 1: Write the failing tests**

File `tests/test_probes_cpu.py`:

```python
import unittest

from healthconsole.findings import FINDING_IDS, Severity
from healthconsole.probes import FAST, EvalContext, load_probes
from healthconsole.probes import cpu


def context(sustained=None, cores=4):
    return EvalContext(sustained=sustained or {}, cores=cores)


class TestRegistry(unittest.TestCase):
    def test_cpu_is_registered_as_fast(self):
        self.assertIn("cpu", [module.NAME for module in load_probes(FAST)])

    def test_every_probe_honours_the_contract(self):
        for module in load_probes():
            for attribute in ("NAME", "CADENCE", "collect", "metrics", "evaluate"):
                self.assertTrue(hasattr(module, attribute),
                                f"{module.__name__} lacks {attribute}")


class TestMetrics(unittest.TestCase):
    def test_extracts_numeric_series(self):
        sample = {"status": "ok", "usage_pct": 23.0, "freq_hz": 3.4e9,
                  "load1": 0.62, "cores": 4}
        self.assertEqual(cpu.metrics(sample),
                         {"cpu.usage": 23.0, "cpu.freq": 3.4e9, "load.1": 0.62})

    def test_implausible_values_are_not_historised(self):
        sample = {"status": "ok", "usage_pct": 4700.0, "freq_hz": 3.4e9,
                  "load1": 0.62, "cores": 4}
        self.assertNotIn("cpu.usage", cpu.metrics(sample))

    def test_unavailable_sample_yields_no_metric(self):
        self.assertEqual(
            cpu.metrics({"status": "unavailable", "reason": "x"}), {})


class TestEvaluate(unittest.TestCase):
    IDLE = {"status": "ok", "usage_pct": 12.0, "freq_hz": 1.6e9,
            "load1": 0.3, "cores": 4}
    BUSY = {"status": "ok", "usage_pct": 99.0, "freq_hz": 3.4e9,
            "load1": 4.0, "cores": 4}

    def test_idle_machine_produces_no_finding(self):
        self.assertEqual(cpu.evaluate(self.IDLE, context()), [])

    def test_spike_alone_produces_no_finding(self):
        # Without the scheduler confirming the breach held, a spike must not
        # trigger anything.
        self.assertEqual(cpu.evaluate(self.BUSY, context()), [])

    def test_sustained_load_produces_attention(self):
        findings = cpu.evaluate(self.BUSY, context({"cpu.usage_high": True}))
        self.assertEqual(len(findings), 1)
        self.assertIs(findings[0].severity, Severity.ATTENTION)
        self.assertEqual(findings[0].id, "cpu.usage_high")

    def test_finding_carries_parameters_not_prose(self):
        finding = cpu.evaluate(self.BUSY, context({"cpu.usage_high": True}))[0]
        self.assertIn(finding.id, FINDING_IDS)
        self.assertIn("usage_pct", finding.params)
        self.assertEqual(finding.params["usage_pct"], 99.0)
        self.assertTrue(finding.detail)

    def test_unavailable_sample_produces_no_finding(self):
        self.assertEqual(
            cpu.evaluate({"status": "unavailable", "reason": "x"}, context()), [])

    def test_implausible_usage_produces_no_finding_even_while_sustained(self):
        # A breach sustained by earlier valid ticks does not justify emitting a
        # finding with an implausible sensor value.
        sample = {"status": "ok", "usage_pct": 4700.0, "freq_hz": 3.4e9,
                  "load1": 0.3, "cores": 4}
        self.assertEqual(
            cpu.evaluate(sample, context({"cpu.usage_high": True})), [])

    def test_sustained_load_produces_finding_with_checked_params(self):
        # Verify the normal sustained-load case still produces a finding with
        # plausibility-checked params.
        findings = cpu.evaluate(self.BUSY, context({"cpu.usage_high": True}))
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.id, "cpu.usage_high")
        self.assertEqual(finding.params["usage_pct"], 99.0)
        self.assertIn("sustain_minutes", finding.params)


class TestCollectSmoke(unittest.TestCase):
    def test_collect_returns_a_usable_sample(self):
        sample = cpu.collect()
        self.assertEqual(sample["status"], "ok")
        self.assertGreaterEqual(sample["cores"], 1)
        self.assertIsInstance(sample["usage_pct"], float)


if __name__ == "__main__":
    unittest.main()
```

File `tests/test_probes_memory.py`:

```python
import unittest

from healthconsole.findings import Severity
from healthconsole.probes import EvalContext
from healthconsole.probes import memory

GIB = 1024 ** 3


def context():
    return EvalContext(sustained={}, cores=4)


def sample(available_pct=50.0, swap_used=0):
    return {"status": "ok", "total": 5 * GIB,
            "available": int(5 * GIB * available_pct / 100),
            "available_pct": available_pct, "swap_used": swap_used,
            "swap_total": 2 * GIB}


class TestMetrics(unittest.TestCase):
    def test_extracts_series(self):
        keys = memory.metrics(sample())
        self.assertIn("mem.available", keys)
        self.assertIn("mem.available_pct", keys)
        self.assertIn("mem.swap.used", keys)


class TestEvaluate(unittest.TestCase):
    def test_comfortable_memory_is_silent(self):
        self.assertEqual(memory.evaluate(sample(50.0), context()), [])

    def test_low_memory_alone_is_silent(self):
        # Low "free" memory without active swapping is Linux behaving
        # normally: the cache fills whatever is unused. Not a defect.
        self.assertEqual(
            memory.evaluate(sample(10.0, swap_used=0), context()), [])

    def test_low_memory_with_active_swap_warns(self):
        findings = memory.evaluate(
            sample(10.0, swap_used=512 * 1024 ** 2), context())
        self.assertEqual(len(findings), 1)
        self.assertIs(findings[0].severity, Severity.ATTENTION)
        self.assertEqual(findings[0].id, "memory.pressure")

    def test_finding_carries_figures_for_the_catalogue(self):
        finding = memory.evaluate(
            sample(10.0, swap_used=512 * 1024 ** 2), context())[0]
        self.assertIn("available_bytes", finding.params)
        self.assertIn("available_pct", finding.params)

    def test_unavailable_sample_produces_no_finding(self):
        self.assertEqual(memory.evaluate(
            {"status": "unavailable", "reason": "x"}, context()), [])

    def test_implausible_available_pct_produces_no_finding_even_with_active_swap(self):
        # Implausible available_pct must not result in a finding, even if swap
        # is genuinely active. We cannot honestly claim memory pressure with
        # unchecked sensor data.
        implausible = {"status": "ok", "total": 5 * GIB,
                       "available": int(5 * GIB * 10 / 100),
                       "available_pct": 4700.0, "swap_used": 512 * 1024 ** 2,
                       "swap_total": 2 * GIB}
        self.assertEqual(memory.evaluate(implausible, context()), [])

    def test_low_memory_with_active_swap_produces_finding_with_checked_params(self):
        # Verify the normal low-memory-with-swap case still produces a finding
        # with plausibility-checked params.
        findings = memory.evaluate(
            sample(10.0, swap_used=512 * 1024 ** 2), context())
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.id, "memory.pressure")
        self.assertIn("available_bytes", finding.params)
        self.assertIn("available_pct", finding.params)
        self.assertIn("swap_used_bytes", finding.params)


class TestCollectSmoke(unittest.TestCase):
    def test_collect_returns_a_usable_sample(self):
        sample_data = memory.collect()
        self.assertEqual(sample_data["status"], "ok")
        self.assertGreater(sample_data["total"], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `ImportError: cannot import name 'EvalContext'`

- [ ] **Step 3: Write `healthconsole/probes/__init__.py`**

```python
"""Probe registry and shared contract.

A probe that fails returns an `unavailable` sample carrying its reason; it never
brings the others down. A diagnostic tool that crashes when something is wrong
is worse than useless.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from types import ModuleType

FAST = "fast"
SLOW = "slow"

# Later tasks extend this tuple as they add probe modules.
PROBE_MODULES: tuple[str, ...] = (
    "cpu", "memory",
)


@dataclass(frozen=True)
class EvalContext:
    """Everything `evaluate()` needs beyond the sample.

    Breaches confirmed over time are computed by the scheduler and passed in
    here, which keeps `evaluate()` purely functional: with no clock of its own,
    it stays testable against frozen samples.
    """

    sustained: dict[str, bool] = field(default_factory=dict)
    cores: int = 1


def load_probes(cadence: str | None = None) -> list[ModuleType]:
    modules = []
    for name in PROBE_MODULES:
        module = importlib.import_module(f"healthconsole.probes.{name}")
        if cadence is None or module.CADENCE == cadence:
            modules.append(module)
    return modules


def unavailable(reason: str) -> dict:
    """`reason` is diagnostic material in English, not user-facing prose."""
    return {"status": "unavailable", "reason": reason}
```

- [ ] **Step 4: Write `healthconsole/probes/cpu.py`**

```python
"""Processor usage, frequency and system load."""

from __future__ import annotations

import os

import psutil

from healthconsole import rules
from healthconsole.findings import Finding, Severity
from healthconsole.plausibility import sane
from healthconsole.probes import FAST, EvalContext, unavailable

NAME = "cpu"
CADENCE = FAST


def collect() -> dict:
    try:
        freq = psutil.cpu_freq()
        load1, load5, load15 = os.getloadavg()
        return {
            "status": "ok",
            "usage_pct": float(psutil.cpu_percent(interval=None)),
            "freq_hz": float(freq.current * 1e6) if freq else None,
            "load1": float(load1), "load5": float(load5), "load15": float(load15),
            "cores": psutil.cpu_count(logical=True) or 1,
        }
    except Exception as exc:                      # noqa: BLE001
        return unavailable(f"cannot read processor state: {exc}")


def metrics(sample: dict) -> dict[str, float]:
    if sample.get("status") != "ok":
        return {}
    series: dict[str, float] = {}
    for key, kind, field_name in (
        ("cpu.usage", "percent", "usage_pct"),
        ("cpu.freq", "hertz", "freq_hz"),
        ("load.1", "load", "load1"),
    ):
        value = sane(kind, sample.get(field_name))
        if value is not None:
            series[key] = value
    return series


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    if sample.get("status") != "ok":
        return []
    if not ctx.sustained.get("cpu.usage_high"):
        return []
    # Do not emit a finding with an unchecked sensor value. A breach that was
    # sustained by earlier valid ticks does not justify displaying a wrong number.
    usage = sane("percent", sample.get("usage_pct"))
    if usage is None:
        return []
    load1 = sane("load", sample.get("load1"))
    load1_str = f"{load1}" if load1 is not None else "unknown"
    return [Finding(
        id="cpu.usage_high",
        severity=Severity.ATTENTION,
        params={"usage_pct": usage,
                "sustain_minutes": rules.CPU_USAGE_SUSTAIN_SECONDS // 60},
        detail=(f"usage={usage:.0f}% sustained >= "
                f"{rules.CPU_USAGE_SUSTAIN_SECONDS}s · "
                f"load1={load1_str} over {sample.get('cores')} cores"),
    )]
```

- [ ] **Step 5: Write `healthconsole/probes/memory.py`**

```python
"""Physical memory and swap."""

from __future__ import annotations

import psutil

from healthconsole import rules
from healthconsole.findings import Finding, Severity
from healthconsole.plausibility import sane
from healthconsole.probes import FAST, EvalContext, unavailable

NAME = "memory"
CADENCE = FAST
GIB = 1024 ** 3


def collect() -> dict:
    try:
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            "status": "ok",
            "total": int(virtual.total), "available": int(virtual.available),
            "available_pct": float(virtual.available) / float(virtual.total) * 100.0,
            "swap_used": int(swap.used), "swap_total": int(swap.total),
        }
    except Exception as exc:                      # noqa: BLE001
        return unavailable(f"cannot read memory state: {exc}")


def metrics(sample: dict) -> dict[str, float]:
    if sample.get("status") != "ok":
        return {}
    series: dict[str, float] = {}
    for key, kind, field_name in (
        ("mem.available", "bytes", "available"),
        ("mem.available_pct", "percent", "available_pct"),
        ("mem.swap.used", "bytes", "swap_used"),
    ):
        value = sane(kind, sample.get(field_name))
        if value is not None:
            series[key] = float(value)
    return series


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    if sample.get("status") != "ok":
        return []
    # Check plausibility of all values before putting them in user-facing params.
    available_pct = sane("percent", sample.get("available_pct"))
    available = sane("bytes", sample.get("available"))
    swap_used = sane("bytes", sample.get("swap_used", 0))
    # We cannot honestly describe memory pressure without trustworthy figures.
    if available_pct is None or available is None:
        return []
    if swap_used is None:
        return []
    # Low "free" memory is Linux behaving normally: the cache fills whatever is
    # unused. Only active swapping signals real pressure.
    if available_pct >= rules.MEM_ATTENTION_AVAILABLE_PCT:
        return []
    if swap_used < rules.MEM_SWAP_ACTIVE_BYTES:
        return []
    return [Finding(
        id="memory.pressure",
        severity=Severity.ATTENTION,
        params={"available_bytes": available,
                "available_pct": available_pct,
                "swap_used_bytes": swap_used},
        detail=(f"available={available / GIB:.2f} GiB "
                f"({available_pct:.1f}%) · swap_used={swap_used / GIB:.2f} GiB"),
    )]
```

- [ ] **Step 6: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 7: Commit**

```bash
git add healthconsole/probes tests/test_probes_cpu.py tests/test_probes_memory.py
git commit -m "Add the probe contract and the CPU and memory probes

collect() measures, evaluate() judges, metrics() historises. evaluate()
is pure: breaches confirmed over time reach it through EvalContext,
computed by the scheduler.

Findings carry parameters rather than sentences, so the wording stays
in the message catalogues.

The memory probe only warns when swap is active: low free memory is
Linux behaving normally, the cache filling whatever is unused."
```

---

### Task 9: Thermal, network and battery probes

**Files:**
- Modify: `healthconsole/probes/__init__.py`
- Create: `healthconsole/probes/thermal.py`, `healthconsole/probes/network.py`, `healthconsole/probes/battery.py`
- Create: `tests/test_probes_thermal.py`, `tests/test_probes_network.py`, `tests/test_probes_battery.py`

**Interfaces:**
- Consumes: `probes.EvalContext`, `plausibility.sane`, `plausibility.battery_capacity_is_coherent`
- Produces: three modules honouring the Task 8 contract, plus `network.rates(previous: dict, current: dict, dt: float) -> dict[str, float]` (pure) and `battery.wear_pct(full: float, design: float) -> float | None`. Extends `PROBE_MODULES` in `probes/__init__.py` to include all five probes.

- [ ] **Step 1: Write the failing tests**

File `tests/test_probes_battery.py`:

```python
import unittest

from healthconsole.findings import Severity
from healthconsole.probes import EvalContext
from healthconsole.probes import battery


def context():
    return EvalContext(sustained={}, cores=4)


class TestWear(unittest.TestCase):
    def test_new_battery_has_no_wear(self):
        self.assertEqual(battery.wear_pct(full=5_000_000, design=5_000_000), 0.0)

    def test_worn_battery(self):
        self.assertAlmostEqual(
            battery.wear_pct(full=3_500_000, design=5_000_000), 30.0)

    def test_zero_design_is_unknown(self):
        self.assertIsNone(battery.wear_pct(full=1000, design=0))


class TestIncoherentDriver(unittest.TestCase):
    """Real values from the target machine: the driver lies."""

    SAMPLE = {"status": "incoherent",
              "reason": "implausible capacities reported by the driver",
              "raw": {"charge_now": 467000, "charge_full": 1000,
                      "charge_full_design": 1000}}

    def test_incoherent_sample_historises_nothing(self):
        self.assertEqual(battery.metrics(self.SAMPLE), {})

    def test_incoherent_sample_produces_an_informational_finding(self):
        # The user is told the driver is unreliable, rather than shown a
        # reassuring zero or nothing at all.
        findings = battery.evaluate(self.SAMPLE, context())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].id, "battery.incoherent")
        self.assertIs(findings[0].severity, Severity.INFO)

    def test_incoherent_finding_keeps_raw_values_in_detail(self):
        finding = battery.evaluate(self.SAMPLE, context())[0]
        self.assertIn("467000", finding.detail)


class TestEvaluate(unittest.TestCase):
    def healthy(self, wear=0.0, charge=80.0):
        return {"status": "ok", "wear_pct": wear, "charge_pct": charge,
                "present": True, "state": "Discharging", "raw": {}}

    def test_healthy_battery_is_silent(self):
        self.assertEqual(battery.evaluate(self.healthy(), context()), [])

    def test_worn_battery_warns(self):
        findings = battery.evaluate(self.healthy(wear=35.0), context())
        self.assertEqual(len(findings), 1)
        self.assertIs(findings[0].severity, Severity.ATTENTION)
        self.assertEqual(findings[0].params["wear_pct"], 35.0)

    def test_very_worn_battery_is_urgent(self):
        findings = battery.evaluate(self.healthy(wear=60.0), context())
        self.assertIs(findings[0].severity, Severity.URGENT)

    def test_absent_battery_produces_no_finding(self):
        self.assertEqual(battery.evaluate(
            {"status": "unavailable", "reason": "no battery"}, context()), [])


class TestOvercharge(unittest.TestCase):
    def test_slight_overcharge_is_clamped_not_dropped(self):
        # The driver may report charge slightly above full right after a
        # complete charge. battery_capacity_is_coherent tolerates up to 105%.
        # But a battery cannot be more than fully charged; clamping keeps the
        # value in the percent domain instead of having it silently dropped
        # downstream by sane().
        sample = {
            "status": "ok", "present": True, "state": "Full",
            "charge_pct": min(100.0, 1100 / 1050 * 100.0),  # 104.76 -> 100.0
            "wear_pct": 0.0,
            "raw": {"charge_now": 1100, "charge_full": 1050,
                    "charge_full_design": 1000},
        }
        metrics = battery.metrics(sample)
        self.assertIn("battery.charge_pct", metrics)
        self.assertEqual(metrics["battery.charge_pct"], 100.0)


class TestCollectSmoke(unittest.TestCase):
    def test_collect_never_raises(self):
        sample = battery.collect()
        self.assertIn(sample["status"], {"ok", "unavailable", "incoherent"})


if __name__ == "__main__":
    unittest.main()
```

File `tests/test_probes_network.py`:

```python
import unittest

from healthconsole.probes import network


class TestRates(unittest.TestCase):
    def test_computes_bytes_per_second(self):
        previous = {"enp0s25": {"rx": 1000, "tx": 500}}
        current = {"enp0s25": {"rx": 3000, "tx": 1500}}
        rates = network.rates(previous, current, dt=2.0)
        self.assertEqual(rates["net.enp0s25.rx_bps"], 1000.0)
        self.assertEqual(rates["net.enp0s25.tx_bps"], 500.0)

    def test_counter_reset_yields_no_negative_rate(self):
        # A restarting interface resets its counters. That is not a negative
        # throughput: the sample is skipped.
        previous = {"wlo1": {"rx": 10_000, "tx": 10_000}}
        current = {"wlo1": {"rx": 5, "tx": 5}}
        self.assertEqual(network.rates(previous, current, dt=2.0), {})

    def test_new_interface_is_ignored_until_second_sample(self):
        self.assertEqual(
            network.rates({}, {"eth0": {"rx": 1, "tx": 1}}, dt=2.0), {})

    def test_zero_dt_is_refused(self):
        previous = {"eth0": {"rx": 1, "tx": 1}}
        current = {"eth0": {"rx": 2, "tx": 2}}
        self.assertEqual(network.rates(previous, current, dt=0.0), {})


class TestCollectSmoke(unittest.TestCase):
    def test_collect_returns_interfaces(self):
        sample = network.collect()
        self.assertEqual(sample["status"], "ok")
        self.assertIn("lo", sample["counters"])


if __name__ == "__main__":
    unittest.main()
```

File `tests/test_probes_thermal.py`:

```python
import unittest

from healthconsole.findings import Severity
from healthconsole.probes import EvalContext
from healthconsole.probes import thermal


def context(sustained=None):
    return EvalContext(sustained=sustained or {}, cores=4)


def sample(package=60.0, zones=None):
    return {"status": "ok", "package_c": package,
            "zones": zones or {"coretemp": 60.0, "acpitz": 45.0}}


class TestMetrics(unittest.TestCase):
    def test_each_zone_becomes_a_series(self):
        series = thermal.metrics(sample())
        self.assertEqual(series["thermal.coretemp"], 60.0)
        self.assertEqual(series["cpu.temp.pkg"], 60.0)

    def test_impossible_temperature_is_dropped(self):
        series = thermal.metrics(
            sample(zones={"broken": 5000.0, "coretemp": 60.0}))
        self.assertNotIn("thermal.broken", series)
        self.assertIn("thermal.coretemp", series)


class TestEvaluate(unittest.TestCase):
    def test_normal_temperature_is_silent(self):
        # 77 °C is this machine's normal loaded temperature.
        self.assertEqual(thermal.evaluate(sample(77.0), context()), [])

    def test_hot_but_brief_is_silent(self):
        self.assertEqual(thermal.evaluate(sample(88.0), context()), [])

    def test_sustained_heat_warns(self):
        findings = thermal.evaluate(
            sample(88.0), context({"cpu.temp_high": True}))
        self.assertEqual(len(findings), 1)
        self.assertIs(findings[0].severity, Severity.ATTENTION)
        self.assertEqual(findings[0].params["temperature_c"], 88.0)

    def test_critical_temperature_is_urgent_without_waiting(self):
        findings = thermal.evaluate(sample(97.0), context())
        self.assertIs(findings[0].severity, Severity.URGENT)
        self.assertEqual(findings[0].id, "thermal.critical")

    def test_missing_package_temperature_is_silent(self):
        self.assertEqual(thermal.evaluate(
            {"status": "ok", "package_c": None, "zones": {}}, context()), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.probes.thermal'`

- [ ] **Step 3: Write `healthconsole/probes/thermal.py`**

```python
"""Temperatures, read straight from sysfs.

`lm-sensors` is absent on the target machine and is not required:
/sys/class/hwmon exposes everything needed.
"""

from __future__ import annotations

from pathlib import Path

from healthconsole import rules
from healthconsole.findings import Finding, Severity
from healthconsole.plausibility import sane
from healthconsole.probes import FAST, EvalContext, unavailable

NAME = "thermal"
CADENCE = FAST
HWMON = Path("/sys/class/hwmon")
PACKAGE_HINTS = ("coretemp", "x86_pkg_temp", "k10temp")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def collect() -> dict:
    zones: dict[str, float] = {}
    try:
        for hwmon in sorted(HWMON.glob("hwmon*")):
            name = _read(hwmon / "name") or hwmon.name
            inputs = sorted(hwmon.glob("temp*_input"))
            if not inputs:
                continue
            values = [int(raw) / 1000.0
                      for raw in (_read(path) for path in inputs)
                      if raw is not None]
            if values:
                zones[name] = max(values)
    except OSError as exc:
        return unavailable(f"thermal sensors unreadable: {exc}")
    if not zones:
        return unavailable("kernel exposes no thermal sensor")
    package = next((zones[hint] for hint in PACKAGE_HINTS if hint in zones), None)
    return {"status": "ok", "package_c": package, "zones": zones}


def metrics(sample: dict) -> dict[str, float]:
    if sample.get("status") != "ok":
        return {}
    series: dict[str, float] = {}
    for zone, value in sample.get("zones", {}).items():
        checked = sane("celsius", value)
        if checked is not None:
            series[f"thermal.{zone}"] = checked
    package = sane("celsius", sample.get("package_c"))
    if package is not None:
        series["cpu.temp.pkg"] = package
    return series


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    if sample.get("status") != "ok":
        return []
    package = sane("celsius", sample.get("package_c"))
    if package is None:
        return []
    if package >= rules.CPU_TEMP_URGENT_C:
        return [Finding(
            id="thermal.critical", severity=Severity.URGENT,
            params={"temperature_c": package},
            detail=(f"package={package:.1f} C >= "
                    f"{rules.CPU_TEMP_URGENT_C} C"))]
    if ctx.sustained.get("cpu.temp_high"):
        return [Finding(
            id="thermal.high", severity=Severity.ATTENTION,
            params={"temperature_c": package,
                    "sustain_minutes": rules.CPU_TEMP_SUSTAIN_SECONDS // 60},
            detail=(f"package={package:.1f} C sustained >= "
                    f"{rules.CPU_TEMP_SUSTAIN_SECONDS}s"))]
    return []
```

- [ ] **Step 4: Write `healthconsole/probes/network.py`**

```python
"""Network interfaces and throughput.

`collect()` returns absolute counters; rate computation is a separate pure
function, so it is testable without a machine and without hidden state.
"""

from __future__ import annotations

import psutil

from healthconsole.findings import Finding
from healthconsole.plausibility import sane
from healthconsole.probes import FAST, EvalContext, unavailable

NAME = "network"
CADENCE = FAST


def collect() -> dict:
    try:
        counters = {
            name: {"rx": int(counter.bytes_recv), "tx": int(counter.bytes_sent)}
            for name, counter in psutil.net_io_counters(pernic=True).items()
        }
        addresses = {
            name: [entry.address for entry in entries
                   if entry.family.name == "AF_INET"]
            for name, entries in psutil.net_if_addrs().items()
        }
        up = {name: stat.isup for name, stat in psutil.net_if_stats().items()}
        return {"status": "ok", "counters": counters,
                "addresses": addresses, "up": up}
    except Exception as exc:                      # noqa: BLE001
        return unavailable(f"cannot read network state: {exc}")


def rates(previous: dict, current: dict, dt: float) -> dict[str, float]:
    """Throughput in bytes per second between two counter samples.

    A restarting interface resets its counters: that is not a negative
    throughput, so the sample is simply skipped.
    """
    if dt <= 0:
        return {}
    series: dict[str, float] = {}
    for name, counters in current.items():
        earlier = previous.get(name)
        if earlier is None:
            continue
        for direction in ("rx", "tx"):
            delta = counters[direction] - earlier[direction]
            if delta < 0:
                continue
            value = sane("bytes_per_second", delta / dt)
            if value is not None:
                series[f"net.{name}.{direction}_bps"] = value
    return series


def metrics(sample: dict) -> dict[str, float]:
    # Rates need two samples: the scheduler computes them with rates() and
    # pushes them into the ring itself.
    return {}


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    return []
```

- [ ] **Step 5: Write `healthconsole/probes/battery.py`**

```python
"""Battery charge, state and real wear.

Two traps handled here. First, kernels expose either `energy_*` or `charge_*`
depending on the hardware — the target machine has only `charge_*`. Second, its
driver reports `charge_now` at 467 times `charge_full`: without a check, the
console would display "0 % wear, charged to 46,700 %".
"""

from __future__ import annotations

from pathlib import Path

from healthconsole import rules
from healthconsole.findings import Finding, Severity
from healthconsole.plausibility import battery_capacity_is_coherent, sane
from healthconsole.probes import FAST, EvalContext, unavailable

NAME = "battery"
CADENCE = FAST
POWER_SUPPLY = Path("/sys/class/power_supply")


def _read_int(directory: Path, name: str) -> int | None:
    try:
        return int((directory / name).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _first_battery() -> Path | None:
    if not POWER_SUPPLY.exists():
        return None
    for entry in sorted(POWER_SUPPLY.iterdir()):
        try:
            if (entry / "type").read_text(encoding="utf-8").strip() != "Battery":
                continue
        except OSError:
            continue
        if (entry / "charge_full").exists() or (entry / "energy_full").exists():
            return entry
    return None


def wear_pct(full: float, design: float) -> float | None:
    if not design:
        return None
    return max(0.0, (1.0 - full / design) * 100.0)


def collect() -> dict:
    directory = _first_battery()
    if directory is None:
        return unavailable("no battery detected (desktop machine?)")

    prefix = "charge" if (directory / "charge_full").exists() else "energy"
    now = _read_int(directory, f"{prefix}_now")
    full = _read_int(directory, f"{prefix}_full")
    design = _read_int(directory, f"{prefix}_full_design")
    try:
        state = (directory / "status").read_text(encoding="utf-8").strip()
    except OSError:
        state = None

    raw = {f"{prefix}_now": now, f"{prefix}_full": full,
           f"{prefix}_full_design": design}
    if now is None or full is None or design is None:
        return {"status": "incoherent", "raw": raw,
                "reason": "incomplete battery capacities"}
    if not battery_capacity_is_coherent(now, full, design):
        return {"status": "incoherent", "raw": raw,
                "reason": "implausible capacities reported by the driver"}

    # battery_capacity_is_coherent tolerates charge up to 105% of full to absorb
    # driver rounding after a complete charge. But a battery cannot be more than
    # fully charged; clamping keeps the value in the percent domain instead of
    # having it silently dropped downstream by sane().
    charge_pct = min(100.0, now / full * 100.0)
    return {
        "status": "ok", "present": True, "state": state,
        "charge_pct": charge_pct,
        "wear_pct": wear_pct(full, design),
        "raw": raw,
    }


def metrics(sample: dict) -> dict[str, float]:
    if sample.get("status") != "ok":
        return {}
    series: dict[str, float] = {}
    for key, field_name in (("battery.charge_pct", "charge_pct"),
                            ("battery.wear_pct", "wear_pct")):
        value = sane("percent", sample.get(field_name))
        if value is not None:
            series[key] = value
    return series


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    status = sample.get("status")
    if status == "incoherent":
        # Say the driver is unreliable rather than show a reassuring zero or
        # silently omit the battery.
        return [Finding(
            id="battery.incoherent", severity=Severity.INFO,
            params={},
            detail=f"{sample.get('reason')} · {sample.get('raw')}")]
    if status != "ok":
        return []
    wear = sane("percent", sample.get("wear_pct"))
    if wear is None or wear < rules.BATTERY_WEAR_ATTENTION_PCT:
        return []
    urgent = wear >= rules.BATTERY_WEAR_URGENT_PCT
    return [Finding(
        id="battery.wear",
        severity=Severity.URGENT if urgent else Severity.ATTENTION,
        params={"wear_pct": wear},
        detail=f"wear={wear:.1f}% · {sample.get('raw')}")]
```

- [ ] **Step 6: Extend the probe registry**

In `healthconsole/probes/__init__.py`, change `PROBE_MODULES` to include all five probes:

```python
# This is the complete set of probes for this plan.
PROBE_MODULES: tuple[str, ...] = (
    "cpu", "memory", "thermal", "network", "battery",
)
```

- [ ] **Step 7: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 8: Commit**

```bash
git add healthconsole/probes/__init__.py healthconsole/probes tests/test_probes_thermal.py tests/test_probes_network.py tests/test_probes_battery.py
git commit -m "Add the thermal, network and battery probes

The battery probe reads either energy_* or charge_*, and refuses
incoherent samples: on the target machine the driver reports a charge
467 times its full capacity, which would render as '0 % wear, charged
to 46,700 %'. It emits an informational finding instead, so the user
learns the driver is unreliable rather than seeing a reassuring zero.

Network rates are computed by a pure function that skips counter
resets rather than inventing a negative throughput."
```

---

### Task 10: Scheduler

**Files:**
- Create: `healthconsole/scheduler.py`
- Create: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `Config`, `Store`, `Ring`, `HysteresisTracker`, `load_probes`, `EvalContext`, `network.rates`
- Produces: `Scheduler(cfg, store, ring, probes=None, clock=time.time)` with `tick(now=None) -> dict`, `flush(now=None) -> int`, `maintain(now=None) -> dict`, `state() -> dict`, and `SUSTAIN_RULES: dict[str, tuple[str, float, float, float]]`

**The `state()` contract** — this is the body of `/api/now`, consumed by the server (Task 11) and the front end (Task 14). It is **locale-neutral**: ids and parameters, never sentences.

```python
{
  "ts": 1772400000.0,
  "score": 92,
  "breakdown": [["thermal.high", 8]],
  "severity": "ATTENTION",
  "findings": [{"id": "thermal.high", "severity": "ATTENTION",
                "params": {"temperature_c": 88.0}, "detail": "...",
                "action": None}],
  "probes": {"cpu": {...}},
  "depth_days": 3.2,
}
```

- [ ] **Step 1: Write the failing tests**

File `tests/test_scheduler.py`:

```python
import unittest

from healthconsole.config import Config
from healthconsole.findings import Finding, Severity
from healthconsole.probes import FAST
from healthconsole.ring import Ring
from healthconsole.scheduler import Scheduler
from healthconsole.store import Store


class FakeProbe:
    """A controlled probe, to drive the scheduler without hardware."""

    NAME = "cpu"
    CADENCE = FAST

    def __init__(self):
        self.value = 10.0
        self.status = "ok"

    def collect(self):
        return {"status": self.status, "usage_pct": self.value, "cores": 4}

    def metrics(self, sample):
        if sample.get("status") != "ok":
            return {}
        return {"cpu.usage": sample["usage_pct"]}

    def evaluate(self, sample, ctx):
        if ctx.sustained.get("cpu.usage_high"):
            return [Finding(id="cpu.usage_high", severity=Severity.ATTENTION,
                            params={"usage_pct": sample["usage_pct"]},
                            detail="raw")]
        return []


class SchedulerCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.ring = Ring(window_seconds=3600, live_seconds=2)
        self.probe = FakeProbe()
        self.scheduler = Scheduler(Config(), self.store, self.ring,
                                   probes=[self.probe])

    def tearDown(self):
        self.store.close()


class TestTick(SchedulerCase):
    def test_tick_fills_the_ring_not_the_database(self):
        self.scheduler.tick(now=1000.0)
        self.assertEqual(len(self.ring.series("cpu.usage")), 1)
        self.assertEqual(self.store.count_rows("metric"), 0)

    def test_tick_returns_the_state(self):
        state = self.scheduler.tick(now=1000.0)
        self.assertEqual(state["score"], 100)
        self.assertEqual(state["findings"], [])
        self.assertIn("cpu", state["probes"])

    def test_state_is_locale_neutral(self):
        self.probe.value = 99.0
        self.scheduler.tick(now=1000.0)
        state = self.scheduler.tick(now=1301.0)
        finding = state["findings"][0]
        self.assertEqual(set(finding),
                         {"id", "severity", "params", "detail", "action"})
        self.assertEqual(finding["severity"], "ATTENTION")

    def test_a_failing_probe_does_not_stop_the_others(self):
        class Exploding(FakeProbe):
            NAME = "memory"

            def collect(self):
                raise RuntimeError("broken sensor")

        scheduler = Scheduler(Config(), self.store, self.ring,
                              probes=[Exploding(), self.probe])
        state = scheduler.tick(now=1000.0)
        self.assertEqual(state["probes"]["memory"]["status"], "unavailable")
        self.assertEqual(state["probes"]["cpu"]["status"], "ok")

    def test_a_raising_evaluate_does_not_stop_the_others_findings(self):
        class Raising(FakeProbe):
            NAME = "memory"

            def evaluate(self, sample, ctx):
                raise RuntimeError("bad rule")

        raising = Raising()
        self.probe.value = 99.0
        scheduler = Scheduler(Config(), self.store, self.ring,
                              probes=[raising, self.probe])
        scheduler.tick(now=1000.0)
        state = scheduler.tick(now=1301.0)
        self.assertEqual(len(state["findings"]), 1)
        self.assertEqual(state["probes"]["memory"]["status"], "ok")
        self.assertIn("RuntimeError", state["probes"]["memory"]["eval_error"])

    def test_a_raising_metrics_marks_the_probe_unavailable(self):
        class BadMetrics(FakeProbe):
            NAME = "memory"

            def metrics(self, sample):
                raise RuntimeError("bad metrics")

        scheduler = Scheduler(Config(), self.store, self.ring,
                              probes=[BadMetrics()])
        state = scheduler.tick(now=1000.0)
        self.assertEqual(state["probes"]["memory"]["status"], "unavailable")
        self.assertIn("RuntimeError", state["probes"]["memory"]["reason"])


class TestSustained(SchedulerCase):
    def test_brief_spike_produces_no_finding(self):
        self.probe.value = 99.0
        self.assertEqual(self.scheduler.tick(now=1000.0)["findings"], [])

    def test_sustained_load_produces_a_finding(self):
        self.probe.value = 99.0
        self.scheduler.tick(now=1000.0)
        state = self.scheduler.tick(now=1301.0)
        self.assertEqual(len(state["findings"]), 1)
        self.assertEqual(state["score"], 92)
        self.assertEqual(state["breakdown"], [["cpu.usage_high", 8]])


class TestFlush(SchedulerCase):
    def test_flush_writes_the_aggregate_to_the_database(self):
        for i in range(15):
            self.scheduler.tick(now=1000.0 + i * 2)
        self.assertEqual(self.scheduler.flush(now=1030.0), 1)
        self.assertEqual(len(self.store.read_series("cpu.usage", 0, 2000)), 1)

    def test_flush_with_no_new_point_writes_nothing(self):
        self.assertEqual(self.scheduler.flush(now=1000.0), 0)

    def test_flush_averages_the_window(self):
        for i, value in enumerate([10.0, 20.0, 30.0]):
            self.probe.value = value
            self.scheduler.tick(now=1000.0 + i * 2)
        self.scheduler.flush(now=1030.0)
        self.assertAlmostEqual(
            self.store.read_series("cpu.usage", 0, 2000)[0][1], 20.0)


class TestMaintain(SchedulerCase):
    def test_maintain_aggregates_and_prunes(self):
        report = self.scheduler.maintain(now=100 * 86400)
        self.assertIn("aggregated", report)
        self.assertIn("deleted", report)


class TestDepth(SchedulerCase):
    def test_depth_reports_what_exists_not_what_is_configured(self):
        self.assertEqual(self.scheduler.state()["depth_days"], 0.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.scheduler'`

- [ ] **Step 3: Write `healthconsole/scheduler.py`**

```python
"""The two cadences and their orchestration.

The live view fills the in-memory ring; only an aggregated value reaches the
database. The scheduler is also the only component that knows the clock, which
is what keeps probes and rules purely functional.

Single-writer invariant: `Store` opens SQLite with `check_same_thread=False`
and `key_id` performs an unlocked SELECT-then-INSERT, which is safe only
because exactly one thread writes while HTTP request threads use read-only
methods. The `Scheduler` is that one writer — it must remain the ONLY
component that calls `write_metrics`, `aggregate_5m` or `prune`. A second
write path would race on `metric_key.key UNIQUE` and raise IntegrityError.
"""

from __future__ import annotations

import sys
import time
from dataclasses import asdict

from healthconsole import rules
from healthconsole.config import Config
from healthconsole.findings import Severity
from healthconsole.probes import FAST, EvalContext, load_probes, unavailable
from healthconsole.probes import network as network_probe
from healthconsole.ring import Ring
from healthconsole.store import Store
from healthconsole.verdict import (
    HysteresisTracker, score, score_breakdown, worst,
)

# breach key -> (metric, high threshold, low threshold, seconds to hold)
SUSTAIN_RULES: dict[str, tuple[str, float, float, float]] = {
    "cpu.usage_high": ("cpu.usage", rules.CPU_USAGE_ATTENTION_PCT,
                       rules.CPU_USAGE_ATTENTION_CLEAR_PCT,
                       rules.CPU_USAGE_SUSTAIN_SECONDS),
    "cpu.temp_high": ("cpu.temp.pkg", rules.CPU_TEMP_ATTENTION_C,
                      rules.CPU_TEMP_ATTENTION_CLEAR_C,
                      rules.CPU_TEMP_SUSTAIN_SECONDS),
}

EMPTY_STATE: dict = {
    "ts": 0.0, "score": 100, "breakdown": [], "severity": "OK",
    "findings": [], "probes": {}, "depth_days": 0.0,
}


class Scheduler:
    """Ties probes, ring, hysteresis and store together on a single clock.

    The Scheduler is the only background writer to the Store: `write_metrics`,
    `aggregate_5m` and `prune` must never be called from anywhere else, since
    `Store` assumes a single writer thread (see module docstring above).
    """

    def __init__(self, cfg: Config, store: Store, ring: Ring,
                 probes=None, clock=time.time) -> None:
        self.cfg = cfg
        self.store = store
        self.ring = ring
        self.clock = clock
        self.probes = load_probes(FAST) if probes is None else probes
        self.tracker = HysteresisTracker()
        self._last_flush: float | None = None
        self._net_previous: dict = {}
        self._net_previous_ts: float | None = None
        self._state: dict = dict(EMPTY_STATE)

    # --- fast cadence -------------------------------------------------

    def tick(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        samples: dict[str, dict] = {}
        measurements: dict[str, float] = {}

        for probe in self.probes:
            try:
                sample = probe.collect()
                samples[probe.NAME] = sample
                measurements.update(probe.metrics(sample))
            except Exception as exc:              # noqa: BLE001
                # A failing probe never brings the others down.
                samples[probe.NAME] = unavailable(
                    f"probe failed: {type(exc).__name__}: {exc}")

        measurements.update(self._network_rates(samples, now))
        for key, value in measurements.items():
            self.ring.push(key, value, ts=now)

        sustained = {
            flag: self.tracker.update(flag, measurements.get(metric),
                                      high, low, now, sustain)
            for flag, (metric, high, low, sustain) in SUSTAIN_RULES.items()
        }
        ctx = EvalContext(sustained=sustained,
                          cores=samples.get("cpu", {}).get("cores", 1))

        findings = []
        for probe in self.probes:
            try:
                findings.extend(probe.evaluate(samples.get(probe.NAME, {}), ctx))
            except Exception as exc:              # noqa: BLE001
                # The reading itself succeeded and was already pushed to the
                # ring before evaluate() ran; only the judgement failed. That
                # is a different failure from an unavailable sensor, so the
                # status is deliberately left as measured rather than
                # flipped to unavailable, which would misdescribe what broke
                # and would hide the collected data from Expert mode.
                sample = samples.get(probe.NAME)
                if isinstance(sample, dict):
                    sample["eval_error"] = f"{type(exc).__name__}: {exc}"
                print(f"{probe.NAME}: evaluate() failed: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                continue

        self._state = {
            "ts": now,
            "score": score(findings),
            "breakdown": [list(pair) for pair in score_breakdown(findings)],
            "severity": worst(findings).name,
            "findings": [self._serialise(finding) for finding in findings],
            "probes": samples,
            "depth_days": self._depth_days(now),
        }
        return self._state

    def _network_rates(self, samples: dict, now: float) -> dict[str, float]:
        sample = samples.get("network")
        if not sample or sample.get("status") != "ok":
            return {}
        current = sample["counters"]
        rates: dict[str, float] = {}
        if self._net_previous and self._net_previous_ts is not None:
            rates = network_probe.rates(self._net_previous, current,
                                        dt=now - self._net_previous_ts)
        self._net_previous = current
        self._net_previous_ts = now
        return rates

    @staticmethod
    def _serialise(finding) -> dict:
        data = asdict(finding)
        data["severity"] = Severity(finding.severity).name
        return data

    def _depth_days(self, now: float) -> float:
        seconds = self.store.available_depth_seconds("metric", int(now))
        return round(seconds / 86_400, 2)

    # --- write cadence ------------------------------------------------

    def flush(self, now: float | None = None) -> int:
        """Write the elapsed window's average to the database.
        Returns the number of instants written (0 or 1)."""
        now = self.clock() if now is None else now
        since = 0.0 if self._last_flush is None else self._last_flush
        rows = []
        for key in self.ring.keys():
            aggregated = self.ring.aggregate(key, since=since)
            if aggregated is None:
                continue
            average, low, high = aggregated
            rows.append((key, average, low, high))
        if not rows:
            return 0
        self.store.write_metrics(int(now), rows)
        self._last_flush = now
        return 1

    # --- daily maintenance --------------------------------------------

    def maintain(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        aggregated = self.store.aggregate_5m(int(now))
        deleted = self.store.prune(self.cfg, int(now))
        return {"aggregated": aggregated, "deleted": deleted}

    def state(self) -> dict:
        return self._state
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 5: Commit**

```bash
git add healthconsole/scheduler.py tests/test_scheduler.py
git commit -m "Add the two-cadence scheduler

tick() fills the ring buffer and produces the state; flush() writes the
window average to the database; maintain() aggregates and prunes.

The scheduler is the only component that knows the clock, which keeps
probes and rules purely functional. A probe raising an exception is
marked unavailable without stopping the others.

The state it produces is locale-neutral: finding ids and parameters,
never sentences."
```

---

### Task 11: HTTP server, token and `/api/now`

**Files:**
- Create: `healthconsole/server.py`
- Create: `tests/test_server.py`
- Create: `web/index.html` (minimal placeholder, replaced in Task 14)

**Interfaces:**
- Consumes: `Config`, `Scheduler`
- Produces: `is_loopback(addr: str) -> bool`, `authorise(client_ip: str, presented: str | None, cfg: Config) -> bool`, `generate_token() -> str`, `make_server(cfg, scheduler, web_dir: Path = WEB_DIR) -> ThreadingHTTPServer`, `WEB_DIR: Path`, `SECURITY_HEADERS: dict[str, str]`

- [ ] **Step 1: Write the failing tests**

File `tests/test_server.py`:

```python
import json
import socket
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from healthconsole.config import Config
from healthconsole.ring import Ring
from healthconsole.scheduler import Scheduler
from healthconsole.server import (
    authorise, generate_token, is_loopback, make_server,
)
from healthconsole.store import Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class TestLoopback(unittest.TestCase):
    def test_ipv4_loopback(self):
        self.assertTrue(is_loopback("127.0.0.1"))

    def test_ipv6_loopback(self):
        self.assertTrue(is_loopback("::1"))

    def test_lan_address_is_not_loopback(self):
        self.assertFalse(is_loopback("192.168.0.3"))

    def test_garbage_is_not_loopback(self):
        self.assertFalse(is_loopback("not-an-address"))


class TestAuthorise(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(token="s3cr3t")

    def test_loopback_needs_no_token(self):
        self.assertTrue(authorise("127.0.0.1", None, self.cfg))

    def test_lan_without_token_is_refused(self):
        self.assertFalse(authorise("192.168.0.3", None, self.cfg))

    def test_lan_with_wrong_token_is_refused(self):
        self.assertFalse(authorise("192.168.0.3", "wrong", self.cfg))

    def test_lan_with_right_token_is_allowed(self):
        self.assertTrue(authorise("192.168.0.3", "s3cr3t", self.cfg))

    def test_empty_configured_token_never_authorises_the_lan(self):
        # An empty token must not open access to the whole network.
        self.assertFalse(authorise("192.168.0.3", "", Config(token="")))


class TestToken(unittest.TestCase):
    def test_token_is_long_and_random(self):
        first, second = generate_token(), generate_token()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 43)


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.scheduler = Scheduler(Config(), cls.store, Ring())
        cls.scheduler.tick(now=1000.0)
        cls.server = make_server(Config(bind="127.0.0.1", port=0),
                                 cls.scheduler, WEB_DIR)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def get(self, path):
        return urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=5)

    def test_api_now_returns_the_state(self):
        payload = json.loads(self.get("/api/now").read())
        self.assertIn("score", payload)
        self.assertIn("probes", payload)
        self.assertIn("depth_days", payload)

    def test_index_is_served(self):
        response = self.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/nonexistent")
        self.assertEqual(ctx.exception.code, 404)

    def test_security_headers_are_present(self):
        headers = self.get("/").headers
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_path_traversal_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/static/../../etc/passwd")
        self.assertIn(ctx.exception.code, (403, 404))

    def test_error_body_is_machine_readable(self):
        # Errors carry codes, not user-facing prose: the browser localises.
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/nonexistent")
        payload = json.loads(ctx.exception.read())
        self.assertIn("error", payload)
        self.assertIn("detail", payload)
        self.assertNotIn(" ", payload["error"])
        self.assertEqual(payload["error"], payload["error"].lower())
        if payload["detail"]:
            self.assertNotIn(" ", payload["detail"])

    def test_unsupported_method_still_gets_security_headers_and_json(self):
        # The base class handles unknown verbs itself, before our routing
        # ever runs -- that path must not bypass the security headers or
        # fall back to an HTML body.
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/now", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 501)
        self.assertIn("default-src 'self'",
                      ctx.exception.headers["Content-Security-Policy"])
        payload = json.loads(ctx.exception.read())
        self.assertIn("error", payload)

    def test_error_response_carries_connection_close(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/now", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.headers["Connection"], "close")

    def test_head_request_sends_headers_but_no_body(self):
        # http.client and urllib both hide a missing HEAD-body guard --
        # their HEAD-aware readers stop at the headers regardless of what
        # is actually on the wire. A raw socket is the only way to see it.
        with socket.create_connection(
                ("127.0.0.1", self.port), timeout=5) as sock:
            sock.settimeout(2)
            sock.sendall(b"HEAD /api/now HTTP/1.1\r\nHost: localhost\r\n\r\n")
            chunks = []
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except socket.timeout:
                pass
        raw = b"".join(chunks)
        headers, _, body = raw.partition(b"\r\n\r\n")
        self.assertIn(b"Content-Length", headers)
        self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.server'`

- [ ] **Step 3: Write `healthconsole/server.py`**

```python
"""HTTP server.

The console listens on the local network: any non-loopback access requires the
token, compared in constant time. Reading is open; acting is not — but actions
belong to plan 3.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from healthconsole.config import Config

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# Status codes the base class can raise itself (an unsupported HTTP verb, a
# malformed request line) before our own routing ever runs. Mapped to a
# machine-readable code so those responses stay code-shaped like every other
# error body, instead of falling back to the base class's HTML page.
FALLBACK_ERROR_CODES = {
    400: "bad_request",
    501: "not_implemented",
}


def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def authorise(client_ip: str, presented: str | None, cfg: Config) -> bool:
    if is_loopback(client_ip):
        return True
    if not cfg.token or not presented:
        return False
    return hmac.compare_digest(cfg.token, presented)


def make_server(cfg: Config, scheduler,
                web_dir: Path = WEB_DIR) -> ThreadingHTTPServer:

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        _stream_lock = threading.Lock()
        _stream_count = 0

        def log_message(self, *args):     # quiet: logged elsewhere
            pass

        # --- helpers ----------------------------------------------
        def _send(self, code: int, body: bytes, content_type: str,
                  extra_headers: dict[str, str] | None = None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            # RFC 9110 SS9.3.2: a HEAD response carries the header fields
            # the equivalent GET would have returned -- Content-Length
            # included -- but never a body, so only the write is skipped.
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, payload: dict,
                  extra_headers: dict[str, str] | None = None):
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8", extra_headers)

        def _error(self, code: int, error: str, detail: str = "",
                   extra_headers: dict[str, str] | None = None):
            # Machine-readable codes, never user-facing prose: the browser
            # localises from its catalogue.
            self._json(code, {"error": error, "detail": detail}, extra_headers)

        def send_error(self, code, message=None, explain=None):
            # The base class calls this directly for errors it detects
            # itself (an unsupported HTTP verb, a malformed request line)
            # before our own do_GET ever runs. Without this override those
            # responses would carry the base class's HTML body and none of
            # our security headers -- "security headers on every response"
            # would not hold.
            #
            # Connection: close is sent only here, not from _send: the
            # base class's own send_error always sends it, and
            # send_header('Connection', 'close') has the side effect of
            # forcing close_connection = True -- a safety net for the
            # narrow cases where parse_request() left close_connection
            # False before an error fired (an overlong HTTP/1.1 request
            # line, or an HTTP/2.0 request line). A normal 200 response
            # must not carry this, so it stays out of _send.
            self._error(code, FALLBACK_ERROR_CODES.get(code, "http_error"),
                       "", {"Connection": "close"})

        def _authorised(self, query) -> bool:
            # Preferred: the X-Health-Token header. Fallback: ?k=<token>,
            # which exists only so a phone opening a shared or bookmarked
            # link can authenticate -- plain navigation has no way to set a
            # header. This is a deliberate trade-off: log_message() above
            # keeps the token out of this process's own access log, and
            # Referrer-Policy: no-referrer keeps it out of cross-navigation
            # Referer headers, but neither reaches browser history,
            # bookmarks, or an intermediary's own logs (LAN router, proxy,
            # connection tracking).
            presented = (self.headers.get("X-Health-Token")
                         or query.get("k", [None])[0])
            return authorise(self.client_address[0], presented, cfg)

        def _serve_file(self, relative: str):
            target = (web_dir / relative).resolve()
            try:
                target.relative_to(web_dir.resolve())
            except (ValueError, OSError):
                return self._error(403, "path_refused", relative)
            if not target.is_file():
                return self._error(404, "file_not_found", relative)
            self._send(200, target.read_bytes(),
                       CONTENT_TYPES.get(target.suffix,
                                         "application/octet-stream"))

        # --- routing ----------------------------------------------
        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)

            if not self._authorised(query):
                return self._error(401, "token_required", "")

            if parsed.path == "/":
                return self._serve_file("index.html")
            if parsed.path.startswith("/static/"):
                return self._serve_file(parsed.path[len("/static/"):])
            if parsed.path == "/api/now":
                return self._json(200, scheduler.state())
            return self._error(404, "unknown_route", parsed.path)

    server = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    server.daemon_threads = True
    return server
```

- [ ] **Step 4: Create the placeholder page so the static-file tests can pass**

```bash
cat > web/index.html <<'HTML'
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Health Console</title></head>
<body><p>Health Console — interface loading.</p></body>
</html>
HTML
```

- [ ] **Step 5: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 6: Commit**

```bash
git add healthconsole/server.py tests/test_server.py web/index.html
git commit -m "Add the HTTP server, token check and /api/now

Loopback access is open; any network access requires the token,
compared in constant time. An empty token never opens network access.

Static files are resolved and then verified to be descendants of the
web directory, closing path traversal. CSP, nosniff and no-referrer
headers on every response.

Error bodies carry machine-readable codes rather than prose, so the
browser localises them from its catalogue."
```

---

### Task 12: Live streaming (SSE) and history

**Files:**
- Modify: `healthconsole/server.py` (routes `/api/stream` and `/api/history`)
- Create: `tests/test_stream.py`

**Interfaces:**
- Consumes: `Scheduler.state()`, `Store.read_series`, `Store.available_depth_seconds`
- Produces: `RANGES: dict[str, int]` (`1h`, `24h`, `7d`, `90d`), `MAX_STREAMS: int = 8`, routes `/api/stream` (SSE) and `/api/history?metric=<key>&range=<window>`

- [ ] **Step 1: Write the failing tests**

File `tests/test_stream.py`:

```python
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from healthconsole.config import Config
from healthconsole.ring import Ring
from healthconsole.scheduler import Scheduler
from healthconsole.server import MAX_STREAMS, RANGES, make_server
from healthconsole.store import Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class HttpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.scheduler = Scheduler(Config(), cls.store, Ring())
        cls.scheduler.tick(now=1000.0)
        # Recent, in-window instants against the real clock: the history
        # route filters with `time.time()`, so fixture rows must sit inside
        # the windows the tests actually request, not near the Unix epoch.
        cls.now = int(time.time())
        cls.store.write_metrics(cls.now - 3600, [("cpu.usage", 20.0, 10.0, 30.0)])
        cls.store.write_metrics(cls.now - 1800, [("cpu.usage", 40.0, 30.0, 50.0)])
        # Fold the raw rows into the 5-minute aggregate table too, so a
        # long-range request (which reads metric_5m) has data to find.
        cls.store.aggregate_5m(cls.now)
        cls.server = make_server(Config(bind="127.0.0.1", port=0),
                                 cls.scheduler, WEB_DIR)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"


class TestHistory(HttpCase):
    def test_recent_range_returns_points_from_the_raw_table(self):
        # 24h is at or under RAW_TABLE_MAX_SECONDS, so it reads the raw
        # `metric` table the fixture writes to directly.
        body = json.loads(urllib.request.urlopen(
            self.url("/api/history?metric=cpu.usage&range=24h"),
            timeout=5).read())
        self.assertEqual(body["metric"], "cpu.usage")
        self.assertEqual(body["table"], "metric")
        self.assertGreaterEqual(len(body["points"]), 1)

    def test_long_range_returns_points_from_the_aggregate_table(self):
        # 90d exceeds RAW_TABLE_MAX_SECONDS, so it reads the `metric_5m`
        # aggregate table, which the fixture folds the raw rows into.
        body = json.loads(urllib.request.urlopen(
            self.url("/api/history?metric=cpu.usage&range=90d"),
            timeout=5).read())
        self.assertEqual(body["metric"], "cpu.usage")
        self.assertEqual(body["table"], "metric_5m")
        self.assertGreaterEqual(len(body["points"]), 1)

    def test_unknown_range_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(
                self.url("/api/history?metric=cpu.usage&range=42x"), timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_missing_metric_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.url("/api/history?range=24h"), timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_metric_returns_empty_not_an_error(self):
        body = json.loads(urllib.request.urlopen(
            self.url("/api/history?metric=nonexistent&range=24h"),
            timeout=5).read())
        self.assertEqual(body["points"], [])

    def test_response_states_the_available_depth(self):
        # Always the depth actually available: a half-empty 90-day chart
        # would suggest a collection failure rather than a young history.
        body = json.loads(urllib.request.urlopen(
            self.url("/api/history?metric=cpu.usage&range=90d"),
            timeout=5).read())
        self.assertIn("depth_days", body)


class TestStream(HttpCase):
    def test_stream_announces_the_right_content_type(self):
        response = urllib.request.urlopen(self.url("/api/stream"), timeout=5)
        self.assertEqual(response.headers["Content-Type"],
                         "text/event-stream; charset=utf-8")
        response.close()

    def test_first_event_carries_the_state(self):
        response = urllib.request.urlopen(self.url("/api/stream"), timeout=5)
        lines = [response.readline().decode("utf-8") for _ in range(3)]
        response.close()
        joined = "".join(lines)
        self.assertIn("event: state", joined)
        self.assertIn('"score"', joined)

    def test_stream_cap_is_declared(self):
        self.assertEqual(MAX_STREAMS, 8)

    def test_known_ranges(self):
        self.assertEqual(set(RANGES), {"1h", "24h", "7d", "90d"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `ImportError: cannot import name 'MAX_STREAMS'`

- [ ] **Step 3: Add the constants near the top of `healthconsole/server.py`**

Add `import time` to the imports, then:

```python
RANGES: dict[str, int] = {"1h": 3600, "24h": 86_400,
                          "7d": 604_800, "90d": 7_776_000}
# Each stream holds a thread. Past this, the client falls back to polling.
MAX_STREAMS = 8
# Ranges up to this length are served from the raw table; longer ones from
# the 5-minute aggregates.
RAW_TABLE_MAX_SECONDS = 172_800
```

- [ ] **Step 4: Add the two routes in `do_GET`, before the final 404**

```python
            if parsed.path == "/api/history":
                return self._history(query)
            if parsed.path == "/api/stream":
                return self._stream()
```

- [ ] **Step 5: Add the matching methods to `Handler`**

```python
        def _history(self, query):
            metric = query.get("metric", [None])[0]
            window = query.get("range", ["24h"])[0]
            if not metric:
                return self._error(400, "missing_parameter", "metric")
            if window not in RANGES:
                return self._error(400, "unknown_range",
                                   ", ".join(RANGES))
            now = int(time.time())
            since = now - RANGES[window]
            table = ("metric" if RANGES[window] <= RAW_TABLE_MAX_SECONDS
                     else "metric_5m")
            points = scheduler.store.read_series(metric, since, now, table=table)
            depth = scheduler.store.available_depth_seconds(table, now)
            return self._json(200, {
                "metric": metric, "range": window, "table": table,
                "points": [[ts, value] for ts, value in points],
                # Always the depth actually available: a half-empty "90 days"
                # chart would suggest a collection failure when the history
                # has simply just begun.
                "depth_days": round(depth / 86_400, 2),
            })

        def _stream(self):
            with Handler._stream_lock:
                if Handler._stream_count >= MAX_STREAMS:
                    return self._error(503, "too_many_streams", str(MAX_STREAMS))
                Handler._stream_count += 1
            try:
                self.send_response(200)
                self.send_header("Content-Type",
                                 "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                # Without Content-Length, HTTP/1.1 must close the connection at
                # the end of the stream: otherwise the client cannot tell where
                # the body ends and the next response is misframed.
                self.send_header("Connection", "close")
                self.close_connection = True
                for name, value in SECURITY_HEADERS.items():
                    self.send_header(name, value)
                self.end_headers()
                while True:
                    payload = json.dumps(scheduler.state())
                    self.wfile.write(b"event: state\n")
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(cfg.sampling.live_seconds)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with Handler._stream_lock:
                    Handler._stream_count -= 1
```

- [ ] **Step 6: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 7: Commit**

```bash
git add healthconsole/server.py tests/test_stream.py
git commit -m "Add SSE streaming and the history route

The SSE stream is capped at eight concurrent clients, each holding a
thread; past that the page falls back to polling rather than exhausting
the server.

The history response always states the depth actually available: a
half-empty 90-day chart would suggest a collection failure when the
history has simply just begun."
```

---

### Task 13: Message catalogues and completeness tests

**Files:**
- Create: `web/i18n/en.json`, `web/i18n/fr.json`
- Create: `tests/test_i18n.py`

**Interfaces:**
- Consumes: `healthconsole.findings.FINDING_IDS`, `FINDING_PARAMS`, `Severity`
- Produces: two catalogue files with an identical key set, served as static files under `/static/i18n/<locale>.json`

**Why this task exists before the interface:** a missing translation must be a
failing test, not a hole the user discovers. The catalogues and their guarantees
come first; the rendering code that consumes them comes next.

**Key scheme:**

| Pattern | Purpose |
|---|---|
| `finding.<id>.title` | the finding headline, Simple mode |
| `finding.<id>.why` | why it matters, in plain language |
| `severity.<NAME>.word` | "Attention" — the word that carries the state alongside the icon |
| `severity.<NAME>.icon` | the glyph; colour never carries meaning alone |
| `verdict.<NAME>` | the one-line verdict sentence |
| `ui.*` | interface chrome |

- [ ] **Step 1: Write the failing tests**

File `tests/test_i18n.py`:

```python
import json
import re
import unittest
from pathlib import Path

from healthconsole.findings import FINDING_IDS, FINDING_PARAMS, Severity

I18N = Path(__file__).resolve().parent.parent / "web" / "i18n"
DEFAULT_LOCALE = "en"
PLACEHOLDER = re.compile(r"\{(\w+)\}")

REQUIRED_UI_KEYS = frozenset({
    "ui.title",
    "ui.mode.simple",
    "ui.mode.expert",
    "ui.mode.group",
    "ui.language",
    "ui.score.label",
    "ui.freshness.live",
    "ui.freshness.stale",
    "ui.probe.unavailable",
    "ui.expert.placeholder",
})


def load(locale):
    return json.loads((I18N / f"{locale}.json").read_text(encoding="utf-8"))


def locales():
    return sorted(path.stem for path in I18N.glob("*.json"))


class TestCataloguesExist(unittest.TestCase):
    def test_english_and_french_are_present(self):
        self.assertIn("en", locales())
        self.assertIn("fr", locales())

    def test_every_catalogue_is_valid_json_and_flat(self):
        for locale in locales():
            for key, value in load(locale).items():
                self.assertIsInstance(value, str,
                                      f"{locale}:{key} is not a string")


class TestCompleteness(unittest.TestCase):
    def test_every_finding_has_a_title_and_a_why(self):
        for locale in locales():
            catalogue = load(locale)
            for finding_id in sorted(FINDING_IDS):
                for suffix in ("title", "why"):
                    key = f"finding.{finding_id}.{suffix}"
                    self.assertIn(key, catalogue, f"{locale} lacks {key}")

    def test_every_severity_has_a_word_an_icon_and_a_verdict(self):
        for locale in locales():
            catalogue = load(locale)
            for severity in Severity:
                for key in (f"severity.{severity.name}.word",
                            f"severity.{severity.name}.icon",
                            f"verdict.{severity.name}"):
                    self.assertIn(key, catalogue, f"{locale} lacks {key}")

    def test_required_ui_keys_are_present(self):
        for locale in locales():
            missing = REQUIRED_UI_KEYS - set(load(locale))
            self.assertEqual(missing, set(), f"{locale} lacks {missing}")

    def test_all_catalogues_share_the_same_key_set(self):
        # An extra key is as much a failure as a missing one: it means one
        # catalogue drifted.
        reference = set(load(DEFAULT_LOCALE))
        for locale in locales():
            self.assertEqual(set(load(locale)), reference,
                             f"{locale} diverges from {DEFAULT_LOCALE}")

    def test_no_value_is_empty(self):
        for locale in locales():
            for key, value in load(locale).items():
                self.assertTrue(value.strip(), f"{locale}:{key} is empty")


class TestPlaceholders(unittest.TestCase):
    def test_corresponding_entries_use_the_same_placeholders(self):
        reference = load(DEFAULT_LOCALE)
        for locale in locales():
            if locale == DEFAULT_LOCALE:
                continue
            catalogue = load(locale)
            for key, text in reference.items():
                self.assertEqual(
                    set(PLACEHOLDER.findall(text)),
                    set(PLACEHOLDER.findall(catalogue[key])),
                    f"{locale}:{key} uses different placeholders")

    def test_placeholders_match_what_the_probes_emit(self):
        # A template referring to {wear} when the probe emits {wear_pct}
        # would render a literal brace to the user.
        for locale in locales():
            catalogue = load(locale)
            for finding_id, allowed in FINDING_PARAMS.items():
                for suffix in ("title", "why"):
                    text = catalogue[f"finding.{finding_id}.{suffix}"]
                    used = set(PLACEHOLDER.findall(text))
                    self.assertLessEqual(
                        used, set(allowed),
                        f"{locale}:finding.{finding_id}.{suffix} uses "
                        f"{used - set(allowed)}, which the probe never emits")


class TestNoJargonLeaksToSimpleMode(unittest.TestCase):
    JARGON = ("swap", "sysfs", "hwmon", "SMART", "psutil", "charge_full")

    def test_titles_and_reasons_avoid_raw_jargon(self):
        for locale in locales():
            catalogue = load(locale)
            for finding_id in sorted(FINDING_IDS):
                for suffix in ("title", "why"):
                    text = catalogue[f"finding.{finding_id}.{suffix}"]
                    for term in self.JARGON:
                        self.assertNotIn(
                            term, text,
                            f"{locale}:finding.{finding_id}.{suffix} leaks "
                            f"'{term}' into Simple mode")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `FileNotFoundError: web/i18n/en.json`

- [ ] **Step 3: Write `web/i18n/en.json`**

```json
{
  "ui.title": "Health Console",
  "ui.mode.simple": "Simple",
  "ui.mode.expert": "Expert",
  "ui.mode.group": "Detail level",
  "ui.language": "Language",
  "ui.score.label": "Overall health",
  "ui.freshness.live": "Up to date · last reading at {time}",
  "ui.freshness.stale": "Data frozen since {time} · reconnecting…",
  "ui.probe.unavailable": "Measurement unavailable: {probe}",
  "ui.expert.placeholder": "Expert mode arrives in plan 2.",

  "severity.OK.word": "All good",
  "severity.OK.icon": "●",
  "severity.INFO.word": "Information",
  "severity.INFO.icon": "ℹ",
  "severity.ATTENTION.word": "Attention",
  "severity.ATTENTION.icon": "⚠",
  "severity.URGENT.word": "Urgent",
  "severity.URGENT.icon": "✖",

  "verdict.OK": "Your computer is in good shape.",
  "verdict.INFO": "Everything works, with one thing worth knowing.",
  "verdict.ATTENTION": "Something deserves your attention.",
  "verdict.URGENT": "Action is needed soon.",

  "finding.cpu.usage_high.title": "Your computer has been working hard for a while",
  "finding.cpu.usage_high.why": "A program has been demanding a lot from the machine for several minutes. That is normal during heavy work, but if it lasts for no reason the computer may heat up and slow down.",

  "finding.memory.pressure.title": "Memory is nearly full ({available_pct} % free)",
  "finding.memory.pressure.why": "The computer has started moving data to the disk, which is far slower than memory. Closing a few applications should make it responsive again.",

  "finding.thermal.high.title": "Your computer has been running hot for a while ({temperature_c} °C)",
  "finding.thermal.high.why": "The heat has lasted several minutes. If this happens often, the fan is probably clogged with dust: cleaning it extends the life of the machine.",

  "finding.thermal.critical.title": "Your computer is very hot ({temperature_c} °C)",
  "finding.thermal.critical.why": "At this temperature the machine throttles itself to avoid damage and may shut down on its own. Check that the air vents are not blocked and place the device on a hard surface.",

  "finding.battery.wear.title": "The battery has lost {wear_pct} % of its original capacity",
  "finding.battery.wear.why": "It therefore lasts less time than when new. This is normal battery ageing, but past half its capacity, replacing it becomes reasonable.",

  "finding.battery.incoherent.title": "Battery readings cannot be trusted",
  "finding.battery.incoherent.why": "This computer reports impossible battery figures, which is a known flaw in some manufacturers' firmware. Rather than show you a wrong number, we show none. The battery itself is probably fine."
}
```

- [ ] **Step 4: Write `web/i18n/fr.json`**

```json
{
  "ui.title": "Console de santé",
  "ui.mode.simple": "Simple",
  "ui.mode.expert": "Expert",
  "ui.mode.group": "Niveau de détail",
  "ui.language": "Langue",
  "ui.score.label": "Santé globale",
  "ui.freshness.live": "À jour · dernière mesure à {time}",
  "ui.freshness.stale": "Données figées depuis {time} · reconnexion…",
  "ui.probe.unavailable": "Mesure indisponible : {probe}",
  "ui.expert.placeholder": "Le mode Expert arrive au plan 2.",

  "severity.OK.word": "Tout va bien",
  "severity.OK.icon": "●",
  "severity.INFO.word": "Information",
  "severity.INFO.icon": "ℹ",
  "severity.ATTENTION.word": "Attention",
  "severity.ATTENTION.icon": "⚠",
  "severity.URGENT.word": "Urgent",
  "severity.URGENT.icon": "✖",

  "verdict.OK": "Votre ordinateur se porte bien.",
  "verdict.INFO": "Tout fonctionne, avec un point à connaître.",
  "verdict.ATTENTION": "Quelque chose mérite votre attention.",
  "verdict.URGENT": "Une action est nécessaire rapidement.",

  "finding.cpu.usage_high.title": "Votre ordinateur travaille beaucoup depuis un moment",
  "finding.cpu.usage_high.why": "Un programme sollicite fortement la machine depuis plusieurs minutes. C'est normal pendant un traitement lourd, mais si cela dure sans raison, l'ordinateur risque de chauffer et de ralentir.",

  "finding.memory.pressure.title": "La mémoire est presque pleine ({available_pct} % de libre)",
  "finding.memory.pressure.why": "L'ordinateur a commencé à déplacer des données vers le disque, beaucoup plus lent que la mémoire. Fermer quelques applications devrait le rendre à nouveau réactif.",

  "finding.thermal.high.title": "Votre ordinateur chauffe depuis un moment ({temperature_c} °C)",
  "finding.thermal.high.why": "La chaleur dure depuis plusieurs minutes. Si cela se répète souvent, le ventilateur est probablement encrassé : un dépoussiérage rallonge la vie de la machine.",

  "finding.thermal.critical.title": "Votre ordinateur est très chaud ({temperature_c} °C)",
  "finding.thermal.critical.why": "À cette température, la machine se bride pour se protéger et peut s'éteindre seule. Vérifiez que les grilles d'aération ne sont pas obstruées et posez l'appareil sur une surface dure.",

  "finding.battery.wear.title": "La batterie a perdu {wear_pct} % de sa capacité d'origine",
  "finding.battery.wear.why": "Elle tient donc moins longtemps qu'à l'achat. C'est l'usure normale d'une batterie, mais au-delà de la moitié, son remplacement devient raisonnable.",

  "finding.battery.incoherent.title": "Les mesures de la batterie ne sont pas fiables",
  "finding.battery.incoherent.why": "Cet ordinateur rapporte des valeurs de batterie impossibles, un défaut connu du micrologiciel de certains fabricants. Plutôt que de vous montrer un chiffre faux, nous n'en montrons aucun. La batterie elle-même se porte probablement bien."
}
```

- [ ] **Step 5: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 6: Commit**

```bash
git add web/i18n tests/test_i18n.py
git commit -m "Add English and French message catalogues

All user-facing wording lives in flat JSON catalogues keyed by finding
id, severity and interface element. English is the default and the
fallback.

The tests make a missing translation a build failure rather than a hole
the user discovers: identical key sets across catalogues, matching
placeholders, no empty values, and every placeholder checked against
the parameters the probes actually emit."
```

---

### Task 14: Interface — Simple mode with locale switching

**Files:**
- Modify: `web/index.html`
- Create: `web/style.css`, `web/app.js`
- Create: `tests/test_web_assets.py`

**Interfaces:**
- Consumes: `/api/now`, `/api/stream`, `/static/i18n/<locale>.json`
- Produces: a self-contained page. `app.js` exposes `translate`, `render`, `setLocale` and `formatBytes` on `globalThis.healthConsole` so they are reachable for inspection.

**Why the front-end tests are written in Python:** this project has no build
chain and will never have one. The properties that matter here — no external
resource, no hard-coded user-facing string, colour never carrying meaning alone,
weight budget respected — are verifiable by inspecting the files, and that check
then runs in the same command as everything else.

- [ ] **Step 1: Write the failing tests**

File `tests/test_web_assets.py`:

```python
import json
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


class TestNoExternalResources(unittest.TestCase):
    """A diagnostic tool must work without Internet access."""

    def test_no_external_urls(self):
        for name in ("index.html", "style.css", "app.js"):
            for match in re.findall(r"https?://[^\s\"')]+", read(name)):
                self.fail(f"{name} references an external resource: {match}")

    def test_no_build_step_artefacts(self):
        self.assertFalse((WEB.parent / "package.json").exists())


class TestIndex(unittest.TestCase):
    def setUp(self):
        self.html = read("index.html")

    def test_default_language_is_english(self):
        self.assertIn('lang="en"', self.html)

    def test_has_a_viewport_for_phones(self):
        self.assertIn("viewport", self.html)

    def test_mode_switch_is_present(self):
        self.assertIn('id="mode-simple"', self.html)
        self.assertIn('id="mode-expert"', self.html)

    def test_language_selector_is_present(self):
        self.assertIn('id="locale"', self.html)

    def test_live_region_announces_changes(self):
        self.assertIn('aria-live="polite"', self.html)

    def test_scripts_and_styles_are_local(self):
        self.assertIn('href="/static/style.css"', self.html)
        self.assertIn('src="/static/app.js"', self.html)

    def test_no_hard_coded_aria_label(self):
        # An aria-label baked into the markup is a user-facing string that
        # cannot be translated: it must instead be set from the catalogue
        # at render time, like every other text node.
        match = re.search(r'aria-label="[^"]+"', self.html)
        self.assertIsNone(
            match, f"index.html hard-codes {match and match.group(0)}")

    def test_tabs_are_associated_with_their_panels(self):
        # role="tab" inside role="tablist" is not enough on its own: a
        # screen reader needs aria-controls/aria-labelledby to link each
        # tab to the panel it toggles. The association is bidirectional
        # or it is not an association: both directions are asserted.
        self.assertIn('aria-controls="simple"', self.html)
        self.assertIn('aria-controls="expert"', self.html)
        self.assertIn('aria-labelledby="mode-simple"', self.html)
        self.assertIn('aria-labelledby="mode-expert"', self.html)
        self.assertEqual(self.html.count('role="tabpanel"'), 2)


class TestStyle(unittest.TestCase):
    def setUp(self):
        self.css = read("style.css")

    def test_dark_theme_is_supported(self):
        self.assertIn("prefers-color-scheme", self.css)

    def test_reduced_motion_is_respected(self):
        self.assertIn("prefers-reduced-motion", self.css)

    def test_touch_targets_are_large_enough(self):
        self.assertIn("min-height: 44px", self.css)


class TestApp(unittest.TestCase):
    def setUp(self):
        self.js = read("app.js")

    def test_stays_within_budget(self):
        self.assertLess((WEB / "app.js").stat().st_size, 80 * 1024,
                        "80 KiB budget exceeded")

    def test_default_locale_is_english(self):
        self.assertIn('"en"', self.js)

    def test_default_mode_is_simple(self):
        self.assertIn('"simple"', self.js)

    def test_no_user_facing_string_is_hard_coded(self):
        # Every sentence the user reads must come from a catalogue. If any
        # English catalogue value appears verbatim in the code, a translation
        # would silently fail to apply.
        catalogue = json.loads(
            (WEB / "i18n" / "en.json").read_text(encoding="utf-8"))
        for key, value in catalogue.items():
            if key.endswith(".icon") or len(value) < 8:
                continue
            self.assertNotIn(
                value, self.js,
                f"app.js hard-codes the wording of {key}")

    def test_catalogue_keys_are_referenced_by_prefix(self):
        for prefix in ("finding.", "severity.", "verdict.", "ui."):
            self.assertIn(prefix, self.js)

    def test_locale_drives_intl_formatting(self):
        # Numbers and times must follow the active locale, not a fixed one.
        self.assertIn("Intl.NumberFormat", self.js)
        self.assertIn("Intl.DateTimeFormat", self.js)
        self.assertNotIn('Intl.NumberFormat("en"', self.js)

    def test_missing_key_falls_back_rather_than_showing_the_key(self):
        self.assertIn("FALLBACK_LOCALE", self.js)

    def test_freshness_stale_flag_is_not_hardcoded(self):
        # The original defect was markFreshness(state.ts * 1000, false) — a
        # literal false that a locale switch mid-outage silently repainted
        # as "up to date". Guard against regressing to any hardcoded
        # boolean argument.
        self.assertIn("let isStale", self.js)
        self.assertNotRegex(
            self.js, r"markFreshness\([^)]*\bfalse\b[^)]*\)",
            "markFreshness is called with a hardcoded false")
        self.assertIn("isStale = true", self.js)
        self.assertIn("isStale = false", self.js)

    def test_catalogue_fetch_is_defensive(self):
        # A missing or broken catalogue file must not silently blank the
        # page: the fetch is checked for failure and guarded by a try.
        self.assertIn("response.ok", self.js)
        self.assertRegex(
            self.js, r"try\s*\{[^}]*loadCatalogue\(",
            "loadCatalogue is not called inside a try block")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `FileNotFoundError: web/style.css`

- [ ] **Step 3: Write `web/index.html`**

Text nodes are intentionally empty: every one is filled from the catalogue at
render time, so no English wording is baked into the markup.

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Health Console</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header class="bar">
    <h1 id="app-title"></h1>
    <div class="controls">
      <div class="modes" id="mode-group" role="tablist">
        <button id="mode-simple" role="tab" aria-selected="true" aria-controls="simple"></button>
        <button id="mode-expert" role="tab" aria-selected="false" aria-controls="expert"></button>
      </div>
      <label class="locale-field">
        <span id="locale-label"></span>
        <select id="locale">
          <option value="en">English</option>
          <option value="fr">Français</option>
        </select>
      </label>
    </div>
  </header>

  <p id="freshness" class="freshness" aria-live="polite"></p>

  <main id="simple" class="view" role="tabpanel" aria-labelledby="mode-simple">
    <section class="verdict" aria-live="polite">
      <p class="verdict-state">
        <span id="verdict-icon" aria-hidden="true"></span>
        <span id="verdict-word"></span>
      </p>
      <p class="verdict-sentence" id="verdict-sentence"></p>
      <p class="verdict-score">
        <span id="score-label"></span>
        <strong id="score">—</strong><span aria-hidden="true">/100</span>
      </p>
    </section>
    <div id="findings"></div>
  </main>

  <main id="expert" class="view" role="tabpanel" aria-labelledby="mode-expert" hidden>
    <p id="expert-placeholder"></p>
    <pre id="raw"></pre>
  </main>

  <script type="module" src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 4: Write `web/style.css`**

```css
:root {
  color-scheme: light dark;
  --background: #f7f7f5;
  --card: #ffffff;
  --text: #1a1a1a;
  --muted: #5a5a5a;
  --border: #d8d8d4;
  --ok: #1c6b3a;
  --info: #2c5a8a;
  --attention: #8a5a00;
  --urgent: #a02020;
}

@media (prefers-color-scheme: dark) {
  :root {
    --background: #16181a; --card: #202325; --text: #ececec;
    --muted: #a8a8a8; --border: #34383b;
    --ok: #6cc48d; --info: #8bb8e8; --attention: #e0b060; --urgent: #f08080;
  }
}

* { box-sizing: border-box; }

body {
  margin: 0;
  font: 16px/1.5 system-ui, sans-serif;
  background: var(--background);
  color: var(--text);
}

.bar {
  display: flex; flex-wrap: wrap; gap: 1rem;
  align-items: center; justify-content: space-between;
  padding: 1rem 1.25rem; border-bottom: 1px solid var(--border);
}

.bar h1 { font-size: 1.1rem; margin: 0; }

.controls { display: flex; flex-wrap: wrap; gap: 1rem; align-items: center; }

.modes button, .locale-field select {
  min-height: 44px; min-width: 88px;
  border: 1px solid var(--border); background: var(--card);
  color: var(--text); border-radius: 8px; cursor: pointer; font: inherit;
}

.modes button[aria-selected="true"] {
  background: var(--text); color: var(--background);
}

.locale-field { display: flex; gap: 0.5rem; align-items: center; }
.locale-field span { color: var(--muted); font-size: 0.9rem; }

.freshness { margin: 0; padding: 0.5rem 1.25rem; color: var(--muted); }
.freshness.stale { color: var(--attention); font-weight: 600; }

.view { max-width: 46rem; margin: 0 auto; padding: 1.25rem; }

.verdict {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; padding: 1.75rem; text-align: center;
}

.verdict-state { font-size: 1.6rem; font-weight: 700; margin: 0 0 0.5rem; }
.verdict-sentence { margin: 0 0 1rem; color: var(--muted); }
.verdict-score { margin: 0; }

.finding {
  background: var(--card); border: 1px solid var(--border);
  border-left: 5px solid var(--border);
  border-radius: 12px; padding: 1rem 1.25rem; margin-top: 1rem;
}

.finding h2 { font-size: 1.05rem; margin: 0 0 0.35rem; }
.finding p { margin: 0; color: var(--muted); }
.finding .tag { font-weight: 700; font-size: 0.85rem; }

.severity-OK        { border-left-color: var(--ok); }
.severity-OK .tag { color: var(--ok); }
.severity-INFO      { border-left-color: var(--info); }
.severity-INFO .tag { color: var(--info); }
.severity-ATTENTION { border-left-color: var(--attention); }
.severity-ATTENTION .tag { color: var(--attention); }
.severity-URGENT    { border-left-color: var(--urgent); }
.severity-URGENT .tag { color: var(--urgent); }

body.stale .view { opacity: 0.45; }

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
```

- [ ] **Step 5: Write `web/app.js`**

```javascript
// Simple mode. No dependency, no outbound network, no wording in the code.

const FALLBACK_LOCALE = "en";
const AVAILABLE_LOCALES = ["en", "fr"];
const DEFAULT_MODE = "simple";
const STALE_AFTER_MS = 15000;

let catalogue = {};
let fallback = {};
let locale = FALLBACK_LOCALE;
let numberFormat = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
let timeFormat = new Intl.DateTimeFormat(locale,
  { hour: "2-digit", minute: "2-digit" });
let lastState = null;
let lastUpdate = Date.now();
let isStale = false;
let interfaceTextUnavailable = false;

function el(id) { return document.getElementById(id); }

function pickLocale() {
  const stored = localStorage.getItem("locale");
  if (stored && AVAILABLE_LOCALES.includes(stored)) return stored;
  for (const candidate of navigator.languages || []) {
    const short = candidate.split("-")[0];
    if (AVAILABLE_LOCALES.includes(short)) return short;
  }
  return FALLBACK_LOCALE;
}

export function translate(key, params = {}) {
  // A key missing from the active catalogue falls back to English rather than
  // showing the raw key to the user.
  let text = catalogue[key];
  if (text === undefined) {
    text = fallback[key];
    if (text === undefined) {
      console.warn(`missing catalogue key: ${key}`);
      return "";
    }
    console.warn(`key missing from ${locale}, using ${FALLBACK_LOCALE}: ${key}`);
  }
  return text.replace(/\{(\w+)\}/g, (whole, name) =>
    name in params ? formatValue(params[name]) : whole);
}

function formatValue(value) {
  return typeof value === "number" ? numberFormat.format(value) : String(value);
}

export function formatBytes(bytes) {
  const units = ["B", "kB", "MB", "GB", "TB"];
  let index = 0;
  let value = bytes;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${numberFormat.format(value)} ${units[index]}`;
}

async function loadCatalogue(target) {
  const response = await fetch(`/static/i18n/${target}.json`);
  if (!response.ok) {
    throw new Error(`catalogue unavailable: ${target} (${response.status})`);
  }
  return response.json();
}

export async function setLocale(target) {
  const requested = AVAILABLE_LOCALES.includes(target) ? target : FALLBACK_LOCALE;
  if (requested === FALLBACK_LOCALE) {
    catalogue = fallback;
    locale = FALLBACK_LOCALE;
  } else {
    try {
      catalogue = await loadCatalogue(requested);
      locale = requested;
    } catch (error) {
      // A broken non-default catalogue must not take the page down: fall
      // back to the already-loaded English catalogue instead of aborting.
      console.warn(`falling back to ${FALLBACK_LOCALE} after ${requested} failed to load`, error);
      catalogue = fallback;
      locale = FALLBACK_LOCALE;
    }
  }
  numberFormat = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
  timeFormat = new Intl.DateTimeFormat(locale,
    { hour: "2-digit", minute: "2-digit" });
  localStorage.setItem("locale", locale);
  document.documentElement.lang = locale;
  el("locale").value = locale;
  paintChrome();
  if (lastState) render(lastState);
}

function paintChrome() {
  document.title = translate("ui.title");
  el("app-title").textContent = translate("ui.title");
  el("mode-simple").textContent = translate("ui.mode.simple");
  el("mode-expert").textContent = translate("ui.mode.expert");
  el("mode-group").setAttribute("aria-label", translate("ui.mode.group"));
  el("locale-label").textContent = translate("ui.language");
  el("score-label").textContent = translate("ui.score.label");
  el("expert-placeholder").textContent = translate("ui.expert.placeholder");
}

function card(severity, titleText, whyText) {
  const article = document.createElement("article");
  article.className = `finding severity-${severity}`;
  const tag = document.createElement("p");
  tag.className = "tag";
  // Colour never carries the state alone: icon and word travel together.
  tag.textContent = `${translate(`severity.${severity}.icon`)} `
    + translate(`severity.${severity}.word`);
  const heading = document.createElement("h2");
  heading.textContent = titleText;
  const why = document.createElement("p");
  why.textContent = whyText;
  article.append(tag, heading, why);
  return article;
}

export function render(state) {
  lastState = state;
  const severity = state.severity || "OK";
  el("verdict-icon").textContent = translate(`severity.${severity}.icon`);
  el("verdict-word").textContent = translate(`severity.${severity}.word`);
  el("verdict-sentence").textContent = translate(`verdict.${severity}`);
  el("score").textContent = state.score;

  const host = el("findings");
  host.textContent = "";
  for (const finding of state.findings || []) {
    host.append(card(
      finding.severity,
      translate(`finding.${finding.id}.title`, finding.params),
      translate(`finding.${finding.id}.why`, finding.params)));
  }

  // An unavailable probe is shown as unavailable, never as a reassuring zero.
  // A probe whose evaluate() raised keeps status "ok" (the reading itself
  // succeeded) but carries eval_error, and that must not stay invisible.
  for (const [name, probe] of Object.entries(state.probes || {})) {
    if (probe.status === "ok" && !probe.eval_error) continue;
    host.append(card("INFO",
      translate("ui.probe.unavailable", { probe: name }),
      probe.reason || probe.eval_error || ""));
  }

  el("raw").textContent = JSON.stringify(state, null, 2);
  markFreshness(isStale);
}

function markFreshness(stale) {
  // While stale, the banner must keep showing the last real update time,
  // never the timestamp of whatever just got (re)painted — a locale
  // switch during an outage must repaint the stale message, not erase it.
  if (interfaceTextUnavailable) {
    // The catalogue never loaded, so translate() has nothing to return
    // but "". If the SSE stream still comes up despite that (a plausible
    // split: static assets down, the API up), a state event must not
    // silently blank out the one visible sign that something is wrong.
    return;
  }
  const zone = el("freshness");
  const milliseconds = stale ? lastUpdate : lastState.ts * 1000;
  const time = timeFormat.format(new Date(milliseconds));
  zone.classList.toggle("stale", stale);
  document.body.classList.toggle("stale", stale);
  zone.textContent = translate(
    stale ? "ui.freshness.stale" : "ui.freshness.live", { time });
}

function switchMode(mode) {
  const simple = mode === "simple";
  el("simple").hidden = !simple;
  el("expert").hidden = simple;
  el("mode-simple").setAttribute("aria-selected", String(simple));
  el("mode-expert").setAttribute("aria-selected", String(!simple));
  localStorage.setItem("mode", mode);
}

function connect() {
  const source = new EventSource("/api/stream");
  source.addEventListener("state", (event) => {
    lastUpdate = Date.now();
    isStale = false;
    render(JSON.parse(event.data));
  });
  source.addEventListener("error", () => {
    isStale = true;
    markFreshness(true);
  });
  setInterval(() => {
    if (Date.now() - lastUpdate > STALE_AFTER_MS) {
      isStale = true;
      markFreshness(true);
    }
  }, 5000);
}

async function start() {
  el("mode-simple").addEventListener("click", () => switchMode("simple"));
  el("mode-expert").addEventListener("click", () => switchMode("expert"));
  el("locale").addEventListener("change", (event) =>
    setLocale(event.target.value));
  switchMode(localStorage.getItem("mode") || DEFAULT_MODE);
  try {
    fallback = await loadCatalogue(FALLBACK_LOCALE);
    await setLocale(pickLocale());
    try {
      render(await (await fetch("/api/now")).json());
    } catch (error) {
      console.warn("initial state unavailable", error);
    }
  } catch (error) {
    // The catalogue itself is what failed here, so there is nothing left
    // to translate this sentence with: every other text node in
    // index.html starts empty, and without this literal sentence the
    // viewer would see a blank page with no sign anything is wrong. Do
    // not "fix" this back to a translate() call.
    console.warn("catalogue unavailable, interface text cannot be shown", error);
    interfaceTextUnavailable = true;
    el("freshness").textContent =
      "Interface text failed to load. Please reload the page.";
  }
  connect();
}

globalThis.healthConsole = { translate, render, setLocale, formatBytes };
start();
```

- [ ] **Step 6: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 7: Commit**

```bash
git add web tests/test_web_assets.py
git commit -m "Add the Simple mode interface with locale switching

Verdict in plain language, one card per finding, a Simple/Expert switch
and a language selector, both remembered. English is the default for
both.

Not one user-facing sentence lives in the markup or the code: every
text node is filled from the active catalogue, and a test fails if any
English wording is found hard-coded in app.js. A key missing from a
catalogue falls back to English rather than showing the raw key.

Every severity carries an icon and a word alongside its colour. Stale
data is dimmed and announced with its timestamp, and an unavailable
probe is shown as such rather than as a reassuring zero."
```

---

### Task 15: Command line and wiring up

**Files:**
- Create: `healthconsole/cli.py`, `bin/health-console`
- Create: `tests/test_cli.py`, `tests/test_smoke.py`

**Interfaces:**
- Consumes: everything above
- Produces: `main(argv: list[str] | None = None) -> int`, `cmd_run(cfg) -> int`, `cmd_config(cfg, config_path: Path) -> int`, `cmd_status(cfg) -> int`, `cmd_prune(cfg) -> int`, `default_db_path() -> Path`, `human_bytes(n: float) -> str`

- [ ] **Step 1: Write the failing tests**

File `tests/test_cli.py`:

```python
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

from healthconsole.cli import SECONDS_PER_DAY, _tick_once, human_bytes, main


class TestHumanBytes(unittest.TestCase):
    def test_scales_to_kilobytes(self):
        self.assertEqual(human_bytes(1536), "1.5 kB")

    def test_bytes_stay_bytes(self):
        self.assertEqual(human_bytes(512), "512 B")


class TestCommands(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(args))
        return code, out.getvalue()

    def test_no_command_shows_usage(self):
        code, output = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("usage", output.lower())

    def test_config_prints_effective_settings(self):
        code, output = self.run_cli("config")
        self.assertEqual(code, 0)
        self.assertIn("raw_days", output)
        self.assertIn("aggregate_days", output)

    def test_config_announces_projected_size(self):
        # The cost must be announced, not suffered.
        _, output = self.run_cli("config")
        self.assertIn("Projected size", output)

    def test_unknown_command_is_refused(self):
        code, _ = self.run_cli("teleport")
        self.assertEqual(code, 2)

    def test_config_announces_the_file_it_actually_read(self):
        # The command must never claim to have read the default path when
        # --config pointed it somewhere else.
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".toml", delete=False) as handle:
            handle.write('[server]\nport = 9999\n')
            path = handle.name
        try:
            code, output = self.run_cli("--config", path, "config")
        finally:
            os.unlink(path)
        self.assertEqual(code, 0)
        self.assertIn(path, output)
        self.assertIn("9999", output)

    def test_help_exits_zero(self):
        code, output = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("usage", output.lower())


class _FakeScheduler:
    """A scheduler stand-in that records calls and can be told to raise.

    Used only to exercise `_tick_once`'s error handling without starting a
    real server or touching a real database.
    """

    def __init__(self, raise_on=()):
        self.raise_on = set(raise_on)
        self.calls = []

    def tick(self, now):
        self.calls.append(("tick", now))
        if "tick" in self.raise_on:
            raise RuntimeError("tick failed")

    def flush(self, now):
        self.calls.append(("flush", now))
        if "flush" in self.raise_on:
            raise RuntimeError("flush failed")

    def maintain(self, now):
        self.calls.append(("maintain", now))
        if "maintain" in self.raise_on:
            raise RuntimeError("maintain failed")


def _fake_cfg(store_seconds=30):
    return SimpleNamespace(sampling=SimpleNamespace(store_seconds=store_seconds))


class TestTickOnce(unittest.TestCase):
    """`_tick_once` is the per-iteration body of `cmd_run`'s background
    loop, extracted so the collection loop's resilience to a raising probe,
    store or scheduler call can be tested without starting a real server.
    """

    def test_a_raising_tick_does_not_propagate(self):
        scheduler = _FakeScheduler(raise_on={"tick"})
        err = io.StringIO()
        with redirect_stderr(err):
            _tick_once(scheduler, _fake_cfg(), last_flush=0,
                       last_maintain=0, now=100)
        self.assertIn("RuntimeError", err.getvalue())

    def test_a_raising_flush_does_not_propagate(self):
        # A disk-full error surfacing from write_metrics is the realistic
        # case that motivated this: it must not kill the collection loop.
        scheduler = _FakeScheduler(raise_on={"flush"})
        err = io.StringIO()
        with redirect_stderr(err):
            _tick_once(scheduler, _fake_cfg(store_seconds=30), last_flush=0,
                       last_maintain=0, now=100)
        self.assertIn("RuntimeError", err.getvalue())

    def test_the_normal_path_advances_the_bookkeeping(self):
        scheduler = _FakeScheduler()
        last_flush, _ = _tick_once(
            scheduler, _fake_cfg(store_seconds=30),
            last_flush=0, last_maintain=0, now=100)
        self.assertIn(("flush", 100), scheduler.calls)
        self.assertEqual(last_flush, 100)

        scheduler = _FakeScheduler()
        _, last_maintain = _tick_once(
            scheduler, _fake_cfg(store_seconds=30),
            last_flush=0, last_maintain=0, now=SECONDS_PER_DAY + 1)
        self.assertIn(("maintain", SECONDS_PER_DAY + 1), scheduler.calls)
        self.assertEqual(last_maintain, SECONDS_PER_DAY + 1)

    def test_a_raising_tick_leaves_bookkeeping_unchanged(self):
        # A failure must not silently skip a flush window: if tick() blew
        # up, last_flush/last_maintain should come back exactly as given.
        scheduler = _FakeScheduler(raise_on={"tick"})
        err = io.StringIO()
        with redirect_stderr(err):
            last_flush, last_maintain = _tick_once(
                scheduler, _fake_cfg(store_seconds=30),
                last_flush=0, last_maintain=0, now=100)
        self.assertEqual(last_flush, 0)
        self.assertEqual(last_maintain, 0)
        self.assertNotIn(("flush", 100), scheduler.calls)


if __name__ == "__main__":
    unittest.main()
```

File `tests/test_smoke.py`:

```python
"""Smoke test: runs the real probes on this machine.

Unlike every other test, this one depends on the hardware.
"""

import math
import unittest

from healthconsole.probes import EvalContext, load_probes


class TestRealProbes(unittest.TestCase):
    def test_every_probe_collects_without_raising(self):
        for module in load_probes():
            with self.subTest(probe=module.NAME):
                sample = module.collect()
                self.assertIn(sample.get("status"),
                              {"ok", "unavailable", "incoherent"})

    def test_every_probe_evaluates_its_own_sample(self):
        ctx = EvalContext(sustained={}, cores=1)
        for module in load_probes():
            with self.subTest(probe=module.NAME):
                self.assertIsInstance(
                    module.evaluate(module.collect(), ctx), list)

    def test_metrics_are_all_finite_numbers(self):
        for module in load_probes():
            for key, value in module.metrics(module.collect()).items():
                with self.subTest(metric=key):
                    self.assertTrue(math.isfinite(value))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.cli'`

- [ ] **Step 3: Write `healthconsole/cli.py`**

```python
"""Command line interface.

Output here is operator-facing diagnostic text and stays in English: it is
compared, pasted into issues and grepped, so it is not part of the translated
interface.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from healthconsole.config import (
    DEFAULT_CONFIG_PATH, ConfigError, estimate_db_bytes, load_config,
)
from healthconsole.ring import Ring
from healthconsole.scheduler import Scheduler
from healthconsole.server import WEB_DIR, make_server
from healthconsole.store import Store

DATA_DIR = Path.home() / ".local" / "share" / "health-console"
WARN_DB_BYTES = 500 * 1024 ** 2
ASSUMED_METRIC_COUNT = 25
SECONDS_PER_DAY = 86_400


def default_db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "db.sqlite3"


def human_bytes(n: float) -> str:
    units = ["B", "kB", "MB", "GB", "TB"]
    index = 0
    while n >= 1024 and index < len(units) - 1:
        n /= 1024
        index += 1
    text = f"{n:.1f}".rstrip("0").rstrip(".")
    return f"{text} {units[index]}"


def _open(cfg):
    store = Store(default_db_path())
    ring = Ring(window_seconds=3600, live_seconds=cfg.sampling.live_seconds)
    return store, Scheduler(cfg, store, ring)


def cmd_config(cfg, config_path: Path) -> int:
    retention, sampling = cfg.retention, cfg.sampling
    print(f"Config file      : {config_path}")
    print(f"Listening on     : {cfg.bind}:{cfg.port}")
    print(f"Token            : {'set' if cfg.token else 'absent'}")
    print("Retention (days)")
    for name in ("raw_days", "aggregate_days", "snapshot_days",
                 "event_days", "audit_days"):
        print(f"  {name:<15} {getattr(retention, name)}")
    print(f"Cadences         : display {sampling.live_seconds}s · "
          f"write {sampling.store_seconds}s")
    projected = estimate_db_bytes(cfg, n_metrics=ASSUMED_METRIC_COUNT)
    print(f"Projected size   : {human_bytes(projected)} "
          f"(for {ASSUMED_METRIC_COUNT} metrics)")
    if projected > WARN_DB_BYTES:
        print("  Warning: these settings will produce a large database. "
              "Lower aggregate_days or raise store_seconds if that is not "
              "intended.")
    return 0


def cmd_status(cfg) -> int:
    # No Ring/Scheduler needed here: status only reads the store, so open
    # it directly rather than paying for a Scheduler (which loads probes).
    store = Store(default_db_path())
    try:
        now = int(time.time())
        print(f"Database         : {store.path}")
        print(f"Actual size      : {human_bytes(store.db_bytes())}")
        print(f"Metrics tracked  : {store.distinct_metric_count()}")
        for table in ("metric", "metric_5m"):
            depth = store.available_depth_seconds(table, now) / SECONDS_PER_DAY
            print(f"Depth {table:<12}: {depth:.2f} days "
                  f"({store.count_rows(table)} rows)")
    finally:
        store.close()
    return 0


def cmd_prune(cfg) -> int:
    store, scheduler = _open(cfg)
    try:
        report = scheduler.maintain()
        print(f"Aggregated       : {report['aggregated']} buckets")
        for table, count in report["deleted"].items():
            print(f"Pruned {table:<11}: {count} rows")
        store.vacuum()
        print(f"Size after       : {human_bytes(store.db_bytes())}")
    finally:
        store.close()
    return 0


def _log_loop_error(exc: Exception) -> None:
    print(f"scheduler loop error: {type(exc).__name__}: {exc}",
          file=sys.stderr)


def _tick_once(scheduler, cfg, last_flush, last_maintain, now=None):
    """One iteration of the background collection loop.

    Kept as a separate, importable function — rather than inlined in the
    closure below — so the loop's resilience to a raising probe, store or
    scheduler call can be exercised by a test without starting a real
    server or thread. This console's whole premise is that it does not
    quietly stop telling the truth, so that resilience is worth covering
    directly rather than only by reading the diff.
    """
    now = time.time() if now is None else now
    try:
        scheduler.tick(now)
        if now - last_flush >= cfg.sampling.store_seconds:
            scheduler.flush(now)
            last_flush = now
        if now - last_maintain >= SECONDS_PER_DAY:
            scheduler.maintain(now)
            last_maintain = now
    except Exception as exc:                  # noqa: BLE001
        # A transient disk or database error must not permanently stop
        # collection: log it and keep looping so the console recovers
        # once the condition clears, instead of leaving the server
        # answering with a state frozen at the last successful tick
        # forever.
        _log_loop_error(exc)
    return last_flush, last_maintain


def cmd_run(cfg) -> int:
    store, scheduler = _open(cfg)
    server = make_server(cfg, scheduler, WEB_DIR)
    stop = threading.Event()

    def loop():
        # Enforce retention once at startup, before entering the periodic
        # loop below. A machine that restarts daily (suspends, reboots, or
        # has its service restarted nightly) may never keep this thread
        # alive for the full 86400 seconds the periodic check waits for, so
        # without this call `store.prune()` could simply never run and the
        # database would grow unbounded.
        try:
            scheduler.maintain()
        except Exception as exc:                  # noqa: BLE001
            _log_loop_error(exc)
        last_flush = time.time()
        last_maintain = time.time()
        while not stop.is_set():
            last_flush, last_maintain = _tick_once(
                scheduler, cfg, last_flush, last_maintain)
            stop.wait(cfg.sampling.live_seconds)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    print(f"Health Console on http://{cfg.bind}:{cfg.port}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        stop.set()
        server.server_close()
        thread.join(timeout=5)
        store.close()
    return 0


COMMANDS = {"run": cmd_run, "config": cmd_config,
            "status": cmd_status, "prune": cmd_prune}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="health-console", description="System health console.")
    parser.add_argument("command", nargs="?", choices=sorted(COMMANDS),
                        help="run, config, status or prune")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to a configuration file")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # -h/--help exits 0 after printing the full help to stdout; an
        # invalid argument exits 2 after argparse has already printed usage
        # and the error to stderr. Either way argparse already wrote
        # everything needed, so do not print a second, duplicate usage line.
        return exc.code if exc.code else 0
    if args.command is None:
        parser.print_usage()
        return 2
    config_path = DEFAULT_CONFIG_PATH if args.config is None else args.config
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1
    if args.command == "config":
        return cmd_config(cfg, config_path)
    return COMMANDS[args.command](cfg)
```

- [ ] **Step 4: Write `bin/health-console`**

```bash
cat > bin/health-console <<'SH'
#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from healthconsole.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
SH
chmod +x bin/health-console
```

- [ ] **Step 5: Run the tests to confirm they pass**

Run: `./run-tests -v`
Expected: PASS — the whole suite is green

- [ ] **Step 6: Verify the foundation on the real machine**

```bash
./bin/health-console config
./bin/health-console status
./bin/health-console run   # then open http://127.0.0.1:8787, Ctrl+C to stop
```

Expected: `config` reports a projected size of roughly 32 MB; the page shows a
verdict, a score, and — on this machine specifically — a card saying the battery
readings cannot be trusted. Switching the selector to Français retranslates the
page without a reload and survives a refresh.

- [ ] **Step 7: Commit**

```bash
git add bin healthconsole/cli.py tests/test_cli.py tests/test_smoke.py
git commit -m "Add the command line and the smoke test

run, config, status and prune. config announces the database size the
settings imply; status reports the history depth actually available.

CLI output stays in English: it is operator-facing diagnostic text that
gets pasted into issues and grepped, not part of the translated
interface.

The smoke test runs this machine's real probes and checks that none
raises and that every metric produced is a finite number."
```

---

## What plan 1 does not do

Deliberately left to the following plans so this one stays executable and ships
working software:

- **Plan 2** — slow probes (storage, SMART, updates, services, journal, processes,
  OS info), regression-based trends, full Expert mode with history charts, report
  export.
- **Plan 3** — action catalogue, sudoers rule, execution lock, audit log, live
  output, systemd unit and the `install` command.
- **Found during review** — spec §13 requires a corrupt database to be recreated
  and the incident logged. Plan 1 does not handle it: `Store` lets the SQLite error
  propagate. Pick this up at the head of plan 2, before adding probes that write
  more.
