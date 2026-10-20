import importlib.util
from pathlib import Path
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
