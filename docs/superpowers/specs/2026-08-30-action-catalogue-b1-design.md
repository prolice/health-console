# Action Catalogue, slice B1 — Design Document

- **Date:** 2026-08-30
- **Status:** design approved, ready for implementation planning
- **Scope:** the execution machinery only, carrying one privileged action.
- **Refines:** §8 of
  [`2026-08-29-health-console-design.md`](2026-08-29-health-console-design.md).
  That section still describes the destination; this one describes the first
  slice and corrects what turned out to be unbuildable.
- **Project language:** English — code, comments, documentation and commit
  messages. The interface ships in English and French.

## 1. Why this is a slice and not the whole catalogue

§8 declares thirteen actions. **Eleven of them cannot be built**, because they
depend on probes that were designed and never written. The probes that exist are
`cpu`, `memory`, `thermal`, `network` and `battery`; `storage`, `smart`,
`updates`, `services`, `journal`, `processes` and `osinfo` do not.

| Action | Needs | Available |
|---|---|---|
| `apt.refresh` | nothing | yes |
| `report.export` | the report generator, unwritten | no |
| `apt.upgrade`, `apt.security` | an `updates` probe for the package count | no |
| `clean.autoremove`, `clean.aptcache` | reclaimable space to announce | no |
| `clean.snaps` | a `snap` probe for disabled revisions | no |
| `clean.journal` | a `journal` probe | no |
| `svc.restart` | a `services` probe for the failed-unit list | no |
| `sys.reboot`, `sys.poweroff` | reboot-required detection | no |
| `disk.selftest`, `disk.trim` | `smart` / `storage` probes | no |

This is not a matter of effort. Two of §8's own guard rails are the blockers:

- §8.3 requires the parameter of `svc.restart` and `clean.snaps` to be checked
  against **a list the server rebuilds at execution time**. With no probe there
  is no list, so the parameter cannot be validated — and those are the only two
  parameterised actions, meaning the only two with an injection surface.
- §8.2 requires cleanup actions to announce the space they will reclaim
  **before** offering the button, and `sys.reboot`/`sys.poweroff` to appear only
  when a reboot is required. Both gates read data that does not exist.

**B1 therefore builds the machinery and one action.** Later slices each add a
probe together with the actions it enables.

## 2. Approved decisions

| Topic | Decision |
|---|---|
| Scope | Execution machinery + `apt.refresh` only |
| Parameterised actions | **None.** Injection surface is nil by construction, not by validation |
| Execution model | `POST` returns a `run_id`; output streams over the existing SSE channel |
| Privileges | The console generates the `sudoers` rule; **the user installs it**. The console never runs as root |
| Surface | A third tab, "Actions", holding the action list and the audit log |
| Confirmation | Required for every action in B1, including the safe one |

### 2.1 Why the console never touches `/etc`

§8.5 reads as though an installer writes the rule after confirmation. B1 does
not. There is a single command, `health-console sudoers`, and it has exactly one
mode: it renders the file into `packaging/sudoers.d/`, validates it with
`visudo -c` on a temporary copy, prints the exact content and destination, and
hands the operator the one command to run. **There is no flag that makes it
write to `/etc`** — an install mode that exists but is not used is still an
install mode someone will use. The privileged write stays the
operator's own act, and the console needs no root path at all — not even one it
declines to use.

### 2.2 Why a third tab, and what it must not become

Simple mode is "the verdict, then one card per finding, each with its action
button where one exists. Nothing else" (§10.1). `apt.refresh` corrects no
finding, so it has no card to sit on, and a maintenance button offered to a
non-technical reader whom nothing has told they need it would be the first
breach of that rule.

The audit log also needs a home a person can find; behind an Expert accordion it
would never be read.

**The tab is for maintenance actions and for the audit log. It is not where
action buttons go.** When the probes land, an action that fixes a specific
finding belongs **on that finding's card in Simple mode**. "Corrective actions
one click away" means nothing if the click is in another tab.

## 3. The engine

Two files, deliberately separate.

### 3.1 `healthconsole/actions.py` — data, no logic

```python
class Risk(Enum):
    SAFE = "safe"
    MEDIUM = "medium"
    SENSITIVE = "sensitive"

@dataclass(frozen=True)
class Action:
    id: str
    argv: tuple[str, ...]
    root: bool
    risk: Risk

CATALOGUE: dict[str, Action] = {
    "apt.refresh": Action(
        id="apt.refresh",
        argv=("/usr/bin/apt-get", "update"),
        root=True,
        risk=Risk.SAFE,
    ),
}
```

The browser sends **an identifier**. `argv` is a frozen tuple, `shell=False`, and
no text from the network enters the argument list. That is what separates an
action catalogue from a remote shell.

This file must stay readable in thirty seconds by someone checking what the
console can run on their machine. It is the file re-read on every addition, and
it must never grow logic.

### 3.2 `healthconsole/runner.py` — execution

| Guard rail | Behaviour |
|---|---|
| Single lock | One action at a time, process-wide. A second `POST` gets `409 action_busy`. |
| Hard timeout | 30 minutes, then `terminate()`, then `kill()`. Recorded as a failure. |
| Audit | One `action_run` row on completion: timestamp, action id, source, exit code, duration, full output. Written on success, on failure **and on timeout**. `source` is the requesting client's IP address, so an action run from a phone on the LAN is distinguishable afterwards from one run at the keyboard. |
| Live output | Read line by line, pushed to SSE and accumulated for the audit row. |
| Off-loopback | `POST` refused unless `allow_remote_actions = true`. The catalogue `GET` stays open. |

The `action_run` table and its retention (`audit_days`, default 365) already
exist in `store.py` and need no schema change.

## 4. HTTP surface

| Route | Method | Purpose |
|---|---|---|
| `/api/actions` | GET | The catalogue available in the current state |
| `/api/actions/<id>` | POST | Run it; `202` with a `run_id` |
| `/api/actions/runs` | GET | The audit log, most recent first |

Refusals: `401` without a token, `403` off-loopback without
`allow_remote_actions`, `404` for an unknown id, `409 action_busy`, `503` during
shutdown.

`GET /api/actions` returns one entry per action:

```json
[{"id": "apt.refresh", "risk": "safe", "available": true}]
```

`available` is always `true` in B1 — every gate that could make it false
(reboot-required, reclaimable space, a failed-unit list) reads probe data that
does not exist yet. The field ships anyway, because the front end must be built
to honour it from the start rather than retrofitted when the first gated action
arrives.

Responses carry ids and risk levels, **never prose** — the browser localises, as
it already does for findings.

SSE `action` events on the existing stream:

```json
{"run_id": "...", "action_id": "apt.refresh",
 "phase": "started" | "output" | "finished",
 "line": "...", "exit_code": 0, "duration_ms": 2140}
```

## 5. The real cost of this chantier

A `sudoers` `NOPASSWD` rule is not granted to the console. **It is granted to a
user account.** Once installed, *any* process running as that user can run that
exact command without a password — a script, a browser extension, a compromised
dependency — not only this console.

For `apt-get update` the consequence is negligible. But the mechanism does not
change as the catalogue grows: the day the rule carries `shutdown -r`, anything
running as that account can reboot the machine unprompted. **This is the true
cost of the action catalogue, and §8 does not state it.**

It argues for what the design document already anticipated and the project does
not yet have: a **dedicated system user** for the service, with the rule scoped
to it rather than to the operator's login account. B1 does not build that. A
rule naming your login account is a **transitional state, not the destination**,
and the packaging must say so in a comment inside the file itself, where whoever
reads `/etc/sudoers.d/health-console` in two years will see it.

## 6. Interface — the Actions tab

Three tabs now, so three `role="tabpanel"` elements — the existing test asserting
exactly two must be updated, not deleted.

- **Action list** — label, risk badge carrying an icon **and** a word (colour
  never alone), and the button.
- **Confirmation dialogue** carrying the consequence in plain language.
- **Live output pane** fed by the SSE `action` events.
- **Audit log** — timestamp, action, source, exit code, duration.

### 6.1 Confirmation applies to every action in B1

§8.4 requires explicit confirmation without qualifying it by risk. Gating it on
risk would be defensible UX — a dialogue over `apt-get update` is heavy — but
B1's only action is `SAFE`, so a risk gate would mean the dialogue ships having
never been exercised. Mislabelling the action's risk to exercise the path would
be worse: the catalogue's risk levels have to mean what they say.

So: confirmation for everything in B1, and this decision is reopened when the
catalogue holds actions at more than one risk level.

## 7. Packaging

`packaging/sudoers.d/health-console`, mode 0440, one entry:

```
# Transitional: this names a login account. The destination is a dedicated
# system user for the service — see the B1 design document, §5.
<user> ALL=(root) NOPASSWD: /usr/bin/apt-get update
```

`<user>` is substituted with the account running the command, resolved at render
time — it is never a literal in the shipped file. Fixed arguments, **no wildcard,
never `ALL`**.

`health-console sudoers` renders the file, checks it with `visudo -c` on a
temporary copy, refuses to print anything if that check fails, and ends with the
exact command for the operator to run:

```
sudo install -m 0440 -o root -g root \
  packaging/sudoers.d/health-console /etc/sudoers.d/health-console
```

## 8. Testing

- **The catalogue is data**: every `argv` is a tuple, contains no shell
  metacharacter, and every id has catalogue entries in both locales — the same
  mechanism that already protects finding ids.
- **The runner** is tested with harmless real commands (`/bin/true`,
  `/bin/false`, `sleep`): the lock refuses a second run, the timeout fires and
  kills, and an `action_run` row is written in **all three** outcomes — success,
  failure, timeout.
- **The routes** are tested on each refusal path separately: unknown id, busy,
  off-loopback, missing token.
- **The sudoers file** must pass `visudo -c` from within the suite itself.

## 9. Out of scope

Parameterised actions. Any probe. `report.export` and the report generator. A
systemd unit. A dedicated system user. The other twelve actions of §8.
