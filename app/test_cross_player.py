import importlib.util
from collections import Counter
from pathlib import Path
import random
import time
import unittest

spec = importlib.util.spec_from_file_location("cross_player", Path(__file__).with_name("cross_player.py"))
cross_player = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cross_player)


class Item:
    def __init__(self, player, advancement=False, local=False):
        self.player, self.advancement, self.local, self.code, self.location = player, advancement, local, 1, None


class Location:
    progress_type = 1

    def __init__(self, player, item, locked=False):
        self.player, self.item, self.locked, self.address = player, item, locked, 1
        item.location = self

    def can_fill(self, state, item, check_access=False):
        return not item.local or item.player == self.player


class World:
    def __init__(self, locations):
        self.locations = locations

    def get_filled_locations(self):
        return self.locations


def run(locations, valid=lambda: True, players=(1, 2, 3)):
    return cross_player.prefer_other_players(World(locations), None, valid, set(players), log=lambda text: None)


def own(locations):
    return sum(location.item.player == location.player for location in locations)


class CrossPlayerTests(unittest.TestCase):
    def test_unequal_worlds_spread_rewards_instead_of_only_serving_the_largest(self):
        for already_optimized in (False, True):
            with self.subTest(already_optimized=already_optimized):
                if already_optimized:
                    locations = ([Location(p, Item(3)) for p in (1, 2) for _ in range(89)]
                                 + [Location(3, Item(p)) for p in (1, 2) for _ in range(89)]
                                 + [Location(3, Item(3)) for _ in range(26)])
                else:
                    locations = [Location(p, Item(p)) for p, size in ((1, 89), (2, 89), (3, 204))
                                 for _ in range(size)]
                items = {id(location.item) for location in locations}
                initial = own(locations)
                moved, remaining = run(locations)
                counts = Counter((location.player, location.item.player) for location in locations)
                for finder in (1, 2, 3):
                    for receiver in (1, 2, 3):
                        if finder != receiver:
                            self.assertGreaterEqual(counts[finder, receiver], 29)
                self.assertEqual(counts[1, 1], 0)
                self.assertEqual(counts[2, 2], 0)
                self.assertIn(remaining, (84, 86))
                self.assertEqual(moved, initial - remaining)
                self.assertEqual({id(location.item) for location in locations}, items)
                self.assertTrue(all(location.item.location is location for location in locations))

    def test_already_remote_rewards_are_spread_without_creating_self_items(self):
        locations = [Location(1, Item(2)) for _ in range(20)]
        locations += [Location(2, Item(3)) for _ in range(20)]
        locations += [Location(3, Item(1)) for _ in range(20)]
        self.assertEqual(run(locations), (0, 0))
        counts = Counter((location.player, location.item.player) for location in locations)
        for finder in (1, 2, 3):
            for receiver in (1, 2, 3):
                if finder != receiver:
                    self.assertEqual(counts[finder, receiver], 10)

    def test_spreading_keeps_rejected_progression_in_place(self):
        locations = ([Location(p, Item(3, advancement=True)) for p in (1, 2) for _ in range(8)]
                     + [Location(3, Item(p, advancement=True)) for p in (1, 2) for _ in range(8)]
                     + [Location(3, Item(3, advancement=True)) for _ in range(4)])
        original = [location.item for location in locations]
        self.assertEqual(run(locations, valid=lambda: False), (0, 4))
        self.assertEqual([location.item for location in locations], original)
        self.assertTrue(all(location.item.location is location for location in locations))

    def test_three_player_rotation_is_validated_and_rolled_back(self):
        locations = [Location(p, Item(p % 3 + 1, advancement=True))
                     for p in (1, 2, 3) for _ in range(13)]
        locations += [Location(p, Item((p + 1) % 3 + 1, advancement=True))
                      for p in (1, 2, 3) for _ in range(7)]
        original = [location.item for location in locations]
        validations = []

        def reject():
            validations.append(sum(location.item is not item for location, item in zip(locations, original)))
            return False

        run(locations, valid=reject)
        self.assertIn(3, validations)
        self.assertEqual([location.item for location in locations], original)
        self.assertTrue(all(location.item.location is location for location in locations))

    def test_expired_spread_budget_does_not_change_items(self):
        locations = [Location(p, Item(p % 3 + 1)) for p in (1, 2, 3)]
        original = [location.item for location in locations]
        cross_player.spread_rewards(locations, None, lambda: True, random.Random(0),
                                    time.monotonic() - 1, log=lambda text: None)
        self.assertEqual([location.item for location in locations], original)

    def test_balanced_worlds_only_unlock_for_others(self):
        locations = [Location(p, Item(p, advancement=i % 3 == 0)) for p in (1, 2, 3) for i in range(20)]
        self.assertEqual(run(locations), (60, 0))
        self.assertEqual(own(locations), 0)
        self.assertTrue(all(location.item.location is location for location in locations))

    def test_large_world_keeps_only_the_excess(self):
        locations = [Location(1, Item(1)) for _ in range(30)] + [Location(2, Item(2)) for _ in range(5)]
        moved, remaining = run(locations, players=(1, 2))
        self.assertEqual(moved, 10)
        self.assertEqual(remaining, 25)

    def test_rules_locks_and_rejected_progression_are_respected(self):
        local = Location(1, Item(1, local=True))
        locked = Location(1, Item(1), locked=True)
        progression = [Location(1, Item(1, advancement=True)), Location(2, Item(2, advancement=True))]
        # The locked location is not movable, so it is not counted.
        self.assertEqual(run([local, locked, *progression], valid=lambda: False, players=(1, 2)), (0, 3))
        self.assertEqual(own([local, locked, *progression]), 4)

    def test_failing_progression_swaps_are_isolated(self):
        bad = Item(1, advancement=True)
        locations = [Location(1, bad), Location(2, Item(2, advancement=True)),
                     Location(1, Item(1, advancement=True)), Location(2, Item(2, advancement=True))]
        moved, remaining = run(locations, valid=lambda: bad.location.player == 1, players=(1, 2))
        self.assertEqual((moved, remaining), (2, 2))
        self.assertEqual(bad.location.player, 1)


if __name__ == "__main__":
    unittest.main()
