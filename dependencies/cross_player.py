"""Moves self-placed items into other players' worlds after the main fill.

Swaps keep every item rule (local/non-local items, excluded locations) and only
keep progression swaps that leave the multiworld beatable with unchanged
accessibility. When perfect separation is impossible (e.g. one player has many
more locations than the others) the remaining items stay where they are.
"""
import random
import time

PRIORITY = 2  # BaseClasses.LocationProgressType.PRIORITY
PARTNER_ATTEMPTS = 300


def movable(location):
    item = location.item
    return (item is not None and location.address is not None and not location.locked
            and item.code is not None)


def is_self(location):
    return location.item.player == location.player


def accepts(location, item, state):
    if location.progress_type == PRIORITY and not item.advancement:
        return False
    return location.can_fill(state, item, check_access=False)


def swap(first, second):
    first.item, second.item = second.item, first.item
    first.item.location, second.item.location = first, second


def prefer_other_players(multiworld, state, valid, players, seed=0, budget=120.0, log=print):
    """Returns (moved, remaining) counts of self-placed items.

    `valid()` must return whether the current placement is still acceptable.
    """
    rng = random.Random(seed)
    deadline = time.monotonic() + budget
    locations = [location for location in multiworld.get_filled_locations()
                 if location.player in players and movable(location)]
    initial = sum(1 for location in locations if is_self(location))
    if len(players) < 2 or not initial:
        return 0, initial

    def committed(swaps):
        for first, second in swaps:
            swap(first, second)
        if valid():
            return len(swaps)
        for first, second in reversed(swaps):
            swap(first, second)
        if len(swaps) == 1 or time.monotonic() > deadline:
            return 0
        middle = len(swaps) // 2
        return committed(swaps[:middle]) + committed(swaps[middle:])

    for round_number in range(1, 6):
        rng.shuffle(locations)
        own = {player: [l for l in locations if l.player == player and is_self(l)] for player in players}
        if not any(own.values()):
            break
        used, free, checked = set(), [], []
        order = sorted(players, key=lambda p: -len(own[p]))
        # Prefer partners that are also self-placed (fixes two at once), then the same item class.
        pools = {}
        for player in order:
            candidates = [l for p in order if p != player for l in own[p]]
            candidates += [l for l in locations if l.player != player and not is_self(l)
                           and l.item.player != player]
            for advancement in (False, True):
                pools[player, advancement] = sorted(
                    candidates, key=lambda l: (not is_self(l), l.item.advancement != advancement))
        # Players with the most self-placed items pick partners first.
        for player in order:
            for first in own[player]:
                if first in used:
                    continue
                if time.monotonic() > deadline:
                    break
                attempts = 0
                for second in pools[player, bool(first.item.advancement)]:
                    if second in used:
                        continue
                    attempts += 1
                    if attempts > PARTNER_ATTEMPTS:
                        break
                    if accepts(first, second.item, state) and accepts(second, first.item, state):
                        used.update((first, second))
                        progression = first.item.advancement or second.item.advancement
                        (checked if progression else free).append((first, second))
                        break
        for first, second in free:
            swap(first, second)
        accepted = len(free) + (committed(checked) if checked else 0)
        log(f"Cross-player placement round {round_number}: {accepted} swap(s), "
            f"{sum(1 for l in locations if is_self(l))} self-placed item(s) left")
        if not accepted or time.monotonic() > deadline:
            break
    remaining = sum(1 for location in locations if is_self(location))
    return initial - remaining, remaining
