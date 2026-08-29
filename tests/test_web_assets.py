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
        # tab to the panel it toggles.
        self.assertIn('aria-controls="simple"', self.html)
        self.assertIn('aria-controls="expert"', self.html)
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
        self.assertIn('"en"', self.js)

    def test_default_mode_is_simple(self):
        self.assertIn('"simple"', self.js)

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

    def test_catalogue_fetch_is_defensive(self):
        # A missing or broken catalogue file must not silently blank the
        # page: the fetch is checked for failure and guarded by a try.
        self.assertIn("response.ok", self.js)
        self.assertRegex(
            self.js, r"try\s*\{[^}]*loadCatalogue\(",
            "loadCatalogue is not called inside a try block")


if __name__ == "__main__":
    unittest.main()
