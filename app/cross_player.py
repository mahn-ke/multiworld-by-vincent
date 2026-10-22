"""Spreads rewards across other players' worlds after the main fill.

Swaps keep every item rule (local/non-local items, excluded locations) and only
keep progression swaps that leave the multiworld beatable with unchanged
accessibility. Spreading rewards takes precedence over removing every possible
self-item; players of the same game are treated like any other players.
"""
from collections import Counter
from itertools import combinations, islice, product
from math import prod
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


def spread_rewards(locations, state, valid, rng, deadline, log):
    counts = Counter((location.player, location.item.player) for location in locations)

    def improvement(before_pairs, after_pairs):
        changes = Counter()
        for pair in before_pairs:
            changes[pair] -= 1
        for pair in after_pairs:
            changes[pair] += 1
        remote = [pair for pair, delta in changes.items() if delta and pair[0] != pair[1]]
        # Maximize sum(log(1 + pair count)): rewards for an underserved pair
        # matter more than further concentrating rewards in an existing pair.
        before = prod(counts[pair] + 1 for pair in remote)
        after = prod(counts[pair] + changes[pair] + 1 for pair in remote)
        fewer_self = sum(delta for (finder, receiver), delta in changes.items() if finder == receiver) < 0
        return changes if after > before or (after == before and fewer_self) else None

    def rotate_rewards():
        groups = {}
        for location in locations:
            groups.setdefault((location.player, location.item.player), []).append(location)
        # A three-player cycle can improve the spread when every two-way swap
        # would temporarily create a self-item and reduce the score.
        for keys in combinations(groups, 3):
            if time.monotonic() > deadline:
                return False
            if len({p for p, _ in keys}) != 3 or len({p for _, p in keys}) != 3:
                continue
            for a, b, c in (keys, (keys[0], keys[2], keys[1])):
                if time.monotonic() > deadline:
                    return False
                changes = improvement((a, b, c), ((a[0], c[1]), (b[0], a[1]), (c[0], b[1])))
                if changes is None:
                    continue
                for first, second, third in islice(product(groups[a], groups[b], groups[c]), PARTNER_ATTEMPTS):
                    if time.monotonic() > deadline:
                        return False
                    if not (accepts(first, third.item, state) and accepts(second, first.item, state)
                            and accepts(third, second.item, state)):
                        continue
                    progression = any(location.item.advancement for location in (first, second, third))
                    swap(first, second)
                    swap(first, third)
                    if progression and not valid():
                        swap(first, third)
                        swap(first, second)
                        continue
                    counts.update(changes)
                    return True
        return False

    round_number = 0
    while time.monotonic() < deadline:
        round_number += 1
        rng.shuffle(locations)
        accepted = 0
        for first in locations:
            for second in locations[:PARTNER_ATTEMPTS]:
                if time.monotonic() > deadline:
                    break
                a, b = first.player, second.player
                x, y = first.item.player, second.item.player
                if a == b or x == y:
                    continue
                changes = improvement(((a, x), (b, y)), ((a, y), (b, x)))
                if changes is None or not (accepts(first, second.item, state)
                                           and accepts(second, first.item, state)):
                    continue
                progression = first.item.advancement or second.item.advancement
                swap(first, second)
                if progression and not valid():
                    swap(first, second)
                    continue
                counts.update(changes)
                accepted += 1
                break
            if time.monotonic() > deadline:
                break
        log(f"Cross-player placement spread round {round_number}: {accepted} swap(s), "
            f"{sum(1 for location in locations if is_self(location))} self-placed item(s)")
        if not accepted and not rotate_rewards():
            break


def prefer_other_players(multiworld, state, valid, players, seed=0, budget=120.0, log=print):
    """Returns (net reduction, remaining) counts of self-placed items.

    `valid()` must return whether the current placement is still acceptable.
    The net reduction can be negative when spreading rewards needs more self-items.
    """
    rng = random.Random(seed)
    deadline = time.monotonic() + budget
    locations = [location for location in multiworld.get_filled_locations()
                 if location.player in players and movable(location)]
    initial = sum(1 for location in locations if is_self(location))
    if len(players) < 2:
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
    spread_rewards(locations, state, valid, rng, deadline, log)
    remaining = sum(1 for location in locations if is_self(location))
    return initial - remaining, remaining
