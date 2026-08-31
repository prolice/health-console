"""Pending package updates known to APT without contacting the network."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from healthconsole.findings import Finding, Severity
from healthconsole.plausibility import sane
from healthconsole.probes import SLOW, EvalContext, describe_exception, unavailable

NAME = "updates"
CADENCE = SLOW
APT_CHECK = Path("/usr/lib/update-notifier/apt-check")
SECURITY_HINTS = ("security", "-security")


def _apt_check() -> tuple[int, int] | None:
    if not APT_CHECK.exists():
        return None
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    result = subprocess.run(
        [str(APT_CHECK)],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
        check=False,
    )
    text = (result.stdout or result.stderr).strip()
    if ";" not in text:
        return None
    pending, security = text.split(";", 1)
    return int(pending), int(security)


def _apt_list_counts() -> tuple[int, int]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    result = subprocess.run(
        ["/usr/bin/apt", "list", "--upgradable"],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        check=False,
    )
    if result.returncode not in (0, 100):
        raise RuntimeError((result.stderr or result.stdout).strip())
    pending = 0
    security = 0
    for line in result.stdout.splitlines():
        if not line or line.startswith("Listing..."):
            continue
        pending += 1
        if any(hint in line.lower() for hint in SECURITY_HINTS):
            security += 1
    return pending, security


def collect() -> dict:
    try:
        counts = _apt_check()
        if counts is None:
            counts = _apt_list_counts()
        pending, security = counts
        return {
            "status": "ok",
            "pending_count": pending,
            "security_count": security,
        }
    except Exception as exc:                      # noqa: BLE001
        return unavailable(
            f"cannot read pending updates: {describe_exception(exc)}")


def metrics(sample: dict) -> dict[str, float]:
    if sample.get("status") != "ok":
        return {}
    series: dict[str, float] = {}
    for key, field_name in (
        ("updates.pending", "pending_count"),
        ("updates.security", "security_count"),
    ):
        value = sane("count", sample.get(field_name))
        if value is not None:
            series[key] = float(value)
    return series


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    if sample.get("status") != "ok":
        return []
    pending = sane("count", sample.get("pending_count"))
    security = sane("count", sample.get("security_count"))
    if pending is None or security is None:
        return []
    if security > 0:
        return [Finding(
            id="updates.security_pending",
            severity=Severity.ATTENTION,
            params={"count": int(security)},
            detail=f"security_count={int(security)} pending_count={int(pending)}",
            action="apt.security",
        )]
    if pending > 0:
        return [Finding(
            id="updates.pending",
            severity=Severity.INFO,
            params={"count": int(pending)},
            detail=f"pending_count={int(pending)}",
            action="apt.upgrade",
        )]
    return []
