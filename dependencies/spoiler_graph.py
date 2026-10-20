"""Generates the multiworld and writes output/AP_<seed>.graph.json.

Run from inside the Archipelago folder with the launcher's venv Python.
For "all" and for each slot, the graph lists only the progression items that
are needed to reach the goal (pruned like Archipelago's spoiler playthrough),
grouped into spheres, with who finds each item and who receives it.

By default, items are afterwards moved so that as many checks as possible
unlock something for another player (see cross_player.py). Pass
--allow-self-checks to keep Archipelago's normal placement.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, os.getcwd())
sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))
# Progress lines for the launcher and the website (Generate is imported, so it doesn't configure logging).
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")

import Generate  # noqa: E402
import Main  # noqa: E402
from BaseClasses import CollectionState  # noqa: E402
from worlds import AutoWorld  # noqa: E402

import cross_player  # noqa: E402


def beatable(mw, targets, locations):
    state = CollectionState(mw)
    done = lambda: all(mw.completion_condition[p](state) for p in targets)
    if done():
        return True
    for _ in state.sweep_for_advancements(locations, yield_each_sweep=True):
        if done():
            return True
    return False


def prune(mw, targets, candidates):
    """Greedy removal in reverse sphere order: keep only items the targets need."""
    required = list(candidates)
    for loc in reversed(candidates):
        item = loc.item
        loc.item = None
        rest = [l for l in required if l is not loc]
        if beatable(mw, targets, rest):
            required = rest
        loc.item = item
    return required


def spheres_of(mw, required, targets):
    state = CollectionState(mw)
    remaining = set(required)
    spheres, goal = [], {}

    def mark(n):
        for p in targets:
            if p not in goal and mw.completion_condition[p](state):
                goal[p] = n

    mark(0)
    while remaining:
        sphere = [l for l in remaining if l.can_reach(state)]
        if not sphere:
            break
        for l in sphere:
            state.collect(l.item, True, l)
            remaining.discard(l)
        spheres.append(sphere)
        mark(len(spheres))
    return spheres, goal


def describe(mw, sphere):
    out = []
    for l in sorted(sphere, key=lambda l: (l.player, l.item.player, l.name)):
        out.append({
            "location": l.name,
            "location_id": l.address,
            "finder": l.player,
            "item": l.item.name,
            "receiver": l.item.player,
            "event": l.address is None,
        })
    return out


def install_cross_player_pass():
    """Runs the cross-player pass after fill/balancing, right before worlds finalize their output."""
    original = AutoWorld.call_all

    def call_all(multiworld, method_name, *args):
        if method_name == "finalize_multiworld" and multiworld.players > 1:
            started = time.time()
            players = set(multiworld.player_ids)
            accessible = multiworld.fulfills_accessibility()

            def valid():
                return multiworld.can_beat_game() and (not accessible or multiworld.fulfills_accessibility())

            moved, remaining = cross_player.prefer_other_players(
                multiworld, CollectionState(multiworld), valid, players, seed=multiworld.seed,
                log=lambda text: print(text, flush=True))
            print(f"Cross-player placement: moved {moved} self-placed item(s) to other players, "
                  f"{remaining} could not be moved ({time.time() - started:.1f}s)", flush=True)
        return original(multiworld, method_name, *args)

    AutoWorld.call_all = call_all


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-self-checks", action="store_true",
                        help="Keep normal placement instead of preferring items for other players")
    parser.add_argument("players_dir")
    parser.add_argument("output_dir")
    options = parser.parse_args()
    players_dir, output_dir = options.players_dir, options.output_dir
    if not options.allow_self_checks:
        install_cross_player_pass()
    for path in Path(players_dir).iterdir():
        documents = Generate.read_weights_yamls(str(path))
        if not any(isinstance(document, dict) and document for document in documents):
            raise ValueError(f"{path.name} contains no player configuration")
    sys.argv = [sys.argv[0], "--player_files_path", players_dir, "--outputpath", output_dir]
    erargs, seed = Generate.main()
    mw = Main.main(erargs, seed)

    t = time.time()
    all_players = list(mw.player_ids)
    ordered = [l for sphere in mw.get_spheres() for l in sorted(sphere, key=lambda l: l.name)
               if l.item and l.item.advancement]
    # Drop locations unreachable in the full sweep (get_spheres yields them last).
    reach = CollectionState(mw)
    reach.sweep_for_advancements()
    ordered = [l for l in ordered if l.can_reach(reach)]

    plans = {}
    needed_all = prune(mw, all_players, ordered)
    for key, targets, base in [("all", all_players, needed_all)] + \
            [(str(p), [p], needed_all) for p in all_players]:
        req = needed_all if key == "all" else prune(mw, targets, base)
        spheres, goal = spheres_of(mw, req, all_players)
        plans[key] = {
            "targets": targets,
            "spheres": [describe(mw, s) for s in spheres],
            "goal_sphere": {str(p): goal.get(p) for p in all_players},
        }
        print(f"plan {key}: {len(req)} required progression placements", flush=True)

    graph = {
        "seed": mw.seed_name,
        "players": [{"slot": p, "name": mw.player_name[p], "game": mw.game[p],
                     "locations": sum(1 for l in mw.get_locations(p) if l.address is not None)}
                    for p in all_players],
        "plans": plans,
    }
    path = os.path.join(output_dir, f"AP_{mw.seed_name}.graph.json")
    with open(path, "w") as f:
        json.dump(graph, f, indent=1)
    print(f"Wrote {path} in {time.time() - t:.1f}s")


if __name__ == "__main__":
    main()
