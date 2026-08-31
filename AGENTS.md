# Repository Guidelines

## Project Structure & Module Organization

`healthconsole/` contains the Python application. Core orchestration lives in
`scheduler.py`, persistence in `store.py`, HTTP routing in `server.py`, CLI
commands in `cli.py`, action execution in `actions.py` and `runner.py`, and
hardware probes in `healthconsole/probes/`.

`web/` contains the static browser UI: `index.html`, `style.css`, ES modules in
`web/js/`, translation catalogues in `web/i18n/`, and vendored Bootstrap/Chart.js
assets in `web/vendor/`. `tests/` contains Python unit tests plus the harness
for JavaScript tests under `web/js/tests/`. Design notes and implementation
plans live under `docs/superpowers/`.

## Build, Test, and Development Commands

- `./bin/health-console run`: start the local console on the configured host and
  port, defaulting to `0.0.0.0:8787`.
- `./bin/health-console config`: print effective configuration and projected
  database size.
- `./bin/health-console status`: show database size, metric count, and retained
  history depth.
- `./bin/health-console prune`: apply retention immediately and vacuum the DB.
- `./run-tests`: run the Python `unittest` suite; JavaScript unit tests run via
  `node --test` when Node is installed.

There is no build step, `package.json`, CDN dependency, or virtualenv expected.

## Coding Style & Naming Conventions

Use Python 3 standard-library style with 4-space indentation, type hints where
they clarify contracts, and small modules with explicit responsibilities. Probe
modules expose `NAME`, `CADENCE`, `collect()`, `metrics()`, and `evaluate()`.
Metric keys use dotted lowercase names such as `cpu.usage` or
`battery.wear_pct`.

Frontend code uses native ES modules, no bundler, and catalogue-driven UI text.
Do not hard-code user-facing strings when they belong in `web/i18n/*.json`.

## Testing Guidelines

Tests use `unittest`; files are named `tests/test_*.py`. Keep tests focused on
observable behavior and the repository’s safety invariants: no external web
assets, complete translations, bounded storage, robust HTTP errors, and audited
actions. When adding findings or actions, update `healthconsole/findings.py` or
`healthconsole/actions.py` plus both locale catalogues and related tests.

## Commit & Pull Request Guidelines

Recent commits use concise imperative subjects, for example
`Refuse a POST body, bound the relay, and guard the shutdown window`. Prefer a
specific subject that names the behavioral change. Pull requests should describe
the user-visible effect, security or retention implications, and tests run.
Include screenshots for UI changes.

## Security & Configuration Tips

The console may listen on the LAN, but non-loopback access requires a token.
Actions are more sensitive than reads: remote action execution is disabled
unless configured. Never add a free-form command route; actions must stay in the
closed catalogue and execute with `shell=False`.
