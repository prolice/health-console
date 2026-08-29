import json
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


class TestNoExternalResources(unittest.TestCase):
    """A diagnostic tool must work without Internet access."""

    def test_no_external_urls(self):
        for name in ("index.html", "style.css", "app.js"):
            for match in re.findall(r"https?://[^\s\"')]+", read(name)):
                self.fail(f"{name} references an external resource: {match}")

    def test_no_build_step_artefacts(self):
        self.assertFalse((WEB.parent / "package.json").exists())


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
        self.assertIn('src="/static/app.js"', self.html)

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


class TestStyle(unittest.TestCase):
    def setUp(self):
        self.css = read("style.css")

    def test_dark_theme_is_supported(self):
        self.assertIn("prefers-color-scheme", self.css)

    def test_reduced_motion_is_respected(self):
        self.assertIn("prefers-reduced-motion", self.css)

    def test_touch_targets_are_large_enough(self):
        self.assertIn("min-height: 44px", self.css)


class TestApp(unittest.TestCase):
    def setUp(self):
        self.js = read("app.js")

    def test_stays_within_budget(self):
        self.assertLess((WEB / "app.js").stat().st_size, 60 * 1024,
                        "60 KiB budget exceeded")

    def test_default_locale_is_english(self):
        # Pinned to the actual declaration: '"en"' alone matches any of
        # AVAILABLE_LOCALES, the locale <option value="en">, or plenty of
        # other unrelated strings, so this passed regardless of what
        # FALLBACK_LOCALE was actually set to.
        self.assertIn('FALLBACK_LOCALE = "en"', self.js)

    def test_default_mode_is_simple(self):
        self.assertIn('DEFAULT_MODE = "simple"', self.js)

    def test_no_user_facing_string_is_hard_coded(self):
        # Every sentence the user reads must come from a catalogue. If any
        # English catalogue value appears verbatim in the code, a translation
        # would silently fail to apply.
        catalogue = json.loads(
            (WEB / "i18n" / "en.json").read_text(encoding="utf-8"))
        for key, value in catalogue.items():
            if key.endswith(".icon") or len(value) < 8:
                continue
            self.assertNotIn(
                value, self.js,
                f"app.js hard-codes the wording of {key}")

    def test_catalogue_keys_are_referenced_by_prefix(self):
        for prefix in ("finding.", "severity.", "verdict.", "ui."):
            self.assertIn(prefix, self.js)

    def test_locale_drives_intl_formatting(self):
        # Numbers and times must follow the active locale, not a fixed one.
        self.assertIn("Intl.NumberFormat", self.js)
        self.assertIn("Intl.DateTimeFormat", self.js)
        self.assertNotIn('Intl.NumberFormat("en"', self.js)

    def test_missing_key_falls_back_rather_than_showing_the_key(self):
        self.assertIn("FALLBACK_LOCALE", self.js)

    def test_freshness_stale_flag_is_not_hardcoded(self):
        # The original defect was markFreshness(state.ts * 1000, false) — a
        # literal false that a locale switch mid-outage silently repainted
        # as "up to date". Guard against regressing to any hardcoded
        # boolean argument.
        self.assertIn("let isStale", self.js)
        self.assertNotRegex(
            self.js, r"markFreshness\([^)]*\bfalse\b[^)]*\)",
            "markFreshness is called with a hardcoded false")
        self.assertIn("isStale = true", self.js)
        self.assertIn("isStale = false", self.js)

    def test_catalogue_fetch_is_defensive(self):
        # A missing or broken catalogue file must not silently blank the
        # page: the fetch is checked for failure and guarded by a try.
        self.assertIn("response.ok", self.js)
        self.assertRegex(
            self.js, r"try\s*\{[^}]*loadCatalogue\(",
            "loadCatalogue is not called inside a try block")

    def test_freshness_considers_measurement_age_not_just_stream_state(self):
        # The original defect: markFreshness(isStale) re-emitted every 2s
        # whether or not scheduler.state() was still advancing, so a wedged
        # collector behind a healthy stream was shown as perpetually "up to
        # date". Freshness must also be a function of state.ts.
        self.assertIn("MEASUREMENT_STALE_AFTER_SECONDS", self.js)
        self.assertNotRegex(
            self.js, r"markFreshness\(\s*isStale\s*\)",
            "markFreshness is driven only by stream connectivity, "
            "ignoring state.ts")

    def test_no_measurement_state_is_rendered_distinctly(self):
        # EMPTY_STATE (ts=None, score=None) must never be shown as a score
        # or a verdict: it is the absence of a measurement, not a good one.
        self.assertIn("hasMeasurement", self.js)
        self.assertIn("ui.state.no_measurement", self.js)
        self.assertIn("renderNoMeasurement", self.js)

    def test_byte_valued_finding_params_are_formatted_through_format_bytes(self):
        # formatBytes was exported and called from nowhere: memory.pressure
        # declares available_bytes/swap_used_bytes but only ever showed a
        # percentage, the form spec §10.1 forbids ("9% of 481GB" rather
        # than "441GB left").
        self.assertIn("formatBytes(value)", self.js)
        self.assertIn("BYTE_FINDING_PARAMS", self.js)
        self.assertIn("formatFindingParams(finding.params)", self.js)

    def test_probe_raw_reason_is_rendered_as_a_secondary_detail(self):
        # The raw reason (English, diagnostic) must stay visible but must
        # not be the card's whole explanation, and must sit behind a
        # translated lead-in rather than standing alone untranslated.
        self.assertIn("ui.probe.unavailable.why", self.js)
        self.assertIn("ui.probe.unavailable.raw_prefix", self.js)
        self.assertIn("raw-detail", self.js)

    def test_live_regions_are_not_repainted_unless_the_value_changed(self):
        # #verdict-word, #verdict-sentence, #score and #freshness sit in
        # aria-live="polite" regions repainted on every SSE event (every
        # 2s); assigning textContent unconditionally makes a screen reader
        # re-read the whole verdict forever, even when nothing changed.
        self.assertIn("function setText", self.js)
        for direct in ('el("verdict-word").textContent =',
                      'el("verdict-sentence").textContent =',
                      'el("score").textContent =',
                      "zone.textContent ="):
            self.assertNotIn(
                direct, self.js,
                f"{direct} bypasses setText and re-announces unconditionally")


if __name__ == "__main__":
    unittest.main()
