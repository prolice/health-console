# Health Console

A local web console that reports the health of an Ubuntu machine through two
deliberate readings: a **Simple mode** in plain language, understandable without a
technical background, and an **Expert mode** that exposes everything.

> **Project status: design.** The design document is complete and approved;
> **there is no code yet**. This repository is not installable as it stands.

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
resident memory, under 2 % CPU on average, and a database of about 32 MB with default
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

The collector runs at two cadences: 2 seconds for what is cheap to read (`/proc`,
`/sys`), 5 minutes for what is expensive (SMART, APT, systemd). The HTTP server
streams state over SSE. The front end is HTML, CSS and native ES modules, with
hand-drawn SVG charts.

Full design:
[`docs/superpowers/specs/2026-08-29-health-console-design.md`](docs/superpowers/specs/2026-08-29-health-console-design.md)

## Security notes

This project targets **a personal machine on a trusted network**. Read this before
deploying it anywhere else.

- **It runs privileged commands.** APT upgrades, service restarts, machine shutdown.
  Installation writes a `sudoers.d` rule restricted to a named list of binaries with
  fixed arguments — never `ALL`, never a wildcard.
- **There is no "run this command" route.** The action catalogue is declared in code;
  the browser sends an identifier, never a command fragment. `shell=False`, and no
  text from the network is interpolated into an argument list.
- **It listens on the local network**, protected by a token. Actions are **refused
  outside `127.0.0.1`** unless explicitly enabled in configuration: reading and acting
  do not carry the same cost when you get it wrong.
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
