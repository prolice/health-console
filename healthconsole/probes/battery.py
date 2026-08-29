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
