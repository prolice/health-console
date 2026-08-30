# Health Console — Design Document

- **Date:** 2026-08-29
- **Target:** Ubuntu 26.04 LTS "Resolute Raccoon", kernel 7.0, single workstation (HP laptop)
- **Status:** design approved, ready for implementation planning
- **Project language:** English — code, comments, documentation and commit messages.
  The user interface is translatable and ships with English and French (§11).

## 1. Goal

A local web console that reports the health of the machine, the OS and the
hardware through two deliberate readings:

- **Simple mode** — a verdict in plain language, understandable without
  technical background, with corrective actions one click away.
- **Expert mode** — the full control room: raw metrics, history charts,
  tables, audit log.

The value of this project is the **interpretation** of measurements, not their
collection. Displaying `77 °C` is trivial; saying whether that is bad is the
product.

## 2. Approved decisions

| Topic | Decision |
|---|---|
| Usage | Live dashboard **and** long-term history |
| Scope | Core vitals, hardware health, OS & maintenance, network & processes |
| Privileges | Narrow `sudoers.d` rule, service runs as an unprivileged user |
| Presentation | Two explicit modes, Simple / Expert switch |
| Network | Listens on the LAN, protected by a token |
| Stack | Python stdlib + `psutil` (apt) + SQLite; vanilla front end, no build step |
| Actions | Closed catalogue: updates, cleanup, services & system, disk & diagnostics |
| Language | English by default, French available, catalogue-driven (§11) |

## 3. Platform constraints (measured on 2026-08-29)

These measurements are not decoration: each one constrains a decision.

- **PEP 668** — `/usr/lib/python3.14/EXTERNALLY-MANAGED` is present. `pip install`
  into the system Python is refused. `psutil` 7.1.0 and `jinja2` are available as
  apt packages; Flask, FastAPI and uvicorn are not. → **No dependency outside apt,
  no virtualenv.**
- **Resources** — 4 cores, 5.2 GB of RAM with ~1.5 GB actually available.
  → **Budget: < 60 MB RSS for the service, < 2 % CPU on average, default database
  ≈ 54 MB.** A health tool that degrades the health of the machine is a design
  failure. This budget is what forces the display cadence (2 s, in memory) apart
  from the write cadence (30 s, to disk) — see §6.1.
- **Hardware** — Crucial MX300 489 GB SSD (`/dev/sda`), AMD Radeon HD 6730M GPU,
  `BAT0` battery, Logitech mouse with its own battery (`hidpp_battery_0`).
- **Sensors** — `lm-sensors` is absent, but `/sys/class/hwmon` exposes `coretemp`,
  `acpitz`, `radeon`, `hp`, `BAT0`, `AC`. → **Read sysfs directly, no dependency on
  `sensors`.**
- **Battery** — `BAT0` does **not** expose `energy_full`; it uses the `charge_*`
  family. → **Probes read both conventions**, otherwise the battery would show as
  "unavailable" on this exact machine.
- **This machine's battery driver reports incoherent values**: `charge_full = 1000`,
  `charge_full_design = 1000`, yet `charge_now = 467000` and `capacity = 46700`
  (where `capacity` is a 0-100 percentage). A naive computation would announce
  "0 % wear, charged to 46,700 %" — a lie stated with confidence.
  → **Plausibility checking is mandatory, see §7.6.**
- **Snaps** — ~39 `loop*` mounts, `/var/lib/snapd` at 4.7 GB, 13 disabled revisions.
  → **Raw `df` is unreadable for a human: squashfs mounts are filtered out**, and
  snap cleanup is a real win.
- **`unattended-upgrades` is enabled.** → The updates probe must separate what will
  be installed automatically from what genuinely needs the user, otherwise it
  demands an action that is already handled.
- **`Linger=no`** — a user service stops at logout. → Installation offers
  `loginctl enable-linger` for uninterrupted history.
- **sudo requires a password** — hence the narrow `sudoers.d` rule.

### Reference state at design time

Root filesystem 9 % full (40 GB of 481 GB) · 0 failed services · 17 pending updates ·
59 journal errors over 24 h · ~5.3 GB reclaimable (snaps 4.7 GB, APT cache 475 MB,
`/var/log` 177 MB) · CPU package 77 °C · load 0.62.

## 4. Architecture

### 4.1 The structuring principle: two cadences

Reading `/proc` costs microseconds; running `apt list --upgradable` or `smartctl`
costs hundreds of milliseconds and wakes the disk. Conflating them would wreck the
resource budget.

- **Fast cadence — 2 s**: CPU, memory, temperatures, network throughput, load,
  battery. Pure reads from `/proc` and `/sys`.
- **Slow cadence — 5 min**: SMART, APT updates, systemd services, journal, partition
  usage, processes, battery wear, OS information.

### 4.2 Independent probes

Every domain is a module exposing the same interface:

```python
NAME    = "thermal"
CADENCE = FAST
def collect() -> dict                            # raw measurement, no judgement
def evaluate(sample, ctx) -> list[Finding]       # pure function
```

**Strict separation of measurement and judgement.** `collect()` produces only
numbers; `evaluate()` reasons only about numbers and touches neither the system nor
the clock. All verdict logic is therefore testable without hardware, from frozen
samples — the property that makes Simple mode verifiable.

**Fault isolation.** A probe that fails (SMART without privileges, no battery, `apt`
locked) returns `{"status": "unavailable", "reason": ...}` and affects no other
probe. A diagnostic tool that crashes when something is wrong is worse than useless.

### 4.3 Data flow

```
  /proc /sys      ┌──────────────┐        ┌──────────┐
  smartctl   ───► │  scheduler   │ ─────► │  SQLite  │
  apt systemd     │  2 s / 5 min │        └────┬─────┘
                  └──────┬───────┘             │
                         │ current state       │ history
                         ▼                     ▼
                  ┌───────────────────────────────┐
                  │  HTTP server (ThreadingHTTP)  │
                  │  /api/now /api/history        │
                  │  /api/stream (SSE) /api/actions│
                  └───────────────┬───────────────┘
                                  ▼
                    browser — Simple / Expert, en / fr
```

Live updates use **SSE** rather than WebSocket: the stream is one-way, SSE fits in
about thirty lines on `http.server`, and it reconnects on its own. Capped at 8
concurrent streams (one thread each); beyond that the client falls back to polling
every 5 s.

## 5. Probe catalogue

| Probe | Cadence | Source | When unavailable |
|---|---|---|---|
| `cpu` | fast | `psutil`, `/proc/stat`, `/proc/cpuinfo` | — |
| `memory` | fast | `psutil`, `/proc/meminfo` | — |
| `thermal` | fast | `/sys/class/hwmon/*` (coretemp, acpitz, radeon, hp) | zone hidden |
| `network` | fast | `psutil.net_io_counters`, `psutil.net_if_addrs` | — |
| `battery` | fast | `/sys/class/power_supply/*` — `energy_*` **and** `charge_*`, plausibility-checked (§7.6) | card hidden (desktop); implausible values → "incoherent (driver)" |
| `storage` | slow | `psutil.disk_partitions` filtered (squashfs/tmpfs excluded), targeted `du` | — |
| `smart` | slow | `sudo smartctl --json -a /dev/sda` | "SMART locked" + the command to enable it |
| `updates` | slow | `apt-get -s dist-upgrade`, `/var/run/reboot-required`, `unattended-upgrades` state | "apt lock busy, retrying" |
| `services` | slow | `systemctl --failed --output=json` | — |
| `journal` | slow | `journalctl -p err -S -24h -o json`, grouped | — |
| `processes` | slow | `psutil.process_iter`, top 10 by CPU / RSS | — |
| `osinfo` | slow | `/etc/os-release`, `uname`, uptime, support horizon | — |

## 6. Storage and retention

SQLite in WAL mode, `synchronous=NORMAL`, at
`~/.local/share/health-console/db.sqlite3`.

### 6.1 Displaying finely is not the same as keeping long

These are two distinct needs, and conflating them makes the database explode. A
sparkline over the last 60 minutes needs a point every 2 seconds; a 90-day trend
needs nothing of the sort.

- **In-memory ring buffer** — 2 s resolution over a rolling 60 minutes. It feeds the
  live view and the sparklines. Measured (`tracemalloc`, 1,800 points × ~25
  metrics stored as `(float, float)` tuples in a deque — CPython object overhead
  dominates over the 8-byte C doubles an earlier, unmeasured estimate assumed):
  ≈ **5.3 MB of RAM**. Nothing is written to disk at that cadence.
- **Database** — one write every 30 s (`store_seconds`), aggregated from the ring
  (average, min, max). More than enough for history, and it **divides the volume by
  15**.

### 6.2 Schema

Metric keys are **normalised to integers**: storing the string
`"net.enp0s25.rx_bps"` on every row would cost more than the measurement itself.

```sql
CREATE TABLE metric_key (id INTEGER PRIMARY KEY, key TEXT UNIQUE);
CREATE TABLE metric     (ts INTEGER, key_id INTEGER, avg REAL, min REAL, max REAL);
CREATE TABLE metric_5m  (ts INTEGER, key_id INTEGER, avg REAL, min REAL, max REAL);
CREATE TABLE snapshot   (ts INTEGER, probe TEXT, json TEXT);
CREATE TABLE event      (id INTEGER PRIMARY KEY, finding_id TEXT, severity TEXT,
                         opened_ts INTEGER, closed_ts INTEGER);
CREATE TABLE action_run (id TEXT PRIMARY KEY, ts INTEGER, action_id TEXT,
                         source TEXT, exit_code INTEGER, duration_ms INTEGER,
                         output TEXT);
CREATE INDEX metric_key_ts ON metric(key_id, ts);
CREATE INDEX metric_5m_key_ts ON metric_5m(key_id, ts);
```

### 6.3 Configurable retention

Every retention period is **expressed in days** and set in
`~/.config/health-console/config.toml`:

```toml
[retention]
raw_days       = 2      # fine-grained samples (30 s step)
aggregate_days = 90     # 5-minute averages — this is what carries the trends
snapshot_days  = 7      # hourly structured states
event_days     = 365    # opened/closed incidents — the memory of past failures
audit_days     = 365    # log of executed actions

[sampling]
live_seconds   = 2      # screen refresh, memory only
store_seconds  = 30     # database write
```

**Validated at startup**, with an explicit refusal rather than surprising behaviour:

- every period is an integer ≥ 1 day;
- `raw_days <= aggregate_days` — keeping fine-grained data longer than the averages
  makes no sense and would betray a typo;
- `store_seconds` must be a multiple of `live_seconds` and ≤ 300;
- an invalid value stops the service with a message naming the offending field and
  the expected value. A service that starts while silently ignoring a broken
  configuration is a trap.

### 6.4 Cost announced, not suffered

The service **computes and displays the projected size** at startup and in
`health-console status`, based on the number of metrics actually collected on this
machine:

```
rows/day  = 86400 / store_seconds × metric_count
size      ≈ raw_days × 4.9 MB  +  aggregate_days × 0.49 MB  +  ~3 MB (rest)
```

using **68 bytes per metric row** (`BYTES_PER_METRIC_ROW`, measured on 500,000
rows — not the 40-byte guess an earlier draft of this section used, which
understated the total by about 45%). With the defaults on this machine
(~25 metrics): **≈ 54 MB**.

Beyond 500 MB projected, startup prints an explicit warning naming the estimate and
the setting responsible — **but does not block**: it is the user's machine and the
user's disk; they should be informed, not prevented.

### 6.5 What changing retention does

This must be stated plainly, because intuition misleads:

- **Lowering** a period purges the excess at the next daily cycle, or immediately
  via `health-console prune`.
- **Raising** a period **resurrects nothing**. History restarts from the date of the
  change. The console therefore always displays the depth *actually available*,
  never the one requested in configuration — otherwise a half-empty "90 days" chart
  would suggest a collection failure.

Pruning and aggregation run daily, `VACUUM` weekly. If free disk space drops below
1 GB, writes stop cleanly and a finding says so: the console must never be the cause
of the filling it reports.

### 6.6 Metric naming

`cpu.usage`, `cpu.freq`, `cpu.temp.pkg`, `mem.available`, `mem.swap.used`, `load.1`,
`disk.sda2.used_pct`, `net.enp0s25.rx_bps`, `thermal.<zone>`, `battery.charge_pct`,
`battery.wear_pct`.

## 7. Verdict engine

### 7.1 The finding: unit of interpretation

A finding carries **an identifier and parameters, never a sentence**. Baking English
prose into a probe would make the console untranslatable without duplicating every
probe; the wording lives in the message catalogues (§11).

```python
Finding(
    id="storage.reclaimable",
    severity=Severity.INFO,
    params={"reclaimable_bytes": 5_690_000_000, "snap_revisions": 13},
    detail="snapd 4.7G (13 disabled rev.) · apt archives 475M · /var/log 177M",
    action="clean.all",
)
```

- `id` selects the message in the active catalogue and is stable forever — it is a
  contract, not a label.
- `params` are numbers and identifiers the catalogue interpolates, formatted by
  `Intl` in the active locale.
- `detail` is the raw technical string for Expert mode. It carries measured values
  and unit symbols, is deliberately **not translated**, and is never shown in Simple
  mode.
- `action` references an entry in the action catalogue (§8), or is `None`.

Severities: `OK`, `INFO`, `ATTENTION`, `URGENT`.

**Every emittable finding id is declared in a single registry** (`FINDING_IDS`).
Constructing a `Finding` with an unregistered id raises. This is what makes
catalogue completeness testable (§11.4).

### 7.2 Explainable score

Starts at 100; each finding subtracts points according to its severity
(`INFO` −2, `ATTENTION` −8, `URGENT` −25, floored at 0). **Every lost point is
traceable**: clicking the score expands the list of findings that lowered it. No
findings means exactly 100 — not 94 "to look serious". The antivirus-style score,
that magic number nobody can explain, is explicitly rejected.

### 7.3 Anti-flapping

A console that turns red because a compile loaded the CPU for three seconds will
never be trusted again. Therefore:

- a threshold must hold over a **window** before opening a finding (e.g. CPU > 90 %
  for 5 minutes);
- it must fall below a **distinct lower threshold** to close it (hysteresis);
- opening and closing are written to `event`, which yields a history of incidents
  rather than merely a history of curves.

### 7.4 Trends

With 90 days on record, the tool can say what an instantaneous dashboard cannot.
Linear regression over the available window, shown only when the correlation is
meaningful (r² > 0.7) and the horizon is under 24 months:

- "the disk grows by 1.8 GB per week, projected full in March 2027";
- extrapolated battery wear;
- rising average CPU temperature — the usual sign of a clogged fan.

This is the concrete payoff of choosing "live + history".

### 7.5 Thresholds

Every threshold lives in **`rules.py`, a single file**, so that adjusting one does
not require reading the whole project.

| Domain | Attention | Urgent | Note |
|---|---|---|---|
| Root filesystem | > 80 % | > 92 % or < 3 GB free | plus a full-by projection |
| CPU temperature | > 85 °C sustained 5 min | > 95 °C or throttling | 77 °C today is normal |
| Memory | available < 15 % and swap active | OOM killer in journal | margin is already thin |
| SMART | reallocations > 0, wear > 80 % | `FAILING_NOW` | wear is the real SSD indicator |
| Battery | wear > 30 % | wear > 50 % | `charge_full / charge_full_design`, **only if plausible** — otherwise no finding |
| Updates | any pending security fix | security pending > 14 days | weighted by `unattended-upgrades` |
| Services | 1 failed | failed and restart-looping | 0 on this machine |
| Journal | new or accelerating pattern | kernel panic, I/O errors | 59/24 h is normal noise |
| Network | DNS lost | no default route | — |

### 7.6 Sensor plausibility checking

Hardware lies. Not maliciously: sloppy drivers, careless ACPI firmware, units that
differ by vendor. This machine's battery is the proof (§3). Displaying a wrong
number with confidence is worse than admitting ignorance — it is exactly what
destroys trust in a diagnostic tool.

Every quantity therefore declares a **validity domain**, enforced inside `collect()`
before anything is stored:

| Quantity | Accepted domain | Cross-check |
|---|---|---|
| Percentages | 0 – 100 | — |
| Temperatures | −20 – 125 °C | zone ignored when out of range |
| Battery charge | > 0 | `charge_now <= charge_full × 1.05` |
| Battery capacity | > 0 | `charge_full <= charge_full_design × 1.05` |
| CPU frequency | 100 MHz – 10 GHz | — |
| Network counters | monotonic | a reset means an interface restart, not a negative rate |

An out-of-domain value is **neither displayed nor stored**. The affected card reads
"incoherent value reported by the driver", with the raw value visible in Expert mode
so the problem stays diagnosable. On this machine, battery wear will therefore be
reported as unavailable rather than falsely reassuring at 0 %.

### 7.7 The noise trap

59 journal errors in 24 hours is the normal background of a Linux desktop
(Bluetooth, ACPI, drivers). Reporting them raw as "59 errors!" would cause panic
over nothing and destroy trust in the tool — after which nobody reads the real
alerts. The `journal` probe **groups by recurring message**, applies a list of known
noise patterns, and reports only a pattern that is **new** or whose frequency is
**accelerating**.

## 8. Action catalogue

### 8.1 Principle

There is **no "run this command" route**. Every action is declared in code:

```python
Action(
    id="apt.upgrade",
    argv=["/usr/bin/apt-get", "-y", "-o", "Dpkg::Options::=--force-confold",
          "dist-upgrade"],
    root=True, risk=Risk.MEDIUM,
    params={"package_count": 17},     # interpolated into the localized confirmation
)
```

The browser sends **an identifier**, never a command fragment. `argv` is fixed,
`shell=False`, and no text from the network is interpolated into it. That is what
separates an action catalogue from a remote shell.

Labels, durations and confirmation prompts are catalogue messages (§11), keyed by
action id — the same mechanism as findings.

### 8.2 The catalogue

| id | Risk | Command |
|---|---|---|
| `apt.refresh` | safe | `apt-get update` |
| `apt.upgrade` | medium | `apt-get -y -o Dpkg::Options::=--force-confold dist-upgrade` |
| `apt.security` | medium | `unattended-upgrade` |
| `clean.autoremove` | medium | `apt-get -y autoremove --purge` |
| `clean.aptcache` | safe | `apt-get clean` |
| `clean.snaps` | medium | `snap remove --revision=<r> <name>` per disabled revision |
| `clean.journal` | medium | `journalctl --vacuum-time=15d` |
| `svc.restart` | medium | `systemctl restart <unit>` — unit taken **from the failed-services list**, never from the client |
| `sys.reboot` | sensitive | `shutdown -r +1` |
| `sys.poweroff` | sensitive | `shutdown -h +1` |
| `disk.selftest` | safe | `smartctl -t short /dev/sda` |
| `disk.trim` | safe | `fstrim -av` |
| `report.export` | safe | internal, unprivileged |

Cleanup actions **first announce how much space they will reclaim** (`apt-get -s`,
`du`, `snap list --all`) before offering the button.

`sys.reboot` and `sys.poweroff` are **shown only when a reboot is required**: a
dangerous button on permanent display eventually gets clicked by accident. One
minute delay, cancellable.

### 8.3 Parameterised actions

**Only `svc.restart` and `clean.snaps` take a parameter.** In both cases the
parameter is never taken on trust: it must belong to a list the server rebuilds
itself at execution time — currently failed units for `svc.restart`, currently
disabled snap revisions for `clean.snaps`. A value absent from that list is rejected
with `400`. The client may choose among possibilities; it may never invent one.

### 8.4 Guard rails

- **Single lock**: one action at a time, never two concurrent `apt` runs.
- **Live output**: output streams into the page over the same SSE channel
  (`apt upgrade` takes minutes; a blind spinner is unacceptable).
- **Audit**: timestamp, action, source, exit code, duration and full output written
  to the database and visible in the interface.
- **Explicit confirmation** in the browser, carrying the consequence text.
- **Refused off-loopback by default**: reading and acting do not carry the same cost
  when you get it wrong. Unlocked by `allow_remote_actions = true`.
- **Hard timeout**: 30 minutes, after which the process is terminated and the
  failure recorded.

### 8.5 Sudoers rule

`/etc/sudoers.d/health-console`, mode 0440, checked with `visudo -c` at install.
Absolute paths resolved and verified on this machine: `/usr/sbin/smartctl`,
`/usr/bin/apt-get`, `/usr/bin/systemctl`, `/usr/bin/journalctl`, `/usr/sbin/fstrim`,
`/usr/bin/snap`, `/usr/sbin/shutdown`, `/usr/bin/unattended-upgrade`.

Each entry is named with its fixed arguments. **No wildcards, never `ALL`.** The
file ships ready to install; the installer prints its content and asks for
confirmation before writing it.

## 9. HTTP API

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | the page |
| `/static/*` | GET | HTML, CSS, JS, icons, message catalogues — served from disk |
| `/api/now` | GET | full state: probes, findings, score |
| `/api/history` | GET | `?metric=<key>&range=1h\|24h\|7d\|90d` |
| `/api/stream` | GET | SSE: `state` (state), `action` (action output) |
| `/api/actions` | GET | catalogue available in the current state |
| `/api/actions/<id>` | POST | run the action, returns `run_id` |
| `/api/actions/runs` | GET | audit log |
| `/api/export` | GET | standalone HTML report, single file |

**The API is locale-neutral.** It returns finding ids, action ids, parameters and
raw values. It never returns a translated sentence, so one response serves every
language and switching locale needs no round trip.

Responses are JSON outside `/` and `/static/*`. Errors carry an accurate HTTP status
and a body of `{"error": "...", "detail": "..."}` — both machine-readable strings,
not user-facing prose.

## 10. Interface

### 10.1 Two modes

One document, two compositions. The switch remembers the choice in `localStorage`;
**Simple is the default**, since the opposite would betray the brief.

**Simple** — a single wide, airy column. Verdict at the top, then one card per
finding, each with its action button where one exists. Nothing else. No charts, no
exotic units: "441 GB left", not "9 % of 481 GB".

**Expert** — the dense grid: gauges, sparklines over the last 60 minutes, process
and partition tables, 24 h / 7 d / 90 d history charts, action audit log, and each
probe's raw JSON as a last resort.

### 10.2 Accessibility

Not a nicety when the audience is the general public.

- Colour **never** carries information alone: every state has an icon and a word
  ("Attention", not merely orange) — otherwise 8 % of men cannot read the dashboard.
- AA contrast, full keyboard navigation, touch targets ≥ 44 px.
- `prefers-reduced-motion` and `prefers-color-scheme` respected.
- ARIA live regions for changing values, without screen-reader chatter.
- Numbers and times formatted through `Intl` in the active locale.

### 10.3 Responsive

Since LAN access was chosen, reading the console from a phone is a real use case:
the Expert grid collapses to one column, and Simple mode is designed mobile-first.

### 10.4 Honest display

If the SSE stream drops, the page does not freeze stale values while pretending
otherwise: it dims the numbers and shows "data frozen since 14:32 · reconnecting…",
retrying with a growing delay. **A health console that lies about its own freshness
is a trap.** Same rule for an unavailable probe: "SMART unavailable" plus the command
to enable it, never a reassuring zero.

### 10.5 Front-end technique

No build step and **no CDN** — the console must work without Internet access,
which is the least one can ask of a diagnostic tool. Native ES modules under
`web/js/`, and two libraries vendored under `web/vendor/` and served from disk:
Bootstrap 5.3 for the layout and Chart.js 4 for the charts.

Vendoring rather than linking is not a preference: `default-src 'self'` means a
CDN stylesheet would fail **silently** in the browser.

Budget: < 60 KiB of uncompressed JS **for code we write**, enforced across
`web/js/*.js`. The vendored libraries sit outside that budget and are recorded
with their exact versions in `web/vendor/LICENSES.md`.

Superseded: this section previously called for hand-drawn SVG charts and
counted the vendored libraries against the 60 KiB budget. See
`2026-08-30-health-console-ui-redesign.md` §2.2.

## 11. Internationalisation

### 11.1 Where the wording lives

The API is locale-neutral (§9) and findings carry ids and parameters (§7.1). All
user-facing wording lives in **JSON message catalogues served as static files**:
`web/i18n/en.json` and `web/i18n/fr.json`.

```json
{
  "finding.battery.wear.title": "The battery has lost {wear_pct} % of its original capacity",
  "finding.battery.wear.why": "It therefore lasts less than when new. This is normal battery ageing, but past half its capacity, replacement becomes reasonable.",
  "severity.ATTENTION": "Attention",
  "ui.freshness.stale": "Data frozen since {time} · reconnecting…"
}
```

Catalogues are fetched from the same origin, so `Content-Security-Policy:
default-src 'self'` covers them without exception.

### 11.2 Locale selection

1. An explicit choice stored in `localStorage` wins.
2. Otherwise the first `navigator.languages` entry with a catalogue is used.
3. Otherwise **English**, which is also the fallback for any key missing from
   another catalogue.

A visible selector switches locale **without reloading**: the state is already in
memory and only the rendering pass reruns. The choice is remembered.

### 11.3 Formatting

`Intl.NumberFormat` and `Intl.DateTimeFormat` are constructed with the active
locale, so "4.7 GB" becomes "4,7 Go" in French without a second code path. Unit
symbols are catalogue entries, not string literals in the rendering code.

### 11.4 Completeness is enforced by tests

A missing translation must be a test failure, not a hole the user discovers.

- Every id in `FINDING_IDS` has `title` and `why` entries in **every** catalogue.
- Every action id has `label` and `confirm` entries in every catalogue.
- Corresponding entries across catalogues use **the same placeholders** — a French
  string referring to `{wear_pct}` when the English one says `{wear}` is a defect.
- Every catalogue has exactly the same key set; extra keys are as much of a failure
  as missing ones.
- No catalogue value is empty.

### 11.5 What is not translated

`detail` strings (§7.1), metric keys, action ids, log messages and API error codes
stay in English. They are diagnostic material, addressed to whoever reads the raw
data, and translating them would make problem reports harder to compare.

## 12. Security

- Listens on `0.0.0.0:8787`, port configurable.
- **Token** of 32 random bytes generated at install, stored in
  `~/.config/health-console/config.toml` with mode 0600, required for any non-loopback
  access, compared in **constant time** (`hmac.compare_digest`).
- The token lives in the browser's `localStorage`, **never in a URL** that could be
  pasted into a chat or land in a history file.
- A `?k=<token>` query parameter is also accepted, for the one case `localStorage`
  cannot cover: a phone opening a bookmarked or shared link, with no way to set a
  header on plain navigation. This is a deliberate residual exposure — it persists
  in browser history and in any intermediary's logs (LAN router, proxy, connection
  tracking) beyond what this process's own access-log suppression and
  `Referrer-Policy: no-referrer` can reach.
- Actions refused off-loopback unless `allow_remote_actions = true`.
- Closed catalogue, fixed `argv`, `shell=False`, no interpolation.
- `sudoers.d` limited to the named binaries with their arguments.
- Headers: `Content-Security-Policy: default-src 'self'`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
- systemd hardening: `NoNewPrivileges=no` (mandatory, `sudo` depends on it),
  `ProtectSystem=strict` with `ReadWritePaths=` limited to
  `~/.local/share/health-console` and `~/.config/health-console`, plus `PrivateTmp`
  and `RestrictNamespaces`. **`ProtectHome` is not used**: it would stop the service
  writing its own database.
- The exported report states up front that it contains system information (hostname,
  interfaces, local IPs) and **never contains the token**.

## 13. Robustness and degraded modes

| Failure | Behaviour |
|---|---|
| Probe fails | `unavailable` + displayed reason + the command to unlock it |
| `apt` lock busy | finding "update running elsewhere", retried next cycle |
| Collector killed | `Restart=on-failure`, `RestartSec=5` |
| Database corrupt | recreated, incident logged — history is valuable, not vital |
| SSE dropped | reconnect with growing delay, data visibly marked stale |
| Action fails | exit code and full output displayed — never a silent failure |
| Disk full | database writes stop cleanly, the console keeps displaying |
| Catalogue missing a key | English fallback, and the missing key logged to the console |

## 14. Testing

The measurement/judgement split pays off here: `evaluate()` being pure, all the
logic is testable without hardware, in milliseconds.

- **Fixtures** — real samples captured on this machine, plus synthetic cases that
  cannot be provoked: disk at 99 %, SMART `FAILING_NOW`, battery at 60 % wear,
  kernel panic, restart-looping service, absent battery (desktop), `charge_*` **and**
  `energy_*`, and **this machine's real out-of-range values**
  (`charge_now` = 467 × `charge_full`), which must yield "incoherent" and not a
  number.
- **Verdicts** — one test per rule in `rules.py`, at both sides of the threshold,
  plus hysteresis (a brief spike opens nothing; a sustained breach opens; returning
  under the low threshold closes).
- **Score** — the sum is exact and every lost point maps back to a finding.
- **Storage** — retention, 5-minute aggregation and pruning, with an **injected
  clock** (no test waits 48 hours). Each configurable period is tested: lowering
  purges, raising resurrects nothing, `raw_days > aggregate_days` is refused at
  startup, and the size estimate is checked against the volume actually written
  after simulating a month of samples.
- **Server** — routing, token absent / wrong / valid, action refused from a
  non-loopback address, behaviour past 8 SSE streams.
- **Actions** — `argv` construction and parameter validation for `svc.restart` and
  `clean.snaps`, **verified without ever executing** the commands.
- **Internationalisation** — the completeness rules of §11.4, plus a check that no
  user-facing string is hard-coded in the rendering code.
- **Smoke** — a separate test runs the real probes on this machine, marked as
  hardware-dependent.

## 15. Installation and operation

`health-console` CLI:

- `run` — run in the foreground (development)
- `install` — write the systemd user unit, generate the token, print the sudoers rule
  **and ask for confirmation** before installing it, offer `loginctl enable-linger`
- `status` — service, token, privileges and database state
- `config` — effective configuration and projected database size
- `prune` — apply the configured retention immediately
- `export [file]` — standalone HTML report
- `token [--rotate]` — show or regenerate the token

systemd user unit: `WantedBy=default.target`, `Restart=on-failure`,
`MemoryMax=128M` (a hard guard against a leak on a 5 GB machine).

## 16. Layout

```
health-console/
├── bin/health-console
├── healthconsole/
│   ├── config.py       server.py     scheduler.py   store.py
│   ├── rules.py        verdict.py    actions.py     findings.py
│   ├── plausibility.py ring.py       cli.py
│   └── probes/  cpu memory thermal network battery storage smart
│                updates services journal processes osinfo
├── web/  index.html  style.css  i18n/en.json  i18n/fr.json
│        js/  app.js  charts.js  dom.js  expert.js  history.js
│             i18n.js  simple.js  stream.js  theme-boot.js
│        vendor/  bootstrap.min.css  bootstrap.bundle.min.js  chart.umd.min.js
├── systemd/health-console.service
├── packaging/sudoers.d/health-console
├── tests/  fixtures/  test_verdict.py  test_store.py  test_server.py
│           test_actions.py  test_i18n.py  test_smoke.py
└── docs/superpowers/
```

## 17. Out of scope (deliberate YAGNI)

Excluded on purpose, to be reopened only on real need:

- multi-machine fleets and aggregation;
- alerts by email, Telegram or desktop notification;
- user accounts, roles, multi-user authentication;
- containers and virtual machines (`docker` absent, `virbr0` down);
- NVIDIA GPUs (`nvidia-smi` absent, AMD GPU here);
- running arbitrary commands — excluded by design, not for lack of time;
- right-to-left locales and translator tooling: the catalogue mechanism supports more
  languages, but only `en` and `fr` ship.

## 18. Known risks

1. **An `apt` action fails midway** and leaves dpkg inconsistent. The risk already
   exists on the command line; the mitigation is the full output displayed and
   logged, plus a finding "dpkg interrupted, run `sudo dpkg --configure -a`"
   detected on the next cycle.
2. **LAN exposure widens the attack surface.** Mitigated by the token, actions
   refused remotely by default, and the closed catalogue — but the choice remains
   deliberate and is documented here.
3. **Other sensors may lie like the battery.** The plausibility checks (§7.6) cover
   known quantities, but a driver can invent a value *inside* the accepted domain.
   There is no general defence; comparison against history at least flags impossible
   jumps.
4. **`smartctl` output varies** by model. The probe uses `--json` and degrades
   cleanly when a field is missing.
5. **`unattended-upgrades` may act while a manual `apt.upgrade` runs.** The apt lock
   protects the system; the action then reports "lock busy" rather than failing
   without explanation.
6. **Translation drift.** Catalogues can diverge as findings are added. The
   completeness tests (§11.4) turn that into a build failure rather than a silent
   English string in a French interface.
