"""Writes every installed game's player options as JSON for the website editor.

Run from inside the Archipelago folder with the launcher's venv Python:
    python options_schema.py <output.json>
"""
import inspect
import json
import logging
import os
import sys

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, os.getcwd())
logging.disable(logging.CRITICAL)

import Options  # noqa: E402
from worlds import AutoWorldRegister, ensure_worlds_loaded  # noqa: E402

SUGGESTION_LIMIT = 5000


def plain(value):
    if isinstance(value, (set, frozenset, tuple, list)):
        return sorted(plain(v) for v in value) if isinstance(value, (set, frozenset)) else [plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def describe(name, option, group, world):
    entry = {
        "name": name,
        "display": getattr(option, "display_name", None) or name.replace("_", " ").title(),
        "doc": inspect.cleandoc(option.__doc__ or ""),
        "group": group,
        "simple": bool(Options.Visibility.simple_ui & option.visibility),
    }
    default = getattr(option, "default", None)
    if issubclass(option, Options.Toggle):
        entry.update(kind="toggle", default=bool(default))
    elif issubclass(option, Options.Choice):
        keys = dict(option.name_lookup)
        choices = [{"key": key, "label": option.get_option_name(value)} for value, key in keys.items()]
        entry.update(kind="textchoice" if issubclass(option, Options.TextChoice) else "choice",
                     choices=choices,
                     aliases=sorted(getattr(option, "aliases", {})),
                     default=keys.get(default, default) if isinstance(default, int) else plain(default))
    elif issubclass(option, Options.Range):
        entry.update(kind="range", min=option.range_start, max=option.range_end, default=plain(default),
                     special=plain(getattr(option, "special_range_names", {})))
    elif issubclass(option, Options.FreeText):
        entry.update(kind="text", default=plain(default))
    else:
        kind = ("set" if issubclass(option, Options.OptionSet)
                else "list" if issubclass(option, Options.OptionList)
                else "dict" if issubclass(option, Options.OptionDict) else "raw")
        suggestions = list(getattr(option, "valid_keys", ()) or ())
        if not suggestions and getattr(option, "verify_item_name", False):
            suggestions = list(world.item_names)
        elif not suggestions and getattr(option, "verify_location_name", False):
            suggestions = list(world.location_names)
        entry.update(kind=kind, default=plain(default),
                     suggestions=sorted(map(str, suggestions))[:SUGGESTION_LIMIT])
    return entry


def main():
    ensure_worlds_loaded()
    games = {}
    for game, world in sorted(AutoWorldRegister.world_types.items()):
        if world.hidden:
            continue
        options = []
        try:
            for group, members in Options.get_option_groups(world, Options.Visibility.complex_ui).items():
                for name, option in members.items():
                    options.append(describe(name, option, group, world))
        except Exception as error:  # A broken world should not hide all other games.
            print(f"Skipping {game}: {error}", file=sys.stderr)
            continue
        games[game] = {"options": options}
    with open(sys.argv[1], "w") as output:
        json.dump({"games": games}, output)


if __name__ == "__main__":
    main()
