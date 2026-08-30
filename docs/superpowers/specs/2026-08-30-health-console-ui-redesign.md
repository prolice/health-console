# Health Console — UI Redesign Design Document

- **Date:** 2026-08-30
- **Status:** design approved, ready for implementation planning
- **Scope:** the web front end only. `healthconsole/server.py` and every other
  Python module are untouched by this work.
- **Supersedes:** §10.5 of
  [`2026-08-29-health-console-design.md`](2026-08-29-health-console-design.md)
  (front-end technique). Everything else in that document still holds.
- **Project language:** English — code, comments, documentation and commit
  messages. The interface itself ships in English and French.

## 1. Goal

The console works but shows almost nothing. Simple mode renders a verdict and a
list of findings; Expert mode is a `<pre>` holding raw JSON behind the sentence
"Expert mode arrives in plan 2". Meanwhile `/api/history` — which serves
`1h / 24h / 7d / 90d` windows over every recorded metric, complete with the
depth actually available — has been finished, tested, and **called from
nowhere**.

This redesign spends that already-paid-for capability. It adds charts, a real
Expert mode, and the controls needed to navigate both, on a CSS framework
rather than 94 lines of hand-written CSS.

**What this is not.** It adds no corrective action, no `POST` route, no
privileged command. Those are the action catalogue (§8 of the design document)
and are deliberately deferred to their own spec, so that a `sudoers.d` rule
granting a daemon the right to reboot the machine is never reviewed in the same
pass as a colour palette.

## 2. Approved decisions

| Topic | Decision |
|---|---|
| CSS framework | Bootstrap 5.3, vendored under `web/vendor/` |
| Charts | Chart.js 4, vendored under `web/vendor/` |
| Sourcing | Downloaded once from cdnjs, committed to the repository |
| Composition | Two modes preserved, each enriched (design document §10.1) |
| Range selector | Global to the Expert grid, not per chart |
| Theme | Explicit auto / light / dark control, `auto` by default |
| Server | Unchanged. No new route, no CSP relaxation |
| Actions | Out of scope — separate spec |

### 2.1 Why vendored rather than a CDN or a Debian package

A CDN is impossible twice over: the console must work without Internet access,
and `server.py` sends `Content-Security-Policy: default-src 'self'`, which
would make a remote stylesheet fail **silently** in the browser.

Ubuntu ships `libjs-bootstrap5` (5.3.8) and `libjs-chart.js`, but the latter is
Chart.js 3.9, and both would make a fresh `git clone` depend on two apt
packages being installed before the page renders. Vendoring keeps the checkout
self-contained: the download happens once, at authoring time; at runtime
nothing leaves the machine, which is the principle that actually matters.

`web/vendor/LICENSES.md` records both licences (MIT) and the exact versions.

### 2.2 The budget, revised honestly

§10.5 of the design document set a budget of "< 60 KiB of uncompressed JS" and
called for hand-drawn SVG charts. Bootstrap and Chart.js together are about
510 KiB. **That budget is superseded, not quietly exceeded.**

| File | Size |
|---|---|
| `vendor/bootstrap.min.css` | ~230 KiB |
| `vendor/bootstrap.bundle.min.js` | ~80 KiB |
| `vendor/chart.umd.min.js` | ~200 KiB |
| our own `web/js/*.js` | < 80 KiB (raised to 80 KiB: §2.2 supplement below) |

The constraint that survives is the one with teeth: **no network access at
runtime, ever.** These files are read from disk, on a machine that already
holds a SQLite database of tens of megabytes. The budget for code we write now
applies at 80 KiB.

Its test does not survive as written: `test_stays_within_budget` measures
`web/app.js` alone, and `app.js` is about to shrink to 80 lines of wiring —
the assertion would pass while guarding nothing. It must measure the **total**
of `web/js/*.js`, `vendor/` excluded.

Both §10.5 of the design document and the Architecture section of `README.md`
state the old position and must be rewritten in the same change. A document
that contradicts the code it describes is worse than no document.

#### 2.2 supplement: second budget revision (80 KiB)

The original 60 KiB budget accommodated three HTML pages with hand-drawn SVG
charts and minimal interactivity. The current implementation replaces that with
eight ES modules (`app.js`, `dom.js`, `i18n.js`, `stream.js`, `history.js`,
`charts.js`, `simple.js`, `expert.js`) plus a synchronous theme-boot script,
implementing a full-fledged dashboard: a doughnut gauge showing system health,
sparklines for trend visualization, a four-card chart grid with a unified range
selector, a probe table with live updates, and an accessible text description
for every visualization. This scope expansion — from three static pages to an
interactive, multi-modal dashboard — justifies raising the ceiling to 80 KiB.

The constraint's purpose remains unchanged: keep the code we write small next to
a tool that ships a multi-megabyte SQLite database, and keep vendored libraries
outside the budget so they cannot hide behind our own footprint.

## 3. Layout

```
web/
├── index.html
├── style.css                    thin layer over Bootstrap: theme tokens,
│                                severity colours, what the framework lacks
├── vendor/                      downloaded once, committed, never edited
│   ├── bootstrap.min.css
│   ├── bootstrap.bundle.min.js
│   ├── chart.umd.min.js
│   └── LICENSES.md
├── js/
│   ├── app.js        start-up, control wiring, orchestration
│   ├── dom.js        el(), setText() — the existing aria-live discipline
│   ├── i18n.js       translate(), setLocale(), formatBytes, the Intl objects
│   ├── stream.js     EventSource, freshness, hasMeasurement/isMeasurementStale
│   ├── history.js    fetch /api/history, cache keyed by (metric, range)
│   ├── charts.js     Chart.js creation and update, colours read from the theme
│   ├── simple.js     Simple mode rendering
│   └── expert.js     Expert mode rendering
└── i18n/  en.json  fr.json
```

Today `web/app.js` is 343 lines doing everything; the redesign would push a
single file past 900. Native ES modules split it along the seams that already
exist in the code — **still no build step**, which
`test_no_build_step_artefacts` keeps enforcing. `app.js` shrinks to roughly 80
lines of wiring.

## 4. Three technical decisions

### 4.1 No date adapter for Chart.js

Chart.js's native time scale (`scale: {type: 'time'}`) requires
`chartjs-adapter-date-fns` **and** `date-fns` — a third and fourth dependency,
some 50 KiB more.

Instead: a `linear` scale over Unix timestamps with a `ticks.callback` that
formats through the `Intl.DateTimeFormat` instance **already held in
`i18n.js`**. One fewer dependency, and chart dates follow the active locale
through the same path as every other date on the page, rather than through a
second formatting system to keep in sync with the first.

### 4.2 No CSP relaxation

`default-src 'self'` blocks `style="…"` attributes in markup — so Bootstrap's
documented `<div class="progress-bar" style="width: 62%">` would not render.
It does **not** block CSSOM writes (`el.style.width = "62%"`), which is what
Bootstrap's collapse animations and Chart.js's canvas sizing use internally.

The rule for our own code, therefore: never a literal `style="…"` and never
`setAttribute("style", …)`. A test enforces it, because the failure mode is
silent — the browser drops the declaration and reports nothing.

`server.py` is not modified. `CONTENT_TYPES` already covers `.css` and `.js`.

### 4.3 No Bootstrap Icons

That package ships font files, which would mean a new MIME type in the server
and about 120 KiB more. The existing icons (`●` `ℹ` `⚠` `✖`) already live in
the message catalogues and already satisfy "colour never carries information
alone". They stay.

## 5. Simple mode

One wide, airy column — the composition is unchanged in spirit. Bootstrap
supplies the card, badge and alert; the content and its restraint do not move.

| Element | Change |
|---|---|
| Verdict | A Chart.js doughnut gauge for the score, **plus** the icon, word and sentence as text. The gauge is `aria-hidden`: it is decoration over a number that is already readable. |
| Finding cards | `.card` with `border-start border-4` tinted by severity; badge carrying icon and word; title; explanation. |
| Sparkline | Added **only** where the finding concerns a recorded metric. No axes, no legend, no tooltip — a shape, not a chart. |
| Unavailable probe | INFO card with the raw technical detail as a secondary line. Current behaviour preserved exactly. |
| Freshness | A Bootstrap alert; the page still dims when data freezes. Preserved. |

Finding-to-metric mapping:

| Finding | Metric |
|---|---|
| `cpu.usage_high` | `cpu.usage` |
| `thermal.high`, `thermal.critical` | `cpu.temp.pkg` |
| `memory.pressure` | `mem.available_pct` |
| `battery.wear` | `battery.wear_pct` |
| `battery.incoherent` | **none** |

`battery.incoherent` deliberately gets no sparkline. Drawing a curve of values
the console has just declared untrustworthy would be precisely the lie the
project refuses to tell.

## 6. Expert mode

The dense grid the design document promised, in five parts.

1. **Tile row** — CPU %, package temperature, available memory, swap, battery,
   1-minute load. Fed by the SSE stream at its 2-second cadence.
2. **Control bar** — a `btn-group` of `1h / 24h / 7d / 90d`, **global to the
   whole grid**, plus a refresh button. Global rather than per-chart is what
   makes several metrics comparable over one window.
3. **Chart grid** — one card per family, one column on a phone:

   | Card | Metrics |
   |---|---|
   | Processor | `cpu.usage`, `load.1` |
   | Thermal | `cpu.temp.pkg`, `thermal.*` zones |
   | Memory | `mem.available_pct`, `mem.swap.used` |
   | Battery | `battery.charge_pct`, `battery.wear_pct` |

4. **Actual depth** — `/api/history` already returns `depth_days` and the front
   end ignores it. When 90 days are requested and the database holds 6, the
   card says so. Without that line, a three-quarters-empty chart reads as a
   collection failure when history has simply just begun.
5. **Accordion** — probe table, then raw JSON as a last resort.

## 7. Theme

Bootstrap 5.3 switches theme through `data-bs-theme` on the root element. The
page gains an **auto / light / dark** control, defaulting to `auto` and
remembered in `localStorage`; `auto` follows `prefers-color-scheme`, which is
the only behaviour available today and leaves the user no say.

Chart colours are read from CSS custom properties at draw time and re-read when
the theme changes, so a chart never keeps light-theme colours on a dark page.

## 8. Controls delivered

Simple/Expert · language · theme · range `1h/24h/7d/90d` · refresh · probe and
JSON accordions.

No `POST`, no privileged command, no server change. Corrective action buttons
belong to the action catalogue spec.

## 9. Internationalisation

Wording is data, not code. Roughly 30 new keys, in `en.json` **and** `fr.json`:

| Family | Keys |
|---|---|
| Theme | `ui.theme.label`, `.auto`, `.light`, `.dark` |
| Range | `ui.range.group`, `.1h`, `.24h`, `.7d`, `.90d`, `ui.refresh` |
| Metrics | `ui.metric.cpu.usage`, `.load.1`, `.cpu.temp.pkg`, `.mem.available`, `.mem.available_pct`, `.mem.swap.used`, `.battery.charge_pct`, `.battery.wear_pct` |
| Families | `ui.chart.group.cpu`, `.thermal`, `.memory`, `.battery` |
| Charts | `ui.chart.empty`, `.depth`, `.depth_short`, `.summary`, `.unavailable` |
| Expert | `ui.expert.overview`, `.probes`, `.raw` |
| Errors | `ui.error.history` |

**Thermal zone series are not translated.** `thermal.<zone>` keys are produced
from whatever the kernel names its zones (`acpitz`, `x86_pkg_temp`, …): the set
is discovered at runtime and differs per machine, so no catalogue can cover it.
Those series are labelled with the raw zone name, which §11.5 of the design
document already classes as untranslated diagnostic material. `cpu.temp.pkg` is
a fixed key and *is* translated.

Two test consequences, easy to forget and load-bearing:

- `REQUIRED_UI_KEYS` in `tests/test_i18n.py` is a **hard-coded** frozenset. New
  keys must be added to it, or nothing guarantees they exist in either
  catalogue.
- `ui.expert.placeholder` ("Expert mode arrives in plan 2") is removed from
  both catalogues and from that set.

## 10. Accessibility

A `<canvas>` is invisible to a screen reader. Adding charts without provision
would *degrade* the product, which §10.2 of the design document treats as an
imperative rather than a decoration.

- Every chart carries `role="img"` and an `aria-label` built from
  `ui.chart.summary`: "Processor over 24 h: minimum 4 %, maximum 87 %,
  currently 34 %". A sentence that says what the curve shows.
- The verdict gauge is `aria-hidden`; the score sits beside it as text already,
  and announcing it twice is noise.
- **The Expert tile row is not a live region.** It changes every 2 seconds;
  `aria-live` there would turn the page into continuous chatter. Only the
  verdict is announced, through the existing `setText()` discipline that writes
  only on real change.
- `prefers-reduced-motion` disables Chart.js animations
  (`options.animation = false`), not merely CSS transitions.
- Real `<button>` elements, keyboard reachable, targets ≥ 44 px — the current
  rule, preserved.

## 11. Degraded modes

"The tool is not allowed to lie" is the project's rule, and a chart is an easy
place to break it.

| Situation | Behaviour |
|---|---|
| `/api/history` fails (401, 503, network) | `ui.error.history` in the card. Never an empty chart, which reads as "everything is zero". |
| Window contains no points | `ui.chart.empty`. Not a flat line at zero — a flat line is a measurement; the absence of one is not. |
| `depth_days` < requested range | `ui.chart.depth_short` states the real depth. |
| Stream down or measurement frozen | Existing behaviour preserved unchanged. |
| **`chart.umd.min.js` missing or failing to load** | `charts.js` checks for `window.Chart` and disables itself. Verdict, findings, tiles and freshness keep rendering. |

The last row is structural. A diagnostic tool that goes blind because a
decorative library is absent would be a serious regression against what exists
today.

## 12. Performance: the 90-day window

Ninety days of 5-minute aggregates is roughly **26,000 points per metric**.
Drawn raw they saturate the canvas and make scrolling stutter on a phone. The
24-hour window is no free ride either: served from the raw table at a 30-second
step, it is about 2,900 points.

Chart.js's built-in `decimation` plugin (LTTB algorithm) is therefore enabled
on **any series above 1,000 points**, rather than being tied to which table the
server chose. It ships inside the bundle — no additional dependency — and
preserves the visible shape of the curve.

## 13. Testing

`tests/test_web_assets.py` is rewritten for the module layout: its assertions
currently pin exact strings inside `web/app.js` (`FALLBACK_LOCALE = "en"`,
`BYTE_FINDING_PARAMS`, `function setText`, …), and moving that code into
modules breaks about ten of them. They are re-aimed at the module that now
owns each behaviour, not deleted.

Four checks that do not exist today:

- **No external URL** anywhere under `web/`, walked recursively with `vendor/`
  excluded — replacing the current hard-coded list of three files, which would
  not notice a URL added in a new module.
- **Vendor files present and non-empty**, and referenced by `index.html`
  through local paths.
- **No `style="…"` and no `setAttribute("style"`** in our own code — the thing
  the CSP drops silently, so a test is worth more than a comment.
- **Front-end range identifiers match `RANGES` in `server.py` exactly**, and
  every metric key the front end references exists among those the probes
  produce. These catch silent drift: a typo in a metric key raises no error, it
  just yields an empty chart nobody notices.

`test_i18n.py` keeps enforcing catalogue completeness, once `REQUIRED_UI_KEYS`
is extended (§9).

Manual verification: both modes, the three theme settings, a phone-width
viewport, and the stale-data path with the collector stopped.

## 14. Documentation to correct in the same change

| Document | What is now false |
|---|---|
| Design document §10.5 | "hand-drawn SVG charts", "< 60 KiB of uncompressed JS" (now 80 KiB; see §2.2 supplement) |
| `README.md`, Architecture | "no CDN […] hand-drawn SVG charts" — "no CDN" stays true, the rest does not |
| Test assertion `test_stays_within_budget` | 60 KiB cap (now 80 KiB; see §2.2 supplement) |

## 15. Accepted risks

| Risk | Position |
|---|---|
| ~510 KiB vendored against an 80 KiB target for our code | Accepted and documented (§2.2, §2.2 supplement). Read from local disk, never from the network. Vendored and own budgets remain separate. |
| The Bootstrap bundle (80 KiB) for mostly an accordion | An explicit choice. Reversible later without touching the markup. |
| Two pinned libraries to maintain | `LICENSES.md` records exact versions; updating is a file replacement. |
| Charts invite reading noise as signal | Mitigated by §11: absent data is shown as absent, never as zero. |

## 16. Out of scope

- Every corrective action, `POST` route, `sudoers.d` rule and audit view — the
  action catalogue spec.
- New probes and new metrics. This work draws only what is already collected.
- `/api/export` (standalone HTML report).
- Any change to `healthconsole/`.
