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
    "apt.upgrade": Action(
        id="apt.upgrade",
        argv=("/usr/bin/apt-get", "-y", "upgrade"),
        root=True,
        risk=Risk.MEDIUM,
    ),
    "apt.security": Action(
        id="apt.security",
        argv=("/usr/bin/unattended-upgrade",),
        root=True,
        risk=Risk.MEDIUM,
    ),
    "clean.aptcache": Action(
        id="clean.aptcache",
        argv=("/usr/bin/apt-get", "clean"),
        root=True,
        risk=Risk.SAFE,
    ),
}

ACTION_IDS: frozenset[str] = frozenset(CATALOGUE)


def lookup(action_id: str) -> Action:
    if not isinstance(action_id, str):
        # A caller handing this a list, dict or other unhashable value
        # would otherwise hit CATALOGUE[action_id] and raise a bare
        # TypeError, breaking the published contract that any bad
        # identifier -- not just an unknown one -- raises UnknownAction.
        raise UnknownAction(action_id)
    try:
        return CATALOGUE[action_id]
    except KeyError:
        raise UnknownAction(action_id) from None
