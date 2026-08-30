import json
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


JS = WEB / "js"


def js(name):
    return (JS / name).read_text(encoding="utf-8")


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


class TestIndex(unittest.TestCase):
    def setUp(self):
        self.html = read("index.html")

    def test_default_language_is_english(self):
        self.assertIn('lang="en"', self.html)

    def test_has_a_viewport_for_phones(self):
        self.assertIn("viewport", self.html)

    def test_mode_switch_is_present(self):
        self.assertIn('id="mode-simple"', self.html)
        self.assertIn('id="mode-expert"', self.html)

    def test_language_selector_is_present(self):
        self.assertIn('id="locale"', self.html)

    def test_live_region_announces_changes(self):
        self.assertIn('aria-live="polite"', self.html)

    def test_scripts_and_styles_are_local(self):
        self.assertIn('href="/static/style.css"', self.html)
        self.assertIn('src="/static/js/app.js"', self.html)

    def test_no_hard_coded_aria_label(self):
        # An aria-label baked into the markup is a user-facing string that
        # cannot be translated: it must instead be set from the catalogue
        # at render time, like every other text node.
        match = re.search(r'aria-label="[^"]+"', self.html)
        self.assertIsNone(
            match, f"index.html hard-codes {match and match.group(0)}")

    def test_tabs_are_associated_with_their_panels(self):
        # role="tab" inside role="tablist" is not enough on its own: a
        # screen reader needs aria-controls/aria-labelledby to link each
        # tab to the panel it toggles. The association is bidirectional
        # or it is not an association: both directions are asserted.
        self.assertIn('aria-controls="simple"', self.html)
        self.assertIn('aria-controls="expert"', self.html)
        self.assertIn('aria-labelledby="mode-simple"', self.html)
        self.assertIn('aria-labelledby="mode-expert"', self.html)
        self.assertEqual(self.html.count('role="tabpanel"'), 2)


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


class TestStyle(unittest.TestCase):
    def setUp(self):
        self.css = read("style.css")

    def test_dark_theme_is_supported(self):
        self.assertIn("prefers-color-scheme", self.css)

    def test_reduced_motion_is_respected(self):
        self.assertIn("prefers-reduced-motion", self.css)

    def test_touch_targets_are_large_enough(self):
        self.assertIn("min-height: 44px", self.css)


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

    def test_charts_are_destroyed_before_their_container_is_cleared(self):
        # Chart.js keeps a live instance per canvas. renderSimple() repaints
        # on every SSE event (every 2s); a clear-then-rebuild that skips
        # destroyIn() first leaks one Chart.js instance per finding, per
        # tick, for as long as the page stays open.
        self.assertIn("destroyIn", self.js)
        rebuild = re.search(r"function rebuildFindings\([^)]*\)\s*\{([^}]*)",
                            self.js, re.DOTALL)
        self.assertIsNotNone(rebuild, "rebuildFindings() not found")
        body = rebuild.group(1)
        destroy_pos = body.find("destroyIn(")
        clear_pos = body.find('.textContent = ""')
        self.assertNotEqual(destroy_pos, -1, "rebuildFindings never calls destroyIn")
        self.assertNotEqual(clear_pos, -1, 'rebuildFindings never clears #findings')
        self.assertLess(destroy_pos, clear_pos,
                        "#findings is cleared before its charts are destroyed")

    def test_findings_are_not_rebuilt_when_nothing_changed(self):
        # A repaint that always tears down and rebuilds every card (and
        # re-fetches every sparkline) would replay the entry animation on
        # every 2s tick -- a visible pulse for any reader who has not asked
        # for reduced motion -- even when the findings never changed.
        self.assertIn("findingsSignature", self.js)
        self.assertIn("lastFindingsSignature", self.js)
        self.assertIn("currentLocale", self.js)

    def test_gauge_is_not_redrawn_when_nothing_changed(self):
        # The gauge is exactly as vulnerable to the every-2s pulse as the
        # cards are: destroying and recreating it on every tick replays its
        # entry animation even when the score has not moved, which reads as
        # the machine's health visibly wavering rather than as a repaint.
        self.assertIn("gaugeSignature", self.js)
        self.assertIn("lastGaugeSignature", self.js)
        # Its colours are baked into the canvas at draw time and do not
        # follow an updated CSS custom property on their own, so something
        # must be able to force a redraw when only the theme changed.
        self.assertIn("forceGaugeRedraw", self.js)

    def test_gauge_redraw_is_forced_on_a_theme_change(self):
        # Without this, a theme flip that does not also change the score
        # would leave the gauge showing the old theme's colours.
        app_js = js("app.js")
        self.assertIn("whenThemeChanges", app_js)
        self.assertIn("forceGaugeRedraw", app_js)

    def test_finding_cards_get_bootstrap_chrome(self):
        # Task 5 stripped style.css down to a thin layer over Bootstrap's
        # .card; .finding only narrows the left border, which paints
        # nothing without .card's own border-style. Without these classes a
        # finding has no background, no border, and no visible severity edge.
        self.assertIn('`finding card severity-${severity}`', self.js)
        self.assertIn('"card-body"', self.js)

    def test_sparkline_points_are_passed_through_without_extra_chart_options(self):
        # simple.js must not grow Chart.js knowledge of its own: normalized
        # is set inside drawSparkline() (charts.js), not here.
        self.assertNotIn("normalized", self.js)


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


class TestAppModule(unittest.TestCase):
    def setUp(self):
        self.js = js("app.js")

    def test_default_mode_is_simple(self):
        self.assertIn('DEFAULT_MODE = "simple"', self.js)

    def test_locale_change_redraws_expert_charts(self):
        # Card titles, legend labels, depth sentences and aria-labels are
        # all produced at chart-draw time; without this, switching locale
        # while Expert mode is open leaves every chart in the old language
        # until the reader happens to click a range button.
        handler = re.search(r"whenLocaleChanges\(\(\) => \{(.*?)\n  \}\);",
                            self.js, re.DOTALL)
        self.assertIsNotNone(handler, "whenLocaleChanges handler not found")
        self.assertIn("redrawCharts", handler.group(1))

    def test_a_stored_expert_mode_switches_in_after_the_first_state_arrives(self):
        # Switching mode earlier reads lastKnownState() as null, so the
        # thermal card silently falls back to cpu.temp.pkg alone --
        # indistinguishable from a machine that genuinely has no sensor
        # zones -- on every single reload of a stored "expert" mode.
        first_render = self.js.find('render(await (await fetch("/api/now"')
        switch_call = self.js.find('switchMode(localStorage.getItem("mode")')
        self.assertNotEqual(first_render, -1, "initial /api/now render not found")
        self.assertNotEqual(switch_call, -1, "initial switchMode call not found")
        self.assertLess(first_render, switch_call,
                        "switchMode runs before the first state has arrived")

    def test_switching_into_expert_mode_paints_the_snapshot_already_held(self):
        # render() only calls renderExpert() while #expert is already
        # visible, so the very first state (noted while Expert was still
        # hidden) is otherwise never painted into the tiles, the probe
        # table or #raw -- a reader whose stored mode is Expert would see
        # full charts sitting above an empty tile row and an empty probe
        # table until the next SSE tick, or forever if the stream never
        # connects. Parsed from switchMode() specifically (not just
        # anywhere in the file) so a call site outside the mode switch
        # would not satisfy this.
        switch_mode = re.search(r"function switchMode\(mode\)\s*\{(.*?)\n\}",
                                self.js, re.DOTALL)
        self.assertIsNotNone(switch_mode, "switchMode() not found")
        body = switch_mode.group(1)
        self.assertIn("renderExpert(state)", body)
        self.assertIn("redrawCharts()", body)


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

    def test_charts_are_destroyed_before_the_grid_is_cleared(self):
        # Chart.js keeps a live instance per canvas; clearing #chart-grid
        # first would detach the canvases without releasing them.
        redraw = re.search(
            r"export async function redrawCharts\(\)\s*\{(.*?)\n\}",
            self.js, re.DOTALL)
        self.assertIsNotNone(redraw, "redrawCharts() not found")
        body = redraw.group(1)
        destroy_pos = body.find("destroyIn(")
        clear_pos = body.find("clear(grid)")
        self.assertNotEqual(destroy_pos, -1, "redrawCharts never calls destroyIn")
        self.assertNotEqual(clear_pos, -1, "redrawCharts never clears #chart-grid")
        self.assertLess(destroy_pos, clear_pos,
                        "#chart-grid is cleared before its charts are destroyed")

    def test_a_generation_counter_guards_against_concurrent_redraws(self):
        # Two quick range clicks (or a click racing refresh, or a theme
        # change) must not interleave cards from two different windows.
        # Checked against the guard's actual comparison, not just the word
        # "generation" appearing somewhere -- deleting every comparison
        # while leaving the counter declared would otherwise still pass.
        redraw = re.search(
            r"export async function redrawCharts\(\)\s*\{(.*?)\n\}",
            self.js, re.DOTALL)
        self.assertIsNotNone(redraw, "redrawCharts() not found")
        body = redraw.group(1)
        guard_count = body.count("myGeneration !== generation")
        self.assertGreaterEqual(
            guard_count, 2,
            "redrawCharts must re-check its generation after every await, "
            "not just once, or a stale call can still append into a grid "
            "a newer call already started clearing")

    def test_thermal_card_is_built_from_the_live_zones_not_a_static_list(self):
        # thermal.acpitz, thermal.x86_pkg_temp, ... are discovered from the
        # kernel at runtime and differ per machine.
        self.assertIn("probes.thermal", self.js)
        self.assertIn("zones", self.js)
        self.assertIn("metricLabel", self.js)

    def test_a_changed_zone_set_redraws_the_thermal_card_once(self):
        # If /api/now fails on load, the thermal card falls back to
        # cpu.temp.pkg alone with no way to tell that apart from a machine
        # that genuinely has no zones -- nothing would otherwise ever
        # redraw it once the real zones become known from the first SSE
        # state. thermalZoneSignature() itself is exercised behaviourally
        # in expert.test.js; this only locks that renderExpert() actually
        # compares against it and calls redrawCharts() when it changed.
        render_expert = re.search(
            r"export function renderExpert\(state\)\s*\{(.*?)\n\}",
            self.js, re.DOTALL)
        self.assertIsNotNone(render_expert, "renderExpert() not found")
        body = render_expert.group(1)
        self.assertIn("thermalZoneSignature(state) !== lastDrawnZoneSignature", body)
        self.assertIn("redrawCharts()", body)

    def test_raw_json_has_exactly_one_owner(self):
        self.assertIn('el("raw").textContent', self.js)
        self.assertNotIn('el("raw").textContent', js("simple.js"))

    def test_depth_reflects_only_the_series_actually_drawn(self):
        # An empty series (already filtered out of `drawable`) always
        # reports depth_days: 0 from the server; folding it into the
        # minimum would print "0 days" under a chart that is, in fact,
        # full -- e.g. a Temperature card whose thermal.acpitz series came
        # back empty while cpu.temp.pkg held a month of readings.
        self.assertIn("drawable.map(({ series: s }) => s.depthDays)", self.js)
        self.assertNotIn("series.map((s) => s.depthDays)", self.js)

    def test_the_accessible_description_never_detaches_a_series_from_its_own_label(self):
        # A <canvas> has no other accessible content. Pairing metrics and
        # series by shared array index survives a filtered-out series only
        # if the filter is applied to (metric, series) pairs together --
        # never to the series alone while indexing metrics separately.
        self.assertIn("pair.series.points.length > 0", self.js)
        self.assertNotIn("describeSeries(metrics[0]", self.js)

    def test_each_series_in_a_card_gets_its_own_colour(self):
        # Two series sharing one card must not share one legend swatch --
        # colour is the only channel separating them.
        self.assertIn("COLOUR_PALETTE", self.js)
        self.assertNotIn("index === 0 ? colourVar", self.js)

    def test_depth_below_a_day_is_shown_in_hours_not_a_rounded_to_zero_day_count(self):
        self.assertIn("ui.chart.depth_short_hours", self.js)
        self.assertIn("ui.chart.depth_short_hour", self.js)

    def test_the_non_short_depth_wording_is_gone(self):
        # The depth line exists to explain a short chart; on a full window
        # it instead named the table's own depth_days, which reads as a
        # claim about the machine and contradicts itself across ranges (a
        # 24h chart says "2 days", a 90d chart on the same machine says "90
        # days"). Checked as an exact, quoted key so "ui.chart.depth_short"
        # (which does still exist, and contains "ui.chart.depth" as a
        # prefix) does not make this test vacuously pass.
        self.assertNotIn('"ui.chart.depth"', self.js)
        self.assertNotIn('"ui.chart.depth_hours"', self.js)

    def test_probe_table_has_a_header_row(self):
        self.assertIn("ui.expert.probe_table.name", self.js)
        self.assertIn("ui.expert.probe_table.status", self.js)
        self.assertIn("ui.expert.probe_table.detail", self.js)


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
        # (?!\s*:) excludes a match used as an object key -- simple.js's
        # FINDING_METRIC maps finding ids ("cpu.usage_high", "battery.wear")
        # to the metric they illustrate, and those ids happen to match this
        # same dotted shape without being metric keys themselves. Blind
        # spot: a genuine metric key immediately followed by a colon (e.g.
        # as a ternary's alternate, "cond ? a : "cpu.usage"") would be
        # skipped too. Nothing in this codebase writes a metric key that
        # way today, but the next reader should not have to rediscover it.
        used = set(re.findall(r'"((?:cpu|mem|load|battery)\.[a-z0-9_.]+)"(?!\s*:)',
                              source))
        unknown = used - self.KNOWN
        self.assertEqual(unknown, set(),
                         f"referenced metric keys no probe emits: {unknown}")


if __name__ == "__main__":
    unittest.main()
