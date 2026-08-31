import unittest

from healthconsole.actions import (
    ACTION_IDS, CATALOGUE, Risk, UnknownAction, lookup,
)

SHELL_METACHARACTERS = set(";|&$><`\n\\\"'*?[]{}()!~")


class TestCatalogueIsData(unittest.TestCase):
    """The catalogue is what someone reads to find out what this console
    can run on their machine. It must stay trivially auditable."""

    def test_every_argv_is_a_frozen_tuple(self):
        for action in CATALOGUE.values():
            self.assertIsInstance(action.argv, tuple, action.id)

    def test_every_argv_starts_with_an_absolute_path(self):
        # A bare name would resolve through PATH, which is attacker-
        # influenceable in ways an absolute path is not.
        for action in CATALOGUE.values():
            self.assertTrue(action.argv[0].startswith("/"), action.id)

    def test_no_argument_contains_a_shell_metacharacter(self):
        # shell=False means these would be harmless, but an argument that
        # needs quoting is a sign the catalogue is drifting towards being
        # a command line rather than a fixed argument list.
        for action in CATALOGUE.values():
            for argument in action.argv:
                self.assertFalse(
                    SHELL_METACHARACTERS & set(argument),
                    f"{action.id}: {argument!r}")

    def test_the_id_matches_its_key(self):
        for key, action in CATALOGUE.items():
            self.assertEqual(key, action.id)

    def test_current_catalogue_is_the_supported_safe_subset(self):
        self.assertEqual(set(CATALOGUE), {
            "apt.refresh", "apt.upgrade", "apt.security", "clean.aptcache",
        })

    def test_an_action_is_immutable(self):
        action = CATALOGUE["apt.refresh"]
        with self.assertRaises(Exception):
            action.argv = ("/bin/true",)


class TestLookup(unittest.TestCase):
    def test_a_known_id_returns_its_action(self):
        self.assertIs(lookup("apt.refresh"), CATALOGUE["apt.refresh"])

    def test_an_unknown_id_raises(self):
        with self.assertRaises(UnknownAction):
            lookup("rm.everything")

    def test_an_unhashable_id_raises_unknown_action_not_type_error(self):
        # The published contract is that any bad identifier -- not just
        # an unknown string -- raises UnknownAction. A list arriving from
        # a careless caller must not reach the dict subscript unguarded.
        with self.assertRaises(UnknownAction):
            lookup(["apt.refresh"])

    def test_action_ids_matches_the_catalogue(self):
        self.assertEqual(ACTION_IDS, frozenset(CATALOGUE))
