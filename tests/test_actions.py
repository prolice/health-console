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

    def test_b1_ships_exactly_one_action(self):
        # B1 is deliberately a thin slice: eleven of the design document's
        # thirteen actions have no inputs until their probes exist. If this
        # fails, either a probe landed and the spec needs updating, or an
        # action was added without one.
        self.assertEqual(set(CATALOGUE), {"apt.refresh"})

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

    def test_action_ids_matches_the_catalogue(self):
        self.assertEqual(ACTION_IDS, frozenset(CATALOGUE))
