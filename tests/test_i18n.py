import json
import re
import unittest
from pathlib import Path

from healthconsole.findings import FINDING_IDS, FINDING_PARAMS, Severity

I18N = Path(__file__).resolve().parent.parent / "web" / "i18n"
DEFAULT_LOCALE = "en"
PLACEHOLDER = re.compile(r"\{(\w+)\}")

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
    "ui.expert.placeholder",
})


def load(locale):
    return json.loads((I18N / f"{locale}.json").read_text(encoding="utf-8"))


def locales():
    return sorted(path.stem for path in I18N.glob("*.json"))


class TestCataloguesExist(unittest.TestCase):
    def test_english_and_french_are_present(self):
        self.assertIn("en", locales())
        self.assertIn("fr", locales())

    def test_every_catalogue_is_valid_json_and_flat(self):
        for locale in locales():
            for key, value in load(locale).items():
                self.assertIsInstance(value, str,
                                      f"{locale}:{key} is not a string")


class TestCompleteness(unittest.TestCase):
    def test_every_finding_has_a_title_and_a_why(self):
        for locale in locales():
            catalogue = load(locale)
            for finding_id in sorted(FINDING_IDS):
                for suffix in ("title", "why"):
                    key = f"finding.{finding_id}.{suffix}"
                    self.assertIn(key, catalogue, f"{locale} lacks {key}")

    def test_every_severity_has_a_word_an_icon_and_a_verdict(self):
        for locale in locales():
            catalogue = load(locale)
            for severity in Severity:
                for key in (f"severity.{severity.name}.word",
                            f"severity.{severity.name}.icon",
                            f"verdict.{severity.name}"):
                    self.assertIn(key, catalogue, f"{locale} lacks {key}")

    def test_required_ui_keys_are_present(self):
        for locale in locales():
            missing = REQUIRED_UI_KEYS - set(load(locale))
            self.assertEqual(missing, set(), f"{locale} lacks {missing}")

    def test_all_catalogues_share_the_same_key_set(self):
        # An extra key is as much a failure as a missing one: it means one
        # catalogue drifted.
        reference = set(load(DEFAULT_LOCALE))
        for locale in locales():
            self.assertEqual(set(load(locale)), reference,
                             f"{locale} diverges from {DEFAULT_LOCALE}")

    def test_no_value_is_empty(self):
        for locale in locales():
            for key, value in load(locale).items():
                self.assertTrue(value.strip(), f"{locale}:{key} is empty")


class TestPlaceholders(unittest.TestCase):
    def test_corresponding_entries_use_the_same_placeholders(self):
        reference = load(DEFAULT_LOCALE)
        for locale in locales():
            if locale == DEFAULT_LOCALE:
                continue
            catalogue = load(locale)
            for key, text in reference.items():
                self.assertEqual(
                    set(PLACEHOLDER.findall(text)),
                    set(PLACEHOLDER.findall(catalogue[key])),
                    f"{locale}:{key} uses different placeholders")

    def test_placeholders_match_what_the_probes_emit(self):
        # A template referring to {wear} when the probe emits {wear_pct}
        # would render a literal brace to the user.
        for locale in locales():
            catalogue = load(locale)
            for finding_id, allowed in FINDING_PARAMS.items():
                for suffix in ("title", "why"):
                    text = catalogue[f"finding.{finding_id}.{suffix}"]
                    used = set(PLACEHOLDER.findall(text))
                    self.assertLessEqual(
                        used, set(allowed),
                        f"{locale}:finding.{finding_id}.{suffix} uses "
                        f"{used - set(allowed)}, which the probe never emits")


class TestNoJargonLeaksToSimpleMode(unittest.TestCase):
    JARGON = ("swap", "sysfs", "hwmon", "SMART", "psutil", "charge_full")

    def test_titles_and_reasons_avoid_raw_jargon(self):
        for locale in locales():
            catalogue = load(locale)
            for finding_id in sorted(FINDING_IDS):
                for suffix in ("title", "why"):
                    text = catalogue[f"finding.{finding_id}.{suffix}"]
                    for term in self.JARGON:
                        self.assertNotIn(
                            term, text,
                            f"{locale}:finding.{finding_id}.{suffix} leaks "
                            f"'{term}' into Simple mode")


if __name__ == "__main__":
    unittest.main()
