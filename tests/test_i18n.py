import json
import re
import unittest
from pathlib import Path

from healthconsole.actions import ACTION_IDS
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
    # Metric hints: a short, visible caption beside each Expert-mode tile
    # (never a title="" tooltip -- see web/js/expert.js's renderTiles).
    "ui.metric.cpu.usage.hint", "ui.metric.cpu.temp.pkg.hint",
    "ui.metric.mem.available.hint", "ui.metric.mem.swap.used.hint",
    "ui.metric.battery.charge_pct.hint", "ui.metric.load.1.hint",
    # Chart card groups
    "ui.chart.group.cpu", "ui.chart.group.thermal",
    "ui.chart.group.memory", "ui.chart.group.battery",
    # Chart states. Only the "short" wording exists: the depth line exists
    # to explain a short chart, and on a full window it instead named the
    # table's own depth_days -- contradicting itself across range buttons
    # on any machine whose raw retention is shorter than its 90d history.
    "ui.chart.empty", "ui.chart.depth_short", "ui.chart.depth_short_day",
    "ui.chart.depth_short_hours", "ui.chart.depth_short_hour",
    "ui.chart.summary", "ui.chart.unavailable",
    # Expert sections
    "ui.expert.overview", "ui.expert.probes", "ui.expert.raw",
    "ui.expert.probe_table.name", "ui.expert.probe_table.status",
    "ui.expert.probe_table.detail",
    # Errors
    "ui.error.history",
    # Actions tab
    "ui.mode.actions", "ui.actions.available", "ui.actions.audit",
    "ui.actions.none", "ui.actions.run", "ui.actions.running",
    "ui.actions.output", "ui.actions.confirm.title",
    "ui.actions.confirm.cancel", "ui.actions.confirm.go",
    "ui.actions.result.ok", "ui.actions.result.failed",
    "ui.actions.result.killed",
    "ui.actions.audit.when", "ui.actions.audit.what",
    "ui.actions.audit.source", "ui.actions.audit.outcome",
    "ui.actions.audit.empty",
    "ui.error.action.busy", "ui.error.action.refused",
    "ui.error.action.failed",
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


class TestFindingParamsAreShown(unittest.TestCase):
    NUMERIC_FINDINGS = ("cpu.usage_high", "memory.pressure")

    def test_every_declared_parameter_is_interpolated_somewhere(self):
        # cpu.usage_high declared usage_pct/sustain_minutes and interpolated
        # neither; memory.pressure declared available_bytes/available_pct/
        # swap_used_bytes and showed only the percentage. A declared
        # parameter that no template ever shows is data the user is denied.
        for locale in locales():
            catalogue = load(locale)
            for finding_id in self.NUMERIC_FINDINGS:
                used: set[str] = set()
                for suffix in ("title", "why"):
                    text = catalogue[f"finding.{finding_id}.{suffix}"]
                    used |= set(PLACEHOLDER.findall(text))
                self.assertEqual(
                    used, set(FINDING_PARAMS[finding_id]),
                    f"{locale}:finding.{finding_id} does not interpolate "
                    f"every declared parameter {FINDING_PARAMS[finding_id]}")


class TestNoJargonLeaksToSimpleMode(unittest.TestCase):
    JARGON = ("swap", "sysfs", "hwmon", "SMART", "psutil", "charge_full")

    def test_titles_and_reasons_avoid_raw_jargon(self):
        for locale in locales():
            catalogue = load(locale)
            for finding_id in sorted(FINDING_IDS):
                for suffix in ("title", "why"):
                    text = catalogue[f"finding.{finding_id}.{suffix}"]
                    # Strip placeholders before scanning: a declared
                    # parameter name like {swap_used_bytes} never reaches
                    # the reader -- it is replaced with a plausibility-
                    # checked, formatted value (see memory.pressure) -- so
                    # it is not jargon "leaking" into the prose a term
                    # appearing only inside the author's own wording is.
                    prose = PLACEHOLDER.sub("", text)
                    for term in self.JARGON:
                        self.assertNotIn(
                            term, prose,
                            f"{locale}:finding.{finding_id}.{suffix} leaks "
                            f"'{term}' into Simple mode")


class TestActionCatalogueWording(unittest.TestCase):
    def test_every_action_has_a_label_a_description_and_a_confirmation(self):
        for locale in locales():
            catalogue = load(locale)
            for action_id in sorted(ACTION_IDS):
                for suffix in ("label", "description", "confirm"):
                    key = f"action.{action_id}.{suffix}"
                    self.assertIn(key, catalogue, f"{locale} lacks {key}")

    def test_every_risk_level_has_a_word_and_an_icon(self):
        # Colour never carries the state alone.
        from healthconsole.actions import Risk
        for locale in locales():
            catalogue = load(locale)
            for risk in Risk:
                for suffix in ("word", "icon"):
                    key = f"risk.{risk.value}.{suffix}"
                    self.assertIn(key, catalogue, f"{locale} lacks {key}")


if __name__ == "__main__":
    unittest.main()
