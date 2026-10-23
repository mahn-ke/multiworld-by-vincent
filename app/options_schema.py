"""Writes every installed game's player options as JSON for the website editor.

Run from inside the Archipelago folder with the launcher's venv Python:
    python options_schema.py <output.json>

Progress goes to stdout as lines of `PROGRESS {"stage", "done", "total", "label"}`.
"""
import importlib
import inspect
import json
import logging
import os
import sys

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, os.getcwd())
logging.disable(logging.CRITICAL)


def progress(stage, done=0, total=0, label=""):
    print("PROGRESS " + json.dumps({"stage": stage, "done": min(done, total), "total": total, "label": label}),
          flush=True)


def world_sources():
    """Names Archipelago will import as `worlds.<name>`, mirroring how worlds/__init__.py finds them."""
    names = set()
    for folder in ("worlds", "custom_worlds"):
        if not os.path.isdir(folder):
            continue
        for entry in os.scandir(folder):
            if entry.name.startswith(("_", ".")):
                continue
            if entry.is_dir() and folder == "worlds" and any(
                    os.path.isfile(os.path.join(entry.path, init)) for init in ("__init__.py", "__init__.pyc")):
                names.add(entry.name)
            elif entry.is_file() and entry.name.endswith(".apworld"):
                names.add(entry.name[:-len(".apworld")])
    return names


class WorldImportObserver:
    """Reports each world's first import; finds/imports nothing itself, so importing is unchanged."""

    def __init__(self, names):
        self.names, self.seen = names, set()

    def observe(self, fullname):
        prefix, _, name = fullname.partition(".")
        if prefix == "worlds" and name in self.names and name not in self.seen:
            self.seen.add(name)
            progress("worlds", len(self.seen), len(self.names), name)

    def find_spec(self, fullname, path=None, target=None):
        self.observe(fullname)
        return None

    def import_module(self, original):
        # Archipelago's apworld finder puts itself first on sys.meta_path, so watch import_module as well.
        def import_module(name, package=None):
            self.observe(f"{package}{name}" if name.startswith(".") and package else name)
            return original(name, package)
        return import_module


progress("starting")
import Options  # noqa: E402

observer = WorldImportObserver(world_sources())
progress("worlds", 0, len(observer.names))
original_import_module = importlib.import_module
importlib.import_module = observer.import_module(original_import_module)
sys.meta_path.insert(0, observer)
try:
    from worlds import AutoWorldRegister, ensure_worlds_loaded  # noqa: E402
finally:
    sys.meta_path.remove(observer)
    importlib.import_module = original_import_module

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
    importlib.import_module = observer.import_module(original_import_module)
    sys.meta_path.insert(0, observer)
    try:
        ensure_worlds_loaded()
    finally:
        sys.meta_path.remove(observer)
        importlib.import_module = original_import_module
    games = {}
    visible = [(game, world) for game, world in sorted(AutoWorldRegister.world_types.items()) if not world.hidden]
    for done, (game, world) in enumerate(visible, 1):
        progress("options", done, len(visible), game)
        options = []
        try:
            for group, members in Options.get_option_groups(world, Options.Visibility.complex_ui).items():
                for name, option in members.items():
                    options.append(describe(name, option, group, world))
        except Exception as error:  # A broken world should not hide all other games.
            print(f"Skipping {game}: {error}", file=sys.stderr)
            continue
        games[game] = {"options": options}
    progress("saving", len(visible), len(visible))
    with open(sys.argv[1], "w") as output:
        json.dump({"games": games}, output)


if __name__ == "__main__":
    main()
