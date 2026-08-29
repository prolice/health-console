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
