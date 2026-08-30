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


if __name__ == "__main__":
    unittest.main()
