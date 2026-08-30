# Health Console

A local web console that reports the health of an Ubuntu machine through two
deliberate readings: a **Simple mode** in plain language, understandable without a
technical background, and an **Expert mode** that exposes everything.

> **Project status: working console.** All 15 tasks of the implementation plan
> are complete and reviewed. It runs today from a checkout — see
> [Running it](#running-it) below.

## The idea

Displaying `77 °C` is trivial. Saying **whether that is bad** is the product.

Most monitoring tools address system administrators and leave the user alone with
numbers. Health Console does the opposite: it measures like a serious tool, then it
**interprets**.

- "The disk grows by 1.8 GB per week, projected full in March 2027" rather than
  `9% /dev/sda2`
- A health score where **every lost point is traceable** to a named finding — not the
  unexplainable magic number of antivirus software
- Corrective actions one click away, drawn from a **closed catalogue**

## Design principles

**Measurement and judgement are separate.** Probes produce only numbers; rules reason
only about numbers. All interpretation logic is a pure function, testable without
hardware.

**The tool is not allowed to lie.** If the data stream drops, the page dims the
values and says since when they have been frozen. If a sensor reports an implausible
value — a real case here: a battery driver announcing a 46,700 % charge — the console
shows "incoherent" rather than a wrong number. A health console that lies about its
own freshness is a trap.

**Noise destroys trust.** 59 journal errors a day is the normal background of a Linux
desktop. Reporting them raw would cause panic over nothing, after which nobody reads
the real alerts. Errors are grouped by pattern; only what is new or accelerating is
reported.

**A health tool must not harm the machine's health.** Target budget: under 60 MB
resident memory, under 2 % CPU on average, and a database of about 54 MB with default
settings. The 2-second live view lives in memory; only one sample every 30 seconds is
written to disk. Retention periods are set in days, and the service announces the
database size they imply **before** it is reached.

**Wording is data, not code.** Findings carry an identifier and parameters, never a
sentence. All user-facing text lives in message catalogues, so the interface ships in
English and French — and a missing translation is a failing test, not a hole the user
discovers.

## Architecture

Python standard library, `psutil` and SQLite. **No `pip` dependency, no virtualenv, no
build step, no CDN** — a diagnostic tool must work without Internet access, and
survive distribution upgrades.

The collector runs at two cadences: 2 seconds for what is cheap to read
(`/proc`, `/sys`), 5 minutes for what is expensive (SMART, APT, systemd). The
HTTP server streams state over SSE. The front end is HTML, CSS and native ES
modules, built on Bootstrap and Chart.js — both **vendored and served from
disk**, never fetched from a CDN, so the console works with no network at all.

Full design:
[`docs/superpowers/specs/2026-08-29-health-console-design.md`](docs/superpowers/specs/2026-08-29-health-console-design.md)

## Running it

No install step: run it straight from a checkout with `psutil` available
(`apt install python3-psutil`, or it is already present on the target Ubuntu
release).

```sh
./bin/health-console run
```

Then open `http://127.0.0.1:8787/`. `Ctrl+C` stops it.

**By default it listens on every interface**, not only on loopback, and prints
the LAN address alongside the local one when it can determine one. Nothing is
exposed by that on its own:
without a token in the configuration, every non-loopback request is refused with
a `401`. Set `bind = "127.0.0.1"` if you want it not to listen beyond this
machine at all.

Other commands:

```sh
./bin/health-console config          # effective configuration and projected database size
./bin/health-console status          # database location, size, and how much history it holds
./bin/health-console prune           # apply the configured retention immediately
./bin/health-console token           # show the configured token, if any
./bin/health-console token --rotate  # generate one and store it in config.toml
```

Configuration lives at `~/.config/health-console/config.toml` (created on
first use of a command that needs one; absent otherwise, in which case every
setting below takes its default). Example:

```toml
[server]
bind  = "0.0.0.0"     # the default — every interface. "127.0.0.1" for loopback only
port  = 8787
token = ""            # required for any non-loopback request; see Security notes

[retention]
raw_days       = 2    # fine-grained samples (30 s step)
aggregate_days = 90   # 5-minute averages — this is what carries the trends
snapshot_days  = 7
event_days     = 365
audit_days     = 365
```

The database lives at `~/.local/share/health-console/db.sqlite3` (WAL mode;
`-wal`/`-shm` files sit alongside it while the service runs).

## Security notes

This project targets **a personal machine on a trusted network**. Read this before
deploying it anywhere else.

- **It runs privileged commands.** APT upgrades, service restarts, machine shutdown.
  Installation writes a `sudoers.d` rule restricted to a named list of binaries with
  fixed arguments — never `ALL`, never a wildcard.
- **There is no "run this command" route.** The action catalogue is declared in code;
  the browser sends an identifier, never a command fragment. `shell=False`, and no
  text from the network is interpolated into an argument list.
- **It listens on the local network by default** (`bind = "0.0.0.0"`), protected by
  a token. With no token configured the socket is open but every non-loopback
  request is refused, so an unconfigured console is not readable from the LAN —
  it is listening, not answering. Actions will be **refused outside `127.0.0.1`**
  unless explicitly enabled in configuration: reading and acting do not carry the
  same cost when you get it wrong.
- **The token crosses the network in clear text.** This is plain HTTP with no TLS,
  so anyone able to observe traffic on the same network can read it, and it grants
  access to detailed telemetry about the machine. `token --rotate` invalidates a
  token you believe was seen.
- **The token can also be passed as `?k=` in the URL**, so a phone can open a
  bookmarked or shared link. That is a deliberate trade-off: unlike the header path,
  it persists in browser history and in any intermediary's logs (LAN router, proxy,
  connection tracking).
- **It is not designed to face the Internet.** Do not put it there.
- Every action is logged with its timestamp, source, exit code and full output.

## Target

Ubuntu 26.04 LTS. The principles hold for any recent distribution, but the APT, snap
and systemd probes are Debian/Ubuntu-specific.

## Languages

The interface ships in **English (default) and French**, selectable in the page and
remembered between visits. Adding a language means adding one catalogue file under
`web/i18n/`; the test suite then requires it to be complete.

Code, comments, documentation and commit messages are in English.

## License

MIT — see [LICENSE](LICENSE).
