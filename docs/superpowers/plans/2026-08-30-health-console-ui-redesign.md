# Health Console UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the front end with a Bootstrap-based interface that draws the
history `/api/history` already serves, and turn the Expert mode placeholder into
a real dense view.

**Architecture:** `web/app.js` (343 lines, one file, everything) splits into
eight native ES modules under `web/js/`. Bootstrap 5.3.8 and Chart.js 4.5.1 are
vendored under `web/vendor/` and served from disk. Simple mode keeps its single
airy column and gains sparklines; Expert mode becomes a tile row, a global range
selector, a chart grid and an accordion. No Python module is modified.

**Tech Stack:** Native ES modules (no build step), Bootstrap 5.3.8, Chart.js
4.5.1, Python `unittest` for the test suite, optional `node --test` for pure
front-end logic.

**Spec:** [`docs/superpowers/specs/2026-08-30-health-console-ui-redesign.md`](../specs/2026-08-30-health-console-ui-redesign.md)

## Global Constraints

Every task's requirements implicitly include this section.

- **No network access at runtime.** No CDN, no external URL in any file under
  `web/` outside `vendor/`. Enforced by `tests/test_web_assets.py`.
- **No build step.** No `package.json` in the repository root, no bundler, no
  transpiler. Enforced by `test_no_build_step_artefacts`.
- **No Python module is modified.** `healthconsole/server.py` in particular
  keeps its current routes, its `RANGES`, and its
  `Content-Security-Policy: default-src 'self'`.
- **CSP: no `style="…"` in markup, no `setAttribute("style", …)` in our code.**
  CSSOM writes (`el.style.width = …`) are permitted and are how widths are set.
- **Wording is data.** Every user-facing string comes from `web/i18n/*.json`.
  Both `en.json` and `fr.json` must carry the identical key set —
  `test_all_catalogues_share_the_same_key_set` fails on any divergence.
- **Colour never carries information alone.** Every state shows an icon and a
  word alongside its colour.
- **Touch targets ≥ 44 px**, real `<button>` elements, keyboard reachable.
- **Our own JS budget: < 60 KiB total** across `web/js/*.js` (`vendor/`
  excluded).
- **Pinned versions:** Bootstrap `5.3.8`, Chart.js `4.5.1`. Recorded in
  `web/vendor/LICENSES.md`.
- **Run the suite with `./run-tests`** (`python3 -m unittest discover -s tests -t .`).
- Code, comments, documentation and commit messages in English.

---

## A note on how the front end is tested

**Read this before Task 3.** The repository has no JavaScript test runner. Every
existing front-end "test" in `tests/test_web_assets.py` is a Python assertion
over the *source text* of `web/app.js` — it checks that a string appears in a
file, not that a function behaves. That catches regressions in structure and
nothing else.

This plan keeps that pattern (it is the established one, and it costs nothing)
**and** adds a second, genuine layer in Task 4: Node 24 is present on this
machine, and `node --test` needs no `package.json` and no dependency, so the
pure logic — series summarisation, the decimation threshold, cache keys, byte
formatting — can be unit-tested for real. `tests/test_js_units.py` shells out to
it and **skips cleanly when `node` is absent**, so the console's own "no
dependency" promise is untouched: node is needed to *run the tests*, never to
run the console.

**Task 4 is the one addition this plan makes beyond the spec.** If it is
unwanted, drop Task 4 entirely; Tasks 5–10 do not depend on it, and the
source-grep tests still run.

---

## Task 1: Vendor Bootstrap and Chart.js

**Files:**
- Create: `web/vendor/bootstrap.min.css`
- Create: `web/vendor/bootstrap.bundle.min.js`
- Create: `web/vendor/chart.umd.min.js`
- Create: `web/vendor/LICENSES.md`
- Modify: `tests/test_web_assets.py:16-27` (`TestNoExternalResources`)

**Interfaces:**
- Consumes: nothing.
- Produces: three vendored asset files at fixed paths, referenced by
  `index.html` in Task 5 as `/static/vendor/<name>`.

- [ ] **Step 1: Write the failing test**

Replace the whole `TestNoExternalResources` class in
`tests/test_web_assets.py` with:

```python
VENDOR = WEB / "vendor"

# Files we author. vendor/ is third-party and excluded: it is read, never
# edited, and its contents are pinned by version in vendor/LICENSES.md.
def authored_files():
    for path in sorted(WEB.rglob("*")):
        if not path.is_file() or VENDOR in path.parents:
            continue
        if path.suffix in (".html", ".css", ".js", ".json"):
            yield path


class TestNoExternalResources(unittest.TestCase):
    """A diagnostic tool must work without Internet access."""

    def test_no_external_urls(self):
        # Walked recursively rather than over a hard-coded list of three
        # files: the old form would not have noticed a URL introduced in a
        # module that did not exist when it was written.
        for path in authored_files():
            text = path.read_text(encoding="utf-8")
            for match in re.findall(r"https?://[^\s\"')]+", text):
                self.fail(f"{path.relative_to(WEB)} references {match}")

    def test_no_build_step_artefacts(self):
        self.assertFalse((WEB.parent / "package.json").exists())


class TestVendoredLibraries(unittest.TestCase):
    """Vendored, not fetched: default-src 'self' would drop a CDN silently."""

    EXPECTED = {
        "bootstrap.min.css": 100 * 1024,
        "bootstrap.bundle.min.js": 40 * 1024,
        "chart.umd.min.js": 100 * 1024,
    }

    def test_every_vendored_file_is_present_and_plausible(self):
        for name, floor in self.EXPECTED.items():
            path = VENDOR / name
            self.assertTrue(path.is_file(), f"vendor/{name} is missing")
            self.assertGreater(
                path.stat().st_size, floor,
                f"vendor/{name} is too small to be the real library")

    def test_licences_are_recorded_with_pinned_versions(self):
        text = (VENDOR / "LICENSES.md").read_text(encoding="utf-8")
        self.assertIn("5.3.8", text)
        self.assertIn("4.5.1", text)
        self.assertIn("MIT", text)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k TestVendoredLibraries -v`
Expected: FAIL — `vendor/bootstrap.min.css is missing`.

- [ ] **Step 3: Download the libraries**

```bash
mkdir -p web/vendor
curl -fsS -o web/vendor/bootstrap.min.css \
  https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.8/css/bootstrap.min.css
curl -fsS -o web/vendor/bootstrap.bundle.min.js \
  https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.8/js/bootstrap.bundle.min.js
curl -fsS -o web/vendor/chart.umd.min.js \
  https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.5.1/chart.umd.min.js

# Strip sourceMappingURL comments: the .map files are not vendored, so
# leaving the references produces a 404 in devtools for no benefit.
sed -i 's|/[*/]# sourceMappingURL=[^ ]*\( \*/\)\?||' \
  web/vendor/bootstrap.min.css \
  web/vendor/bootstrap.bundle.min.js \
  web/vendor/chart.umd.min.js
```

- [ ] **Step 4: Write `web/vendor/LICENSES.md`**

```markdown
# Vendored third-party libraries

Downloaded once from cdnjs and committed. They are read from disk at runtime;
nothing here contacts the network. Do not edit these files — to update a
library, replace the file and change the version recorded below.

| Library | Version | File | Licence |
|---|---|---|---|
| Bootstrap | 5.3.8 | `bootstrap.min.css`, `bootstrap.bundle.min.js` | MIT |
| Chart.js | 4.5.1 | `chart.umd.min.js` | MIT |

`sourceMappingURL` comments were stripped: the `.map` files are not vendored.

## Bootstrap — MIT

Copyright (c) 2011-2025 The Bootstrap Authors

## Chart.js — MIT

Copyright (c) 2014-2025 Chart.js Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./run-tests -v`
Expected: PASS — all tests, including the pre-existing ones.

- [ ] **Step 6: Commit**

```bash
git add web/vendor tests/test_web_assets.py
git commit -m "Vendor Bootstrap 5.3.8 and Chart.js 4.5.1

default-src 'self' would drop a CDN stylesheet silently, and a diagnostic
tool must work without Internet access. Downloaded once, committed, read
from disk at runtime.

The external-URL guard walked a hard-coded list of three files; it now
walks web/ recursively with vendor/ excluded, so a URL introduced in a
module that did not exist when the test was written is still caught."
```

---

## Task 2: Add every new message-catalogue key

Done before any rendering task: `test_all_catalogues_share_the_same_key_set`
requires both locales to move together, so adding keys piecemeal from six
different tasks would leave the suite red between commits.

**Files:**
- Modify: `web/i18n/en.json`
- Modify: `web/i18n/fr.json`
- Modify: `tests/test_i18n.py:13-27` (`REQUIRED_UI_KEYS`)

**Interfaces:**
- Consumes: nothing.
- Produces: the catalogue keys consumed by Tasks 5–9, listed below verbatim.

- [ ] **Step 1: Write the failing test**

In `tests/test_i18n.py`, replace `REQUIRED_UI_KEYS` with:

```python
REQUIRED_UI_KEYS = frozenset({
    "ui.title",
    "ui.mode.simple",
    "ui.mode.expert",
    "ui.mode.group",
    "ui.language",
    "ui.score.label",
    "ui.freshness.live",
    "ui.freshness.stale",
    "ui.probe.unavailable",
    "ui.probe.unavailable.why",
    "ui.probe.unavailable.raw_prefix",
    "ui.state.no_measurement",
    "ui.state.no_measurement.detail",
    # Theme control
    "ui.theme.label", "ui.theme.auto", "ui.theme.light", "ui.theme.dark",
    # Range control
    "ui.range.group", "ui.range.1h", "ui.range.24h", "ui.range.7d",
    "ui.range.90d", "ui.refresh",
    # Metric labels
    "ui.metric.cpu.usage", "ui.metric.load.1", "ui.metric.cpu.temp.pkg",
    "ui.metric.mem.available", "ui.metric.mem.available_pct",
    "ui.metric.mem.swap.used", "ui.metric.battery.charge_pct",
    "ui.metric.battery.wear_pct",
    # Chart card groups
    "ui.chart.group.cpu", "ui.chart.group.thermal",
    "ui.chart.group.memory", "ui.chart.group.battery",
    # Chart states
    "ui.chart.empty", "ui.chart.depth", "ui.chart.depth_short",
    "ui.chart.summary", "ui.chart.unavailable",
    # Expert sections
    "ui.expert.overview", "ui.expert.probes", "ui.expert.raw",
    # Errors
    "ui.error.history",
})
```

`ui.expert.placeholder` is deliberately gone: Expert mode stops being a
placeholder in Task 9.

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k test_required_ui_keys_are_present -v`
Expected: FAIL — `en lacks {'ui.theme.label', ...}`.

- [ ] **Step 3: Add the keys to `web/i18n/en.json`**

Remove the `"ui.expert.placeholder"` line, and insert after
`"ui.state.no_measurement.detail"`:

```json
  "ui.theme.label": "Theme",
  "ui.theme.auto": "Auto",
  "ui.theme.light": "Light",
  "ui.theme.dark": "Dark",

  "ui.range.group": "Time window",
  "ui.range.1h": "1 h",
  "ui.range.24h": "24 h",
  "ui.range.7d": "7 d",
  "ui.range.90d": "90 d",
  "ui.refresh": "Refresh",

  "ui.metric.cpu.usage": "Processor use",
  "ui.metric.load.1": "Load, 1 minute",
  "ui.metric.cpu.temp.pkg": "Processor temperature",
  "ui.metric.mem.available": "Memory available",
  "ui.metric.mem.available_pct": "Memory free",
  "ui.metric.mem.swap.used": "Moved to disk",
  "ui.metric.battery.charge_pct": "Battery charge",
  "ui.metric.battery.wear_pct": "Battery wear",

  "ui.chart.group.cpu": "Processor",
  "ui.chart.group.thermal": "Temperature",
  "ui.chart.group.memory": "Memory",
  "ui.chart.group.battery": "Battery",

  "ui.chart.empty": "Nothing recorded over this window yet.",
  "ui.chart.depth": "History available: {days} days.",
  "ui.chart.depth_short": "Only {days} days of history so far. Collection began recently — this is not a fault.",
  "ui.chart.summary": "{metric} over {range}: minimum {min}, maximum {max}, currently {current}.",
  "ui.chart.unavailable": "Charts are unavailable: the drawing library did not load. Every reading above is still current.",

  "ui.expert.overview": "Current readings",
  "ui.expert.probes": "Probes",
  "ui.expert.raw": "Raw data",

  "ui.error.history": "History could not be loaded.",
```

- [ ] **Step 4: Add the matching keys to `web/i18n/fr.json`**

Remove `"ui.expert.placeholder"`, and insert at the same position:

```json
  "ui.theme.label": "Thème",
  "ui.theme.auto": "Auto",
  "ui.theme.light": "Clair",
  "ui.theme.dark": "Sombre",

  "ui.range.group": "Fenêtre de temps",
  "ui.range.1h": "1 h",
  "ui.range.24h": "24 h",
  "ui.range.7d": "7 j",
  "ui.range.90d": "90 j",
  "ui.refresh": "Rafraîchir",

  "ui.metric.cpu.usage": "Utilisation du processeur",
  "ui.metric.load.1": "Charge sur 1 minute",
  "ui.metric.cpu.temp.pkg": "Température du processeur",
  "ui.metric.mem.available": "Mémoire disponible",
  "ui.metric.mem.available_pct": "Mémoire libre",
  "ui.metric.mem.swap.used": "Déplacé sur le disque",
  "ui.metric.battery.charge_pct": "Charge de la batterie",
  "ui.metric.battery.wear_pct": "Usure de la batterie",

  "ui.chart.group.cpu": "Processeur",
  "ui.chart.group.thermal": "Température",
  "ui.chart.group.memory": "Mémoire",
  "ui.chart.group.battery": "Batterie",

  "ui.chart.empty": "Rien d'enregistré sur cette fenêtre pour l'instant.",
  "ui.chart.depth": "Historique disponible : {days} jours.",
  "ui.chart.depth_short": "Seulement {days} jours d'historique à ce jour. La collecte a commencé récemment — ce n'est pas une panne.",
  "ui.chart.summary": "{metric} sur {range} : minimum {min}, maximum {max}, actuellement {current}.",
  "ui.chart.unavailable": "Les graphiques sont indisponibles : la librairie de tracé ne s'est pas chargée. Toutes les valeurs ci-dessus restent à jour.",

  "ui.expert.overview": "Valeurs actuelles",
  "ui.expert.probes": "Sondes",
  "ui.expert.raw": "Données brutes",

  "ui.error.history": "L'historique n'a pas pu être chargé.",
```

Note `ui.metric.mem.swap.used` deliberately reads "Moved to disk" / "Déplacé sur
le disque" rather than "Swap": the same plain-language rule the finding
catalogue already follows.

- [ ] **Step 5: Remove the last reference to the deleted key**

`web/app.js` still calls `translate("ui.expert.placeholder")` inside
`paintChrome()`, and `index.html` still holds `<p id="expert-placeholder">`.
Delete both lines now — `translate()` would otherwise log a missing-key warning
on every locale switch.

```bash
sed -i '/expert-placeholder/d' web/app.js web/index.html
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./run-tests -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web/i18n web/app.js web/index.html tests/test_i18n.py
git commit -m "Add the message-catalogue keys the redesigned interface needs

Both locales move together in one commit: the suite requires an identical
key set across catalogues, so adding keys task by task would leave it red
in between.

ui.expert.placeholder is dropped -- Expert mode stops being a placeholder."
```

---

## Task 3: Split `app.js` into ES modules

Behaviour-preserving refactor. Nothing the user sees changes; the page must look
and behave exactly as before at the end of this task.

**Files:**
- Create: `web/js/dom.js`, `web/js/i18n.js`, `web/js/stream.js`,
  `web/js/simple.js`, `web/js/app.js`
- Delete: `web/app.js`
- Modify: `web/index.html:50` (script `src`)
- Modify: `tests/test_web_assets.py` (`TestApp` — re-aim at the new modules)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `dom.js`: `el(id) -> HTMLElement`, `setText(node, text) -> void`
  - `i18n.js`: `translate(key, params={}) -> string`,
    `setLocale(target) -> Promise<void>`, `formatBytes(bytes) -> string`,
    `formatNumber(value) -> string`, `formatTime(date) -> string`,
    `currentLocale() -> string`, `loadFallback() -> Promise<void>`,
    `authHeaders() -> object`, `AVAILABLE_LOCALES`, `FALLBACK_LOCALE`
  - `stream.js`: `connect({onState, onFreshness}) -> void`,
    `hasMeasurement(state) -> boolean`,
    `isMeasurementStale(state) -> boolean`, `markStreamDown() -> void`
  - `simple.js`: `renderSimple(state) -> void`
  - `app.js`: `start() -> Promise<void>`, and `globalThis.healthConsole`

- [ ] **Step 1: Write the failing test**

In `tests/test_web_assets.py`, replace `class TestApp` wholesale:

```python
JS = WEB / "js"


def js(name):
    return (JS / name).read_text(encoding="utf-8")


class TestModuleLayout(unittest.TestCase):
    MODULES = ("app.js", "dom.js", "i18n.js", "stream.js", "simple.js")

    def test_every_module_exists(self):
        for name in self.MODULES:
            self.assertTrue((JS / name).is_file(), f"web/js/{name} is missing")

    def test_the_old_monolith_is_gone(self):
        self.assertFalse(
            (WEB / "app.js").exists(),
            "web/app.js still exists alongside web/js/ -- one of them is dead "
            "code, and the server would happily serve either")

    def test_stays_within_budget(self):
        # Measured across every module, not app.js alone: app.js is now ~80
        # lines of wiring, so a single-file assertion would pass while
        # guarding nothing.
        total = sum(path.stat().st_size for path in JS.glob("*.js"))
        self.assertLess(total, 60 * 1024,
                        f"60 KiB budget exceeded: {total} bytes")


class TestI18nModule(unittest.TestCase):
    def setUp(self):
        self.js = js("i18n.js")

    def test_default_locale_is_english(self):
        self.assertIn('FALLBACK_LOCALE = "en"', self.js)

    def test_locale_drives_intl_formatting(self):
        self.assertIn("Intl.NumberFormat", self.js)
        self.assertIn("Intl.DateTimeFormat", self.js)
        self.assertNotIn('Intl.NumberFormat("en"', self.js)

    def test_missing_key_falls_back_rather_than_showing_the_key(self):
        self.assertIn("FALLBACK_LOCALE", self.js)

    def test_catalogue_fetch_is_defensive(self):
        self.assertIn("response.ok", self.js)

    def test_byte_valued_finding_params_are_formatted_through_format_bytes(self):
        self.assertIn("formatBytes(value)", self.js)
        self.assertIn("BYTE_FINDING_PARAMS", self.js)


class TestStreamModule(unittest.TestCase):
    def setUp(self):
        self.js = js("stream.js")

    def test_freshness_stale_flag_is_not_hardcoded(self):
        self.assertIn("let isStale", self.js)
        self.assertNotRegex(
            self.js, r"markFreshness\([^)]*\bfalse\b[^)]*\)",
            "markFreshness is called with a hardcoded false")
        self.assertIn("isStale = true", self.js)
        self.assertIn("isStale = false", self.js)

    def test_freshness_considers_measurement_age_not_just_stream_state(self):
        self.assertIn("MEASUREMENT_STALE_AFTER_SECONDS", self.js)
        self.assertNotRegex(
            self.js, r"markFreshness\(\s*isStale\s*\)",
            "markFreshness is driven only by stream connectivity")

    def test_no_measurement_state_is_distinguished(self):
        self.assertIn("hasMeasurement", self.js)


class TestSimpleModule(unittest.TestCase):
    def setUp(self):
        self.js = js("simple.js")

    def test_no_measurement_state_is_rendered_distinctly(self):
        self.assertIn("ui.state.no_measurement", self.js)
        self.assertIn("renderNoMeasurement", self.js)

    def test_finding_params_are_formatted(self):
        self.assertIn("formatFindingParams(finding.params)", self.js)

    def test_probe_raw_reason_is_rendered_as_a_secondary_detail(self):
        self.assertIn("ui.probe.unavailable.why", self.js)
        self.assertIn("ui.probe.unavailable.raw_prefix", self.js)
        self.assertIn("raw-detail", self.js)


class TestAppModule(unittest.TestCase):
    def setUp(self):
        self.js = js("app.js")

    def test_default_mode_is_simple(self):
        self.assertIn('DEFAULT_MODE = "simple"', self.js)


class TestLiveRegionDiscipline(unittest.TestCase):
    """A verdict re-announced every 2 s is a screen reader talking forever."""

    def test_live_regions_are_not_repainted_unless_the_value_changed(self):
        self.assertIn("export function setText", js("dom.js"))
        for name in ("simple.js", "stream.js"):
            for direct in ('el("verdict-word").textContent =',
                          'el("verdict-sentence").textContent =',
                          'el("score").textContent =',
                          "zone.textContent ="):
                self.assertNotIn(direct, js(name),
                                 f"{name}: {direct} bypasses setText")


class TestNoHardCodedWording(unittest.TestCase):
    def test_no_user_facing_string_is_hard_coded(self):
        catalogue = json.loads(
            (WEB / "i18n" / "en.json").read_text(encoding="utf-8"))
        for path in JS.glob("*.js"):
            text = path.read_text(encoding="utf-8")
            for key, value in catalogue.items():
                if key.endswith(".icon") or len(value) < 8:
                    continue
                self.assertNotIn(value, text,
                                 f"{path.name} hard-codes the wording of {key}")

    def test_catalogue_keys_are_referenced_by_prefix(self):
        joined = "".join(js(p.name) for p in JS.glob("*.js"))
        for prefix in ("finding.", "severity.", "verdict.", "ui."):
            self.assertIn(prefix, joined)
```

Also update `TestIndex.test_scripts_and_styles_are_local` to expect the new
path:

```python
    def test_scripts_and_styles_are_local(self):
        self.assertIn('href="/static/style.css"', self.html)
        self.assertIn('src="/static/js/app.js"', self.html)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k TestModuleLayout -v`
Expected: FAIL — `web/js/app.js is missing`.

- [ ] **Step 3: Create `web/js/dom.js`**

```javascript
// Element access and the write discipline the live regions depend on.

export function el(id) { return document.getElementById(id); }

// #verdict-word, #verdict-sentence, #score and #freshness sit inside two
// aria-live="polite" regions and are repainted on every SSE event (every
// 2s). Assigning textContent unconditionally makes a screen reader re-read
// the whole verdict forever, even when nothing changed -- spec 10.2 asks
// for live regions "without screen-reader chatter". Only actually writing
// when the value changed keeps the live region silent the rest of the time.
export function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

export function clear(node) { node.textContent = ""; }
```

- [ ] **Step 4: Create `web/js/i18n.js`**

Move, unchanged in behaviour, from the old `app.js`: `FALLBACK_LOCALE`,
`AVAILABLE_LOCALES`, `TOKEN_STORAGE_KEY`, `catalogue`, `fallback`, `locale`,
`numberFormat`, `timeFormat`, `adoptTokenFromUrl`, `authHeaders`, `pickLocale`,
`translate`, `formatValue`, `formatBytes`, `BYTE_FINDING_PARAMS`,
`formatFindingParams`, `loadCatalogue`, `setLocale`.

Changes required while moving:

```javascript
export const FALLBACK_LOCALE = "en";
export const AVAILABLE_LOCALES = ["en", "fr"];

export function currentLocale() { return locale; }
export function formatNumber(value) { return numberFormat.format(value); }
export function formatTime(date) { return timeFormat.format(date); }

// setLocale() previously called paintChrome() and render() directly. It now
// notifies a listener instead, so i18n.js does not depend on the modules
// that render -- which would be a cycle.
let onLocaleChange = () => {};
export function whenLocaleChanges(handler) { onLocaleChange = handler; }

export async function loadFallback() {
  fallback = await loadCatalogue(FALLBACK_LOCALE);
}
```

and at the end of `setLocale`, replace `paintChrome(); if (lastState) render(lastState);` with `onLocaleChange();`.

- [ ] **Step 5: Create `web/js/stream.js`**

Move `STALE_AFTER_MS`, `MEASUREMENT_STALE_AFTER_SECONDS`, `lastState`,
`lastUpdate`, `isStale`, `interfaceTextUnavailable`, `hasMeasurement`,
`isMeasurementStale`, `markFreshness`, `connect`. Export
`hasMeasurement`, `isMeasurementStale`, `markFreshness`,
`setInterfaceTextUnavailable`, `lastKnownState()`, and `connect({onState})`
which calls the supplied handler instead of importing `render` directly.

- [ ] **Step 6: Create `web/js/simple.js`**

Move `card`, `renderNoMeasurement`, `render` (renamed `renderSimple`), and the
probe-unavailable loop. Import `el`, `setText` from `dom.js` and `translate`,
`formatFindingParams` from `i18n.js`.

- [ ] **Step 7: Create `web/js/app.js`**

```javascript
// Start-up and control wiring. Everything else lives in its own module.

import { el } from "./dom.js";
import {
  adoptTokenFromUrl, authHeaders, loadFallback, pickLocale, setLocale,
  translate, whenLocaleChanges,
} from "./i18n.js";
import { connect, lastKnownState, markFreshness,
         setInterfaceTextUnavailable } from "./stream.js";
import { renderSimple } from "./simple.js";

const DEFAULT_MODE = "simple";

function paintChrome() {
  document.title = translate("ui.title");
  el("app-title").textContent = translate("ui.title");
  el("mode-simple").textContent = translate("ui.mode.simple");
  el("mode-expert").textContent = translate("ui.mode.expert");
  el("mode-group").setAttribute("aria-label", translate("ui.mode.group"));
  el("locale-label").textContent = translate("ui.language");
  el("score-label").textContent = translate("ui.score.label");
}

function switchMode(mode) {
  const simple = mode === "simple";
  el("simple").hidden = !simple;
  el("expert").hidden = simple;
  el("mode-simple").setAttribute("aria-selected", String(simple));
  el("mode-expert").setAttribute("aria-selected", String(!simple));
  localStorage.setItem("mode", mode);
}

function render(state) { renderSimple(state); }

async function start() {
  adoptTokenFromUrl();
  el("mode-simple").addEventListener("click", () => switchMode("simple"));
  el("mode-expert").addEventListener("click", () => switchMode("expert"));
  el("locale").addEventListener("change", (event) =>
    setLocale(event.target.value));
  switchMode(localStorage.getItem("mode") || DEFAULT_MODE);
  whenLocaleChanges(() => {
    paintChrome();
    const state = lastKnownState();
    if (state) render(state);
  });
  try {
    await loadFallback();
    await setLocale(pickLocale());
    try {
      render(await (await fetch("/api/now", { headers: authHeaders() })).json());
    } catch (error) {
      console.warn("initial state unavailable", error);
    }
  } catch (error) {
    // The catalogue itself is what failed here, so there is nothing left
    // to translate this sentence with: every other text node in
    // index.html starts empty, and without this literal sentence the
    // viewer would see a blank page with no sign anything is wrong. Do
    // not "fix" this back to a translate() call.
    console.warn("catalogue unavailable, interface text cannot be shown", error);
    setInterfaceTextUnavailable();
    el("freshness").textContent =
      "Interface text failed to load. Please reload the page.";
  }
  connect({ onState: render, onFreshness: markFreshness });
}

globalThis.healthConsole = { render, setLocale };
start();
```

- [ ] **Step 8: Point `index.html` at the new entry point and delete the monolith**

```bash
sed -i 's|src="/static/app.js"|src="/static/js/app.js"|' web/index.html
git rm web/app.js
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `./run-tests -v`
Expected: PASS.

- [ ] **Step 10: Verify the page still works, unchanged**

Run: `./bin/health-console run`, open `http://127.0.0.1:8787/`.
Expected: identical to before — verdict, score, findings, freshness banner,
Simple/Expert switch, language switch. Check the browser console is free of
module-resolution errors. `Ctrl+C`.

- [ ] **Step 11: Commit**

```bash
git add web tests/test_web_assets.py
git commit -m "Split the front end into ES modules

app.js did i18n, streaming, freshness, rendering and wiring in 343 lines,
and the redesign would have pushed one file past 900. Native ES modules,
still no build step.

Behaviour is unchanged. The budget test now measures every module rather
than app.js alone, which is about to become 80 lines of wiring and would
have passed while guarding nothing."
```

---

## Task 4: Unit-test the pure front-end logic with `node --test`

**Optional — the one addition beyond the spec.** Drop this task and Tasks 5–10
still work. See "A note on how the front end is tested" above.

**Files:**
- Create: `web/js/tests/i18n.test.js`
- Create: `tests/test_js_units.py`

**Interfaces:**
- Consumes: `formatBytes` from `web/js/i18n.js`.
- Produces: a runner that later tasks add cases to
  (`web/js/tests/*.test.js`, discovered automatically).

- [ ] **Step 1: Write the failing test**

`tests/test_js_units.py`:

```python
"""Run the front-end unit tests through node's built-in runner.

The console needs no JavaScript runtime -- it ships HTML, CSS and ES modules
the browser executes. node is a *test-time* convenience only, so this module
skips rather than fails when it is absent, and no package.json is created:
node --test needs neither.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_TESTS = ROOT / "web" / "js" / "tests"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed; front-end unit tests skipped")
class TestJavaScriptUnits(unittest.TestCase):
    def test_node_test_suite_passes(self):
        result = subprocess.run(
            [NODE, "--test", str(JS_TESTS)],
            capture_output=True, text=True, cwd=ROOT, timeout=120)
        self.assertEqual(
            result.returncode, 0,
            f"node --test failed:\n{result.stdout}\n{result.stderr}")
```

`web/js/tests/i18n.test.js`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";

import { formatBytes } from "../i18n.js";

test("formatBytes climbs units and stops at the right one", () => {
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(1024), "1 kB");
  assert.equal(formatBytes(1024 ** 3), "1 GB");
});

test("formatBytes does not promote a value below the next unit", () => {
  assert.equal(formatBytes(1023), "1,023 B");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k TestJavaScriptUnits -v`
Expected: FAIL — importing `../i18n.js` throws, because `i18n.js` touches
`localStorage` and `document` at module scope.

- [ ] **Step 3: Make `i18n.js` importable outside a browser**

Move every `localStorage` and `document` access out of module top level and into
the functions that need them. `formatBytes`, `translate` and `formatValue` must
not touch either. Verify by reading the module: no statement outside a function
body may reference `document`, `localStorage`, `location` or `navigator`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run-tests -k TestJavaScriptUnits -v`
Expected: PASS (or SKIP on a machine without node).

- [ ] **Step 5: Commit**

```bash
git add web/js/tests tests/test_js_units.py web/js/i18n.js
git commit -m "Unit-test the pure front-end logic with node --test

Every front-end test so far asserts that a string appears in a source file,
which catches structural regressions and no behaviour at all. node --test
needs no package.json and no dependency, so the pure logic can be tested
for real.

node is a test-time convenience: the suite skips when it is absent, and the
console itself still needs no JavaScript runtime."
```

---

## Task 5: Rebuild the page shell on Bootstrap, with a theme control

**Files:**
- Modify: `web/index.html` (rewritten)
- Modify: `web/style.css` (rewritten as a thin layer)
- Modify: `web/js/app.js` (theme wiring)
- Modify: `tests/test_web_assets.py` (`TestIndex`, `TestStyle`)

**Interfaces:**
- Consumes: `ui.theme.*` from Task 2; vendored assets from Task 1.
- Produces: DOM ids `verdict-icon`, `verdict-word`, `verdict-sentence`,
  `score`, `score-label`, `findings`, `freshness`, `simple`, `expert`,
  `mode-simple`, `mode-expert`, `mode-group`, `locale`, `locale-label`,
  `app-title`, `theme-group`, `theme-auto`, `theme-light`, `theme-dark`,
  `verdict-gauge` (canvas), and the Expert containers `expert-tiles`,
  `range-group`, `refresh`, `chart-grid`, `probe-table`, `raw`.
  `applyTheme(choice)` and `resolvedTheme() -> "light"|"dark"` exported from
  `app.js`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_web_assets.py`:

```python
class TestBootstrapShell(unittest.TestCase):
    def setUp(self):
        self.html = read("index.html")

    def test_vendored_assets_are_referenced_locally(self):
        for reference in ('href="/static/vendor/bootstrap.min.css"',
                          'src="/static/vendor/bootstrap.bundle.min.js"',
                          'src="/static/vendor/chart.umd.min.js"'):
            self.assertIn(reference, self.html)

    def test_no_inline_style_attribute(self):
        # default-src 'self' drops style="..." silently -- the browser
        # reports nothing, the declaration simply never applies.
        self.assertNotRegex(
            self.html, r'\sstyle="',
            "index.html carries an inline style attribute, which the CSP drops")

    def test_theme_control_is_present(self):
        for element_id in ("theme-auto", "theme-light", "theme-dark"):
            self.assertIn(f'id="{element_id}"', self.html)

    def test_expert_containers_are_present(self):
        for element_id in ("expert-tiles", "range-group", "refresh",
                           "chart-grid", "probe-table", "raw"):
            self.assertIn(f'id="{element_id}"', self.html)

    def test_the_expert_placeholder_paragraph_is_gone(self):
        self.assertNotIn('id="expert-placeholder"', self.html)


class TestNoInlineStyleInCode(unittest.TestCase):
    def test_our_code_never_sets_the_style_attribute(self):
        # el.style.width = "..." is CSSOM and permitted; setAttribute("style")
        # is an inline style attribute and is dropped by the CSP.
        for path in JS.glob("*.js"):
            self.assertNotIn(
                'setAttribute("style"', path.read_text(encoding="utf-8"),
                f"{path.name} sets the style attribute, which the CSP drops")
```

Update `TestStyle` — `prefers-color-scheme` still applies (for `auto`), and
`min-height: 44px` must survive the rewrite.

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k TestBootstrapShell -v`
Expected: FAIL — `'href="/static/vendor/bootstrap.min.css"' not found`.

- [ ] **Step 3: Rewrite `web/index.html`**

```html
<!doctype html>
<html lang="en" data-bs-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Health Console</title>
  <link rel="stylesheet" href="/static/vendor/bootstrap.min.css">
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header class="bar border-bottom">
    <h1 id="app-title" class="h6 m-0"></h1>
    <div class="controls">
      <div class="btn-group" id="mode-group" role="tablist">
        <button type="button" class="btn btn-outline-secondary" id="mode-simple"
                role="tab" aria-selected="true" aria-controls="simple"></button>
        <button type="button" class="btn btn-outline-secondary" id="mode-expert"
                role="tab" aria-selected="false" aria-controls="expert"></button>
      </div>
      <div class="btn-group" id="theme-group">
        <button type="button" class="btn btn-outline-secondary" id="theme-auto"></button>
        <button type="button" class="btn btn-outline-secondary" id="theme-light"></button>
        <button type="button" class="btn btn-outline-secondary" id="theme-dark"></button>
      </div>
      <label class="locale-field">
        <span id="locale-label" class="text-body-secondary small"></span>
        <select id="locale" class="form-select form-select-sm">
          <option value="en">English</option>
          <option value="fr">Français</option>
        </select>
      </label>
    </div>
  </header>

  <p id="freshness" class="freshness alert alert-light m-0 rounded-0 border-0"
     aria-live="polite"></p>

  <main id="simple" class="view" role="tabpanel" aria-labelledby="mode-simple">
    <section class="verdict card text-center" aria-live="polite">
      <div class="card-body">
        <canvas id="verdict-gauge" width="180" height="180" aria-hidden="true"></canvas>
        <p class="verdict-state h4 mb-2">
          <span id="verdict-icon" aria-hidden="true"></span>
          <span id="verdict-word"></span>
        </p>
        <p class="verdict-sentence text-body-secondary mb-3" id="verdict-sentence"></p>
        <p class="verdict-score m-0">
          <span id="score-label" class="text-body-secondary"></span>
          <strong id="score">—</strong><span aria-hidden="true">/100</span>
        </p>
      </div>
    </section>
    <div id="findings"></div>
  </main>

  <main id="expert" class="view view-wide" role="tabpanel"
        aria-labelledby="mode-expert" hidden>
    <div id="expert-tiles" class="row g-2 mb-3"></div>

    <div class="controls mb-3">
      <div class="btn-group" id="range-group" role="group"></div>
      <button type="button" class="btn btn-outline-secondary" id="refresh"></button>
    </div>

    <div id="chart-grid" class="row g-3"></div>

    <div class="accordion mt-3" id="expert-accordion">
      <div class="accordion-item">
        <h2 class="accordion-header">
          <button class="accordion-button collapsed" type="button"
                  data-bs-toggle="collapse" data-bs-target="#probe-panel"
                  id="probes-heading"></button>
        </h2>
        <div id="probe-panel" class="accordion-collapse collapse"
             data-bs-parent="#expert-accordion">
          <div class="accordion-body"><table id="probe-table" class="table table-sm m-0"></table></div>
        </div>
      </div>
      <div class="accordion-item">
        <h2 class="accordion-header">
          <button class="accordion-button collapsed" type="button"
                  data-bs-toggle="collapse" data-bs-target="#raw-panel"
                  id="raw-heading"></button>
        </h2>
        <div id="raw-panel" class="accordion-collapse collapse"
             data-bs-parent="#expert-accordion">
          <div class="accordion-body"><pre id="raw" class="m-0 small"></pre></div>
        </div>
      </div>
    </div>
  </main>

  <script src="/static/vendor/bootstrap.bundle.min.js"></script>
  <script src="/static/vendor/chart.umd.min.js"></script>
  <script type="module" src="/static/js/app.js"></script>
</body>
</html>
```

- [ ] **Step 4: Rewrite `web/style.css` as a thin layer**

```css
/* What Bootstrap does not do: severity colours, the two-mode layout, and
   the dimming that says the data has frozen. Everything else is Bootstrap. */

:root,
:root[data-bs-theme="light"] {
  --hc-ok: #1c6b3a;
  --hc-info: #2c5a8a;
  --hc-attention: #8a5a00;
  --hc-urgent: #a02020;
  --hc-grid: rgba(0, 0, 0, 0.08);
}

:root[data-bs-theme="dark"] {
  --hc-ok: #6cc48d;
  --hc-info: #8bb8e8;
  --hc-attention: #e0b060;
  --hc-urgent: #f08080;
  --hc-grid: rgba(255, 255, 255, 0.12);
}

.bar {
  display: flex; flex-wrap: wrap; gap: 1rem;
  align-items: center; justify-content: space-between;
  padding: 0.75rem 1.25rem;
}

.controls { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; }
.locale-field { display: flex; gap: 0.5rem; align-items: center; }

/* Touch targets: the framework's default buttons are shorter than the 44 px
   the design document requires. */
.btn, .form-select { min-height: 44px; }
#mode-group .btn, #theme-group .btn { min-width: 88px; }
#range-group .btn { min-width: 60px; }

.view { max-width: 46rem; margin: 0 auto; padding: 1.25rem; }
.view-wide { max-width: 76rem; }

#verdict-gauge { max-width: 180px; height: auto; margin-bottom: 0.5rem; }

.finding { border-left-width: 4px !important; margin-top: 1rem; }
.finding .tag { font-weight: 700; font-size: 0.85rem; }
/* Diagnostic material (a probe's raw, untranslated failure reason), kept
   visible but visually secondary to the translated explanation above it. */
.finding .raw-detail { margin-top: 0.5rem; font-size: 0.8rem; opacity: 0.8; }

.severity-OK        { border-left-color: var(--hc-ok) !important; }
.severity-OK .tag   { color: var(--hc-ok); }
.severity-INFO      { border-left-color: var(--hc-info) !important; }
.severity-INFO .tag { color: var(--hc-info); }
.severity-ATTENTION      { border-left-color: var(--hc-attention) !important; }
.severity-ATTENTION .tag { color: var(--hc-attention); }
.severity-URGENT         { border-left-color: var(--hc-urgent) !important; }
.severity-URGENT .tag    { color: var(--hc-urgent); }

.freshness.stale { color: var(--hc-attention); font-weight: 600; }
body.stale .view { opacity: 0.45; }

.chart-holder { position: relative; height: 200px; }
.sparkline-holder { position: relative; height: 44px; margin-top: 0.5rem; }

/* Retained for the "auto" theme setting, which resolves through this query. */
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; } }

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
```

- [ ] **Step 5: Add theme wiring to `web/js/app.js`**

```javascript
const THEME_KEY = "theme";
const THEMES = ["auto", "light", "dark"];

export function resolvedTheme() {
  const choice = localStorage.getItem(THEME_KEY) || "auto";
  if (choice !== "auto") return choice;
  return globalThis.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark" : "light";
}

const themeListeners = [];
export function whenThemeChanges(handler) { themeListeners.push(handler); }

export function applyTheme(choice) {
  const wanted = THEMES.includes(choice) ? choice : "auto";
  localStorage.setItem(THEME_KEY, wanted);
  document.documentElement.setAttribute("data-bs-theme", resolvedTheme());
  for (const id of THEMES) {
    el(`theme-${id}`).classList.toggle("active", id === wanted);
    el(`theme-${id}`).setAttribute("aria-pressed", String(id === wanted));
  }
  for (const handler of themeListeners) handler();
}
```

Wire it in `start()`:

```javascript
  for (const id of THEMES) {
    el(`theme-${id}`).addEventListener("click", () => applyTheme(id));
  }
  applyTheme(localStorage.getItem(THEME_KEY) || "auto");
  // "auto" must follow the system while the page is open, not only at load.
  globalThis.matchMedia("(prefers-color-scheme: dark)")
    .addEventListener("change", () => applyTheme(
      localStorage.getItem(THEME_KEY) || "auto"));
```

and extend `paintChrome()`:

```javascript
  el("theme-group").setAttribute("aria-label", translate("ui.theme.label"));
  el("theme-auto").textContent = translate("ui.theme.auto");
  el("theme-light").textContent = translate("ui.theme.light");
  el("theme-dark").textContent = translate("ui.theme.dark");
  el("refresh").textContent = translate("ui.refresh");
  el("range-group").setAttribute("aria-label", translate("ui.range.group"));
  el("probes-heading").textContent = translate("ui.expert.probes");
  el("raw-heading").textContent = translate("ui.expert.raw");
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./run-tests -v`
Expected: PASS.

- [ ] **Step 7: Verify in the browser**

Run the console. Check: the page is styled by Bootstrap; the three theme buttons
switch light/dark and the choice survives a reload; `auto` follows the system
setting when changed live; Simple/Expert still switch; **the browser console
reports no CSP violation**.

- [ ] **Step 8: Commit**

```bash
git add web tests/test_web_assets.py
git commit -m "Rebuild the page shell on Bootstrap and add a theme control

style.css drops from a full stylesheet to the severity colours, the layout
and the freshness dimming -- what the framework does not do.

The theme was prefers-color-scheme only, which left the reader no say. It
is now auto/light/dark, remembered, with auto still following the system
live rather than only at load.

No inline style attribute anywhere: default-src 'self' drops those
silently, so a test now guards both the markup and our own code."
```

---

## Task 6: `history.js` — fetching, caching, and an honest error taxonomy

**Files:**
- Create: `web/js/history.js`
- Create: `web/js/tests/history.test.js`
- Modify: `tests/test_web_assets.py`

**Interfaces:**
- Consumes: `authHeaders` from `i18n.js`.
- Produces:
  - `RANGES` — `["1h", "24h", "7d", "90d"]`
  - `cacheKey(metric, range) -> string`
  - `fetchSeries(metric, range, {force}) -> Promise<Series>` where
    `Series = {points: [[ts, value], …], depthDays: number, table: string}`
  - `HistoryError` — thrown on any non-OK response or network failure
  - `clearCache() -> void`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_web_assets.py`:

```python
class TestRangeAgreement(unittest.TestCase):
    """The front end and the server must agree on the window identifiers."""

    def test_front_end_ranges_match_the_server(self):
        from healthconsole.server import RANGES as SERVER_RANGES
        source = js("history.js")
        declared = re.search(r"export const RANGES = \[([^\]]*)\]", source)
        self.assertIsNotNone(declared, "history.js declares no RANGES")
        front = set(re.findall(r'"([^"]+)"', declared.group(1)))
        self.assertEqual(
            front, set(SERVER_RANGES),
            "history.js and server.py disagree on the window identifiers; "
            "a mismatch yields a 400 the user sees as an empty chart")

```

`web/js/tests/history.test.js`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";

import { RANGES, cacheKey } from "../history.js";

test("the four windows the server accepts are declared", () => {
  assert.deepEqual([...RANGES].sort(), ["1h", "24h", "7d", "90d"].sort());
});

test("cache keys separate metric from range unambiguously", () => {
  assert.notEqual(cacheKey("cpu.usage", "1h"), cacheKey("cpu.usage", "24h"));
  assert.notEqual(cacheKey("cpu.usage", "1h"), cacheKey("cpu", "usage1h"));
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k "TestRangeAgreement or TestJavaScriptUnits" -v`
Expected: FAIL — `web/js/history.js` does not exist.

- [ ] **Step 3: Create `web/js/history.js`**

```javascript
// History fetching. One cache entry per (metric, window); the server does
// the aggregation, this module only avoids asking twice for the same thing.

import { authHeaders } from "./i18n.js";

// Must match RANGES in healthconsole/server.py -- a mismatch is a 400 the
// user reads as an empty chart. A test asserts the agreement.
export const RANGES = ["1h", "24h", "7d", "90d"];

export class HistoryError extends Error {}

const cache = new Map();

export function cacheKey(metric, range) { return `${metric} ${range}`; }

export function clearCache() { cache.clear(); }

export async function fetchSeries(metric, range, { force = false } = {}) {
  const key = cacheKey(metric, range);
  if (!force && cache.has(key)) return cache.get(key);

  let response;
  try {
    response = await fetch(
      `/api/history?metric=${encodeURIComponent(metric)}`
      + `&range=${encodeURIComponent(range)}`,
      { headers: authHeaders() });
  } catch (cause) {
    // A network failure is not an empty series: the caller must be able to
    // tell "nothing was recorded" from "we could not ask".
    throw new HistoryError(`network failure for ${metric}`, { cause });
  }
  if (!response.ok) {
    throw new HistoryError(`${metric} ${range}: HTTP ${response.status}`);
  }
  const body = await response.json();
  const series = {
    points: body.points || [],
    depthDays: body.depth_days ?? 0,
    table: body.table || "",
  };
  cache.set(key, series);
  return series;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run-tests -v`
Expected: PASS. `history.js` imports only `authHeaders`, so it stands alone —
nothing here waits on `charts.js`.

- [ ] **Step 5: Commit**

```bash
git add web/js/history.js web/js/tests/history.test.js tests/test_web_assets.py
git commit -m "Add the history layer

/api/history has been finished, tested and called from nowhere. One cache
entry per (metric, window), and a HistoryError that a caller can tell apart
from an empty series -- 'we could not ask' and 'nothing was recorded' are
different facts, and only one of them is about the machine's health.

The window identifiers are asserted against RANGES in server.py: a
mismatch is a 400 the reader would see as an empty chart."
```

---

## Task 7: `charts.js` — drawing, theming, decimation, accessibility, absence

**Files:**
- Create: `web/js/charts.js`
- Create: `web/js/tests/charts.test.js`

**Interfaces:**
- Consumes: `translate`, `formatNumber` from `i18n.js`; `Series` from
  `history.js`.
- Produces:
  - `chartsAvailable() -> boolean`
  - `summarise(points) -> {min, max, current} | null`
  - `shouldDecimate(points) -> boolean`
  - `themeColour(name) -> string`
  - `drawLine(canvas, {datasets, range}) -> Chart | null`
  - `drawSparkline(canvas, points, colour) -> Chart | null`
  - `drawGauge(canvas, score, colour) -> Chart | null`
  - `describeSeries(metricKey, range, points) -> string` (the `aria-label`)
  - `destroyAll() -> void`

- [ ] **Step 1: Write the failing test**

`web/js/tests/charts.test.js`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";

import { summarise, shouldDecimate } from "../charts.js";

test("summarise reports the extremes and the latest value", () => {
  const points = [[100, 12], [160, 87], [220, 34]];
  assert.deepEqual(summarise(points), { min: 12, max: 87, current: 34 });
});

test("summarise returns null rather than inventing zeros for no data", () => {
  // A chart with no points must say "nothing recorded", never draw a flat
  // line at zero -- a flat line is a measurement, absence is not.
  assert.equal(summarise([]), null);
  assert.equal(summarise(undefined), null);
});

test("summarise ignores null samples rather than treating them as zero", () => {
  assert.deepEqual(summarise([[100, null], [160, 5], [220, 9]]),
                   { min: 5, max: 9, current: 9 });
});

test("decimation switches on above a thousand points", () => {
  assert.equal(shouldDecimate(new Array(999).fill([0, 0])), false);
  assert.equal(shouldDecimate(new Array(1001).fill([0, 0])), true);
});
```

Add to `tests/test_web_assets.py`:

```python
class TestChartsDegradeGracefully(unittest.TestCase):
    def test_missing_chart_library_does_not_take_the_page_down(self):
        source = js("charts.js")
        self.assertIn("chartsAvailable", source)
        self.assertIn("globalThis.Chart", source)

    def test_charts_carry_a_text_alternative(self):
        # A <canvas> is invisible to a screen reader.
        source = js("charts.js")
        self.assertIn('role", "img"', source)
        self.assertIn("ui.chart.summary", source)

    def test_reduced_motion_disables_chart_animation(self):
        self.assertIn("prefers-reduced-motion", js("charts.js"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k "TestChartsDegradeGracefully or TestJavaScriptUnits" -v`
Expected: FAIL — `web/js/charts.js` does not exist.

- [ ] **Step 3: Create `web/js/charts.js`**

```javascript
// Chart.js wrapper. Everything that knows about canvases lives here, so the
// rest of the front end keeps working when the library does not load.

import { translate, formatNumber } from "./i18n.js";

// Above this, a raw draw saturates the canvas and stutters on a phone: 90
// days of 5-minute aggregates is ~26,000 points, and even 24 h from the raw
// table at a 30 s step is ~2,900. Tied to the point count rather than to
// which table the server chose, so both cases are covered.
const DECIMATION_THRESHOLD = 1000;

const live = new Set();

export function chartsAvailable() {
  return typeof globalThis.Chart === "function";
}

export function summarise(points) {
  if (!points || points.length === 0) return null;
  const values = points
    .map(([, value]) => value)
    .filter((value) => typeof value === "number" && Number.isFinite(value));
  if (values.length === 0) return null;
  return {
    min: Math.min(...values),
    max: Math.max(...values),
    current: values[values.length - 1],
  };
}

export function shouldDecimate(points) {
  return Boolean(points) && points.length > DECIMATION_THRESHOLD;
}

export function themeColour(name) {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
}

function animationsAllowed() {
  return !globalThis.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function describeSeries(metricKey, range, points) {
  const summary = summarise(points);
  if (!summary) return translate("ui.chart.empty");
  return translate("ui.chart.summary", {
    metric: translate(`ui.metric.${metricKey}`),
    range: translate(`ui.range.${range}`),
    min: formatNumber(summary.min),
    max: formatNumber(summary.max),
    current: formatNumber(summary.current),
  });
}

function register(chart) { live.add(chart); return chart; }

export function destroyAll() {
  for (const chart of live) chart.destroy();
  live.clear();
}

function baseOptions(points) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: animationsAllowed() && { duration: 200 },
    parsing: false,
    normalized: true,
    plugins: {
      legend: { display: false },
      decimation: {
        enabled: shouldDecimate(points),
        algorithm: "lttb",
        samples: 500,
      },
    },
  };
}

export function drawLine(canvas, { datasets, range, points }) {
  if (!chartsAvailable()) return null;
  const options = baseOptions(points);
  options.plugins.legend = { display: datasets.length > 1 };
  options.scales = {
    x: {
      type: "linear",
      // No date adapter: the tick labels are formatted through the same
      // Intl.DateTimeFormat the rest of the page uses, so chart dates follow
      // the active locale without a second formatting system to keep in sync.
      ticks: { callback: (value) => formatTick(value, range), maxTicksLimit: 6 },
      grid: { color: themeColour("--hc-grid") },
    },
    y: { grid: { color: themeColour("--hc-grid") }, beginAtZero: false },
  };
  return register(new globalThis.Chart(canvas, {
    type: "line", data: { datasets }, options,
  }));
}

export function drawSparkline(canvas, points, colour) {
  if (!chartsAvailable()) return null;
  const options = baseOptions(points);
  options.plugins.tooltip = { enabled: false };
  options.scales = { x: { display: false, type: "linear" },
                     y: { display: false } };
  options.elements = { point: { radius: 0 } };
  return register(new globalThis.Chart(canvas, {
    type: "line",
    data: { datasets: [{ data: points.map(([x, y]) => ({ x, y })),
                         borderColor: colour, borderWidth: 2,
                         fill: false, tension: 0.3 }] },
    options,
  }));
}

export function drawGauge(canvas, score, colour) {
  if (!chartsAvailable()) return null;
  return register(new globalThis.Chart(canvas, {
    type: "doughnut",
    data: {
      datasets: [{
        data: [score, 100 - score],
        backgroundColor: [colour, themeColour("--hc-grid")],
        borderWidth: 0,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: true, cutout: "72%",
      animation: animationsAllowed() && { duration: 300 },
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
    },
  }));
}

// A <canvas> is invisible to a screen reader, so every chart carries a
// sentence saying what it shows.
export function labelChart(canvas, text) {
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", text);
}
```

Add `formatTick`, which needs `i18n.js` to expose the active
`Intl.DateTimeFormat`:

```javascript
import { formatTime, formatDate } from "./i18n.js";

function formatTick(unixSeconds, range) {
  const when = new Date(unixSeconds * 1000);
  return (range === "1h" || range === "24h")
    ? formatTime(when) : formatDate(when);
}
```

and add to `i18n.js`:

```javascript
let dateFormat = new Intl.DateTimeFormat(locale, { day: "2-digit", month: "short" });
export function formatDate(date) { return dateFormat.format(date); }
```

remembering to rebuild `dateFormat` alongside `numberFormat` and `timeFormat`
inside `setLocale`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run-tests -v`
Expected: PASS. `charts.js` is imported by nothing yet, so the suite stays
green; Task 8 is what puts it on screen.

- [ ] **Step 5: Commit**

```bash
git add web/js tests/test_web_assets.py
git commit -m "Add the history and chart layers

No Chart.js date adapter: a linear scale over Unix timestamps with tick
labels formatted through the Intl.DateTimeFormat the page already holds.
Two fewer dependencies, and chart dates follow the active locale by the
same path as every other date.

summarise() returns null for an empty series rather than zeros, so a caller
can tell 'nothing recorded' from 'everything is zero' -- and charts.js
disables itself when the library is absent instead of taking the page with
it.

Decimation is keyed to the point count, not to the table the server chose:
24 h from the raw table is ~2,900 points and needs it as much as 90 d does."
```

---

## Task 8: Simple mode — verdict gauge and finding sparklines

**Files:**
- Modify: `web/js/simple.js`
- Modify: `tests/test_web_assets.py`

**Interfaces:**
- Consumes: `drawGauge`, `drawSparkline`, `labelChart`, `describeSeries`,
  `themeColour`, `chartsAvailable` from `charts.js`; `fetchSeries`,
  `HistoryError` from `history.js`.
- Produces: `renderSimple(state) -> void` (unchanged signature),
  `FINDING_METRIC` — the finding-id to metric-key map.

- [ ] **Step 1: Write the failing test**

```python
class TestSimpleModeCharts(unittest.TestCase):
    def setUp(self):
        self.js = js("simple.js")

    def test_incoherent_battery_gets_no_sparkline(self):
        # Drawing a curve of values the console has just declared
        # untrustworthy would be the exact lie the project refuses to tell.
        self.assertIn("FINDING_METRIC", self.js)
        self.assertNotRegex(
            self.js, r'"battery\.incoherent"\s*:\s*"',
            "battery.incoherent is mapped to a metric; it must not be")

    def test_every_mapped_finding_id_is_one_the_rules_emit(self):
        from healthconsole.findings import FINDING_IDS
        mapped = set(re.findall(r'"([a-z]+\.[a-z_]+)":\s*"[a-z]', self.js))
        self.assertLessEqual(
            mapped, set(FINDING_IDS),
            f"simple.js maps finding ids no rule emits: {mapped - set(FINDING_IDS)}")

    def test_the_gauge_is_hidden_from_screen_readers(self):
        # The score sits beside it as text; announcing it twice is noise.
        self.assertIn("verdict-gauge", self.js)
        self.assertIn('aria-hidden', read("index.html"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k TestSimpleModeCharts -v`
Expected: FAIL — `FINDING_METRIC` not in `simple.js`.

- [ ] **Step 3: Add the mapping and the drawing to `simple.js`**

```javascript
import { chartsAvailable, drawGauge, drawSparkline, describeSeries,
         labelChart, themeColour } from "./charts.js";
import { fetchSeries } from "./history.js";

// Which recorded metric illustrates which finding. battery.incoherent is
// deliberately absent: the console has just declared those readings
// untrustworthy, and drawing them anyway would be the lie it refuses.
const FINDING_METRIC = {
  "cpu.usage_high": "cpu.usage",
  "thermal.high": "cpu.temp.pkg",
  "thermal.critical": "cpu.temp.pkg",
  "memory.pressure": "mem.available_pct",
  "battery.wear": "battery.wear_pct",
};

const SEVERITY_COLOUR = {
  OK: "--hc-ok", INFO: "--hc-info",
  ATTENTION: "--hc-attention", URGENT: "--hc-urgent",
};

// The sparkline is illustration, not measurement: if the history call
// fails, the card keeps its verdict and simply shows no curve. Never an
// error banner over a finding that is itself perfectly valid.
async function attachSparkline(article, findingId, severity) {
  const metric = FINDING_METRIC[findingId];
  if (!metric || !chartsAvailable()) return;
  let series;
  try {
    series = await fetchSeries(metric, "1h");
  } catch (error) {
    console.warn(`sparkline unavailable for ${findingId}`, error);
    return;
  }
  if (series.points.length === 0) return;
  const holder = document.createElement("div");
  holder.className = "sparkline-holder";
  const canvas = document.createElement("canvas");
  holder.append(canvas);
  article.append(holder);
  labelChart(canvas, describeSeries(metric, "1h", series.points));
  drawSparkline(canvas, series.points,
                themeColour(SEVERITY_COLOUR[severity] || "--hc-info"));
}
```

In `renderSimple`, after appending each finding card, call
`attachSparkline(article, finding.id, finding.severity)`. Draw the verdict
gauge with `drawGauge(el("verdict-gauge"), state.score, themeColour(SEVERITY_COLOUR[severity]))`,
destroying the previous gauge first so repeated SSE events do not stack
charts.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run-tests -v` → PASS.

- [ ] **Step 5: Verify in the browser**

Simple mode shows the gauge and, where the machine has a live finding, a
sparkline. Stop the collector mid-view and confirm the page dims and the
freshness banner still says since when.

- [ ] **Step 6: Commit**

```bash
git add web/js tests/test_web_assets.py
git commit -m "Draw the verdict gauge and per-finding sparklines

battery.incoherent maps to no metric on purpose: the console has just told
the reader those numbers cannot be trusted, and a curve drawn from them
would take the claim back.

A failed history call leaves the card without a curve rather than putting
an error over a finding that is itself perfectly valid."
```

---

## Task 9: Expert mode — tiles, range control, chart grid, depth, accordion

**Files:**
- Modify: `web/js/expert.js` (currently the Task 7 stub)
- Modify: `web/js/app.js` (route state to Expert, wire range and refresh)
- Modify: `tests/test_web_assets.py`

**Interfaces:**
- Consumes: everything from `charts.js` and `history.js`.
- Produces: `renderExpert(state) -> void`, `setRange(range) -> Promise<void>`,
  `currentRange() -> string`, `redrawCharts() -> Promise<void>`.

- [ ] **Step 1: Write the failing test**

```python
class TestExpertMode(unittest.TestCase):
    def setUp(self):
        self.js = js("expert.js")

    def test_depth_is_reported_not_silently_swallowed(self):
        # A three-quarters-empty 90 d chart reads as a collection failure
        # when history has simply just begun. depth_days already comes back
        # from the server; it must reach the reader.
        self.assertIn("depthDays", self.js)
        self.assertIn("ui.chart.depth_short", self.js)

    def test_absent_data_is_not_drawn_as_zero(self):
        self.assertIn("ui.chart.empty", self.js)

    def test_a_failed_history_call_is_reported(self):
        self.assertIn("ui.error.history", self.js)

    def test_missing_library_is_announced_rather_than_left_blank(self):
        self.assertIn("ui.chart.unavailable", self.js)

    def test_the_tile_row_is_not_a_live_region(self):
        # Tiles change every 2 s; aria-live there is continuous chatter.
        self.assertNotIn("aria-live", self.js)


class TestMetricKeysExist(unittest.TestCase):
    """A typo in a metric key raises nothing -- it yields an empty chart.

    Placed here rather than with history.js: it reads every module that
    names a metric, and expert.js is the last of them to be written.
    """

    KNOWN = {
        "cpu.usage", "cpu.freq", "load.1", "cpu.temp.pkg",
        "mem.available", "mem.available_pct", "mem.swap.used",
        "battery.charge_pct", "battery.wear_pct",
    }

    def test_every_referenced_metric_key_is_one_a_probe_emits(self):
        source = js("charts.js") + js("expert.js") + js("simple.js")
        used = set(re.findall(r'"((?:cpu|mem|load|battery)\.[a-z0-9_.]+)"',
                              source))
        unknown = used - self.KNOWN
        self.assertEqual(unknown, set(),
                         f"referenced metric keys no probe emits: {unknown}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run-tests -k TestExpertMode -v`
Expected: FAIL — the stub contains none of it.

- [ ] **Step 3: Write `web/js/expert.js`**

```javascript
// The dense view: current readings, then history over one window shared by
// every chart, then the material of last resort.

import { el, clear, setText } from "./dom.js";
import { translate, formatNumber, formatBytes } from "./i18n.js";
import { RANGES, fetchSeries, clearCache } from "./history.js";
import { chartsAvailable, drawLine, describeSeries, labelChart,
         themeColour, destroyAll } from "./charts.js";

const DEFAULT_RANGE = "24h";
let range = DEFAULT_RANGE;

// Tile key -> [metric key, how to format it]
const TILES = [
  ["cpu.usage", (v) => `${formatNumber(v)} %`],
  ["cpu.temp.pkg", (v) => `${formatNumber(v)} °C`],
  ["mem.available", (v) => formatBytes(v)],
  ["mem.swap.used", (v) => formatBytes(v)],
  ["battery.charge_pct", (v) => `${formatNumber(v)} %`],
  ["load.1", (v) => formatNumber(v)],
];

const CHART_CARDS = [
  ["ui.chart.group.cpu", ["cpu.usage", "load.1"], "--hc-info"],
  ["ui.chart.group.thermal", ["cpu.temp.pkg"], "--hc-urgent"],
  ["ui.chart.group.memory", ["mem.available_pct", "mem.swap.used"], "--hc-ok"],
  ["ui.chart.group.battery", ["battery.charge_pct", "battery.wear_pct"], "--hc-attention"],
];

export function currentRange() { return range; }

export function paintRangeControl(onChange) {
  const group = el("range-group");
  clear(group);
  for (const id of RANGES) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-outline-secondary";
    button.textContent = translate(`ui.range.${id}`);
    button.setAttribute("aria-pressed", String(id === range));
    button.classList.toggle("active", id === range);
    button.addEventListener("click", () => { range = id; onChange(); });
    group.append(button);
  }
}
```

Then `renderTiles(state)`, which reads the live measurements out of
`state.probes` and writes one Bootstrap column per entry of `TILES` — **no
`aria-live`**, per §10 of the spec — and `redrawCharts()`:

```javascript
export async function redrawCharts() {
  const grid = el("chart-grid");
  clear(grid);
  destroyAll();

  if (!chartsAvailable()) {
    grid.append(notice("alert-warning", translate("ui.chart.unavailable")));
    return;
  }

  for (const [titleKey, metrics, colourVar] of CHART_CARDS) {
    const card = chartCard(translate(titleKey));
    grid.append(card.column);
    let series;
    try {
      series = await Promise.all(metrics.map((m) => fetchSeries(m, range)));
    } catch (error) {
      console.warn("history unavailable", error);
      card.body.append(notice("alert-danger", translate("ui.error.history")));
      continue;
    }
    const drawable = series.filter((s) => s.points.length > 0);
    if (drawable.length === 0) {
      // Not a flat line at zero: a flat line is a measurement, and the
      // absence of one is not.
      card.body.append(notice("alert-light", translate("ui.chart.empty")));
      continue;
    }
    const canvas = document.createElement("canvas");
    const holder = document.createElement("div");
    holder.className = "chart-holder";
    holder.append(canvas);
    card.body.append(holder);
    drawLine(canvas, {
      range,
      points: drawable[0].points,
      datasets: metrics.map((metric, index) => ({
        label: translate(`ui.metric.${metric}`),
        data: series[index].points.map(([x, y]) => ({ x, y })),
        borderColor: themeColour(index === 0 ? colourVar : "--hc-info"),
        borderWidth: 2, fill: false, tension: 0.25, pointRadius: 0,
      })),
    });
    labelChart(canvas, describeSeries(metrics[0], range, drawable[0].points));
    card.body.append(depthLine(Math.min(...series.map((s) => s.depthDays))));
  }
}

// depth_days comes back from every /api/history response and was ignored.
// Without this line, a 90 d chart holding 6 days reads as a collection
// failure rather than as a history that has simply just begun.
function depthLine(depthDays) {
  const paragraph = document.createElement("p");
  paragraph.className = "small text-body-secondary mt-2 mb-0";
  const requested = { "1h": 1 / 24, "24h": 1, "7d": 7, "90d": 90 }[range];
  paragraph.textContent = translate(
    depthDays + 0.05 < requested ? "ui.chart.depth_short" : "ui.chart.depth",
    { days: depthDays });
  return paragraph;
}
```

plus small helpers `notice(variant, text)` and `chartCard(title)`, and
`renderProbeTable(state)` / `el("raw").textContent = JSON.stringify(state, null, 2)`.

`renderExpert(state)` calls `renderTiles(state)` and `renderProbeTable(state)`
on every SSE event; `redrawCharts()` runs only on a range change, on refresh,
on a theme change, and on entering Expert mode — never on the 2-second tick.

- [ ] **Step 4: Wire it into `app.js`**

```javascript
import { renderExpert, paintRangeControl, redrawCharts,
         currentRange } from "./expert.js";

function render(state) {
  renderSimple(state);
  if (!el("expert").hidden) renderExpert(state);
}
```

In `start()`:

```javascript
  paintRangeControl(redrawCharts);
  el("refresh").addEventListener("click", () => { clearCache(); redrawCharts(); });
  whenThemeChanges(() => { if (!el("expert").hidden) redrawCharts(); });
```

and in `switchMode`, when switching to Expert, call `redrawCharts()` once.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./run-tests -v` → PASS, including `TestMetricKeysExist`.

- [ ] **Step 6: Verify in the browser**

Expert mode: tiles update every 2 s; the four range buttons redraw every chart;
refresh re-fetches; the accordion opens the probe table and the raw JSON; the
layout collapses to one column at phone width. Rename
`web/vendor/chart.umd.min.js` temporarily and reload — **the page must still
render everything except charts**, with the `ui.chart.unavailable` notice.
Rename it back.

- [ ] **Step 7: Commit**

```bash
git add web/js tests/test_web_assets.py
git commit -m "Build the Expert mode the placeholder promised

Tiles on the 2 s stream, a range selector shared by every chart so metrics
are comparable over one window, and the probe table and raw JSON behind an
accordion.

/api/history has always returned depth_days and the front end ignored it.
A 90 d chart holding 6 days now says so, because otherwise it reads as a
collection failure rather than a history that has just begun.

The tile row is deliberately not a live region: it changes every 2 s, and
aria-live there is a screen reader talking without pause."
```

---

## Task 10: Correct the documentation the redesign falsified

**Files:**
- Modify: `docs/superpowers/specs/2026-08-29-health-console-design.md` (§10.5)
- Modify: `README.md` (Architecture section)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Rewrite §10.5 of the design document**

Replace the section body with:

```markdown
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
```

- [ ] **Step 2: Rewrite the README's Architecture paragraph**

Replace the last paragraph of that section with:

```markdown
The collector runs at two cadences: 2 seconds for what is cheap to read
(`/proc`, `/sys`), 5 minutes for what is expensive (SMART, APT, systemd). The
HTTP server streams state over SSE. The front end is HTML, CSS and native ES
modules, built on Bootstrap and Chart.js — both **vendored and served from
disk**, never fetched from a CDN, so the console works with no network at all.
```

- [ ] **Step 3: Run the full suite one last time**

Run: `./run-tests -v`
Expected: PASS, nothing skipped except `TestJavaScriptUnits` if node is absent.

- [ ] **Step 4: Manual verification checklist**

Run `./bin/health-console run` and confirm each:

- [ ] Simple mode: verdict, gauge, score, findings, sparklines
- [ ] Expert mode: tiles, all four ranges, refresh, accordion, depth line
- [ ] Theme auto / light / dark, each surviving a reload
- [ ] French and English, including chart tick labels and the `aria-label`
- [ ] Phone width (device toolbar, 390 px): both modes single-column
- [ ] Stop the collector: the page dims and states since when
- [ ] Browser console: no CSP violation, no 404, no module error

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/specs/2026-08-29-health-console-design.md
git commit -m "Correct the two documents the redesign falsified

Design document 10.5 called for hand-drawn SVG charts and counted every
byte against a 60 KiB budget; the README's Architecture section said the
same. Both now describe what the code does: vendored libraries served from
disk, and the 60 KiB budget narrowed to code we write.

'No CDN' was and remains true, and is now stated with the reason -- a
remote stylesheet fails silently under default-src 'self'."
```

---

## Self-review

**Spec coverage.** §2.1 vendoring → Task 1. §2.2 budget → Tasks 1, 3. §3 layout
→ Task 3. §4.1 no date adapter → Task 7. §4.2 CSP → Task 5. §4.3 no Bootstrap
Icons → Task 5 (none referenced). §5 Simple mode → Task 8. §6 Expert mode →
Task 9. §7 theme → Task 5. §8 controls → Tasks 5, 9. §9 i18n → Task 2. §10
accessibility → Tasks 7, 8, 9. §11 degraded modes → Tasks 6, 7, 8, 9. §12
decimation → Task 7. §13 testing → Tasks 1, 3, 6, 7, 9. §14 documentation →
Task 10. §15 risks — no task needed. §16 out of scope — nothing to build.

**Task independence.** Every task leaves the suite green on its own. The metric-key guard moved from Task 6 to Task 9 for that reason: it reads `expert.js`, so introducing it earlier would have held the suite red across three commits.

**Type consistency.** `fetchSeries` returns `{points, depthDays, table}`
throughout. `summarise` returns `{min, max, current}` or `null`, checked for
null at every call site. `themeColour` takes a CSS custom-property name with
its leading `--` at every call. `RANGES` is declared once, in `history.js`, and
imported everywhere else.
