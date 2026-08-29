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

import time
from dataclasses import asdict

from healthconsole import rules
from healthconsole.config import Config
from healthconsole.findings import Severity
from healthconsole.probes import FAST, EvalContext, load_probes
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
                samples[probe.NAME] = {"status": "unavailable",
                                       "reason": f"probe failed: {exc}"}

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
            except Exception:                     # noqa: BLE001
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
