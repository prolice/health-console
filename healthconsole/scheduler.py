"""The two cadences and their orchestration.

The live view fills the in-memory ring; only an aggregated value reaches the
database. The scheduler is also the only component that knows the clock, which
is what keeps probes and rules purely functional.

Single-writer policy: within one process, `Store` serialises every access to
its shared SQLite connection on an internal `RLock` (see store.py's own
comment for why — concurrent readers alone were enough to corrupt a cursor,
with no second writer involved). That lock is what makes concurrent access
from this process's own threads safe; it is not what this policy is about.
The policy is a simplicity choice layered on top: the `Scheduler` remains the
ONLY component in this process that calls `write_metrics`, `aggregate_5m` or
`prune`. It cannot protect against a second OS process opening its own
`Store` against the same database file — that process holds its own separate
lock, so its writes could still race this one on `metric_key.key UNIQUE` and
raise IntegrityError. Do not read this as "reads need no locking" — they go
through the same lock as every write.
"""

from __future__ import annotations

import sys
import time
from dataclasses import asdict

from healthconsole import rules
from healthconsole.config import Config
from healthconsole.findings import Severity
from healthconsole.probes import (
    FAST, SLOW, EvalContext, describe_exception, load_probes, unavailable,
)
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
    # None, not a reassuring 0.0/100/"OK": no tick has happened yet, so there
    # is no measurement to report. A consumer that treats this as a real,
    # fresh, all-clear reading (the defect this guards against) must instead
    # recognise the absence and say so -- see web/app.js's hasMeasurement().
    "ts": None, "score": None, "breakdown": [], "severity": None,
    "findings": [], "probes": {}, "depth_days": 0.0,
}


class Scheduler:
    """Ties probes, ring, hysteresis and store together on a single clock.

    The Scheduler is the only background writer to the Store: `write_metrics`,
    `aggregate_5m` and `prune` must never be called from anywhere else. This
    is a design policy, not a safety requirement — within one process, `Store`
    locks every access, writer or reader, on a single `RLock`, so a second
    writer sharing this Store would not corrupt anything. What the policy
    guards against is a second OS process opening its own `Store` against the
    same database file: that process holds its own separate lock, so its
    writes could still race this one on `metric_key.key UNIQUE` and raise
    IntegrityError (see module docstring above).
    """

    def __init__(self, cfg: Config, store: Store, ring: Ring,
                 probes=None, clock=time.time) -> None:
        self.cfg = cfg
        self.store = store
        self.ring = ring
        self.clock = clock
        if probes is None:
            self.fast_probes = load_probes(FAST)
            self.slow_probes = load_probes(SLOW)
            self.probes = [*self.fast_probes, *self.slow_probes]
        else:
            self.fast_probes = probes
            self.slow_probes = []
            self.probes = probes
        self.tracker = HysteresisTracker()
        self._last_flush: float | None = None
        self._last_slow: float | None = None
        self._slow_samples: dict[str, dict] = {}
        self._net_previous: dict = {}
        self._net_previous_ts: float | None = None
        self._state: dict = dict(EMPTY_STATE)
        # available_depth_seconds() is a full-table MIN(ts) scan the
        # (key_id, ts) index cannot serve -- measured at ~29ms on two days
        # of data, which tick()'s 2s budget cannot absorb (~1.4% of a core
        # continuously, for a field the front end does not even read).
        # Computed once here so a fresh process does not under-report the
        # depth actually on disk, then refreshed only on the much rarer
        # write cadence, in flush().
        self._depth_days_cached: float = self._compute_depth_days()

    # --- fast cadence -------------------------------------------------

    def tick(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        samples: dict[str, dict] = {}
        measurements: dict[str, float] = {}

        for probe in self.fast_probes:
            try:
                sample = probe.collect()
                samples[probe.NAME] = sample
                measurements.update(probe.metrics(sample))
            except Exception as exc:              # noqa: BLE001
                # A failing probe never brings the others down.
                samples[probe.NAME] = unavailable(
                    f"probe failed: {describe_exception(exc)}")

        if self._last_slow is None or now - self._last_slow >= 300:
            self._last_slow = now
            for probe in self.slow_probes:
                try:
                    sample = probe.collect()
                    self._slow_samples[probe.NAME] = sample
                    measurements.update(probe.metrics(sample))
                except Exception as exc:          # noqa: BLE001
                    self._slow_samples[probe.NAME] = unavailable(
                        f"probe failed: {describe_exception(exc)}")
        samples.update(self._slow_samples)

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
                    sample["eval_error"] = describe_exception(exc)
                print(f"{probe.NAME}: evaluate() failed: "
                      f"{describe_exception(exc)}", file=sys.stderr)
                continue

        self._state = {
            "ts": now,
            "score": score(findings),
            "breakdown": [list(pair) for pair in score_breakdown(findings)],
            "severity": worst(findings).name,
            "findings": [self._serialise(finding) for finding in findings],
            "probes": samples,
            "depth_days": self._depth_days_cached,
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
        sample["rates"] = rates
        return rates

    @staticmethod
    def _serialise(finding) -> dict:
        data = asdict(finding)
        data["severity"] = Severity(finding.severity).name
        return data

    def _compute_depth_days(self, now: float | None = None) -> float:
        now = self.clock() if now is None else now
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
        # Refreshed on every flush, whether or not this window had a point
        # to write: depth still needs to track a prune that happened since
        # the last flush, and the write cadence (default 30s) is cheap
        # enough for the MIN(ts) scan that tick()'s 2s cadence is not.
        self._depth_days_cached = self._compute_depth_days(now)
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
