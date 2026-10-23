"""Portable launcher. Persistent paths are relative to this bundle, or to MULTIWORLD_ROOT when set."""

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
from urllib.error import URLError
import uuid

import sources

APP = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("MULTIWORLD_ROOT") or APP.parent).resolve()
SETTINGS_FILE = "room-settings.json"


def cache_dir() -> Path:
    return Path(os.environ.get("MULTIWORLD_CACHE") or ROOT / ".cache")


def player_files(root: Path) -> list[Path]:
    files = sorted(path for path in root.iterdir()
                   if path.is_file() and path.suffix.lower() in {".yaml", ".yml"})
    if not files:
        raise ValueError(f"No player YAMLs found. Add .yaml or .yml files directly inside {root}")
    return files


def check_ports(web_port: int, ap_port: int) -> None:
    if web_port == ap_port:
        raise ValueError("The website and game server need different ports.")
    for port in (web_port, ap_port):
        if not 1 <= port <= 65535:
            raise ValueError(f"Invalid port: {port}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            if client.connect_ex(("127.0.0.1", port)) == 0:
                raise ValueError(f"Port {port} is unavailable. Stop its server or choose another port.")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("0.0.0.0", port))
                probe.listen(1)
            except OSError as error:
                raise ValueError(f"Port {port} is unavailable. Stop its server or choose another port.") from error


def runtime(cache: Path) -> Path:
    """MultiworldGG source: MULTIWORLD_RUNTIME or a release cached per tag.
    MULTIWORLDGG_VERSION pins a tag; ARCHIPELAGO_VERSION remains a legacy alias."""
    if os.environ.get("MULTIWORLD_RUNTIME"):
        target = Path(os.environ["MULTIWORLD_RUNTIME"])
    else:
        requested = os.environ.get("MULTIWORLDGG_VERSION", os.environ.get("ARCHIPELAGO_VERSION", "latest"))
        try:
            tag = sources.latest_runtime_tag() if requested in ("", "latest") else sources.safe_tag(requested)
        except (OSError, URLError, ValueError, KeyError) as error:
            cached = sorted(cache.glob("runtime-multiworldgg-*/ARCHIPELAGO_VERSION"),
                            key=lambda path: path.stat().st_mtime)
            if not cached:
                raise RuntimeError(f"Could not look up the latest MultiworldGG release: {error}") from error
            target = cached[-1].parent
            print(f"Could not look up the latest MultiworldGG release ({error}); using cached "
                  f"{sources.runtime_version(target)}", file=sys.stderr, flush=True)
        else:
            target = cache / f"runtime-multiworldgg-{tag}"
            if not (target / "ARCHIPELAGO_VERSION").is_file():
                sources.download_runtime(tag, target)
    if not (target / "Generate.py").is_file() or not (target / "MultiServer.py").is_file():
        raise ValueError(f"Runtime is incomplete: {target}")
    return target


def worlds_dir() -> Path:
    """Custom games fetched from GitHub (MULTIWORLD_WORLDS in the Docker image)."""
    return Path(os.environ.get("MULTIWORLD_WORLDS") or cache_dir() / "worlds")


def update_worlds() -> None:
    """Fetches the custom games released in MULTIWORLD_WORLDS_ORG (default mahnkepelago). Keeps the
    previous download when GitHub is unreachable. Skipped when MULTIWORLD_WORLDS is preset."""
    if os.environ.get("MULTIWORLD_WORLDS"):
        return
    org = os.environ.get("MULTIWORLD_WORLDS_ORG", sources.WORLDS_ORG)
    if not org:
        return
    print(f"Custom games released in {org}:", flush=True)
    try:
        sources.fetch_worlds(org, worlds_dir())
    except (OSError, URLError, ValueError, KeyError) as error:
        print(f"Could not update custom games from {org} ({error}); using the previous download.",
              file=sys.stderr, flush=True)


def python_environment(cache: Path) -> Path:
    if os.environ.get("MULTIWORLD_SYSTEM_PYTHON") == "1":
        python = Path(sys.executable)
        subprocess.run([str(python), "-c", "import yaml, websockets, jinja2, jellyfish, schema, orjson"], check=True)
        return python
    requirements = [APP / "requirements.txt"]
    extra = APP / "requirements-extra.txt"
    if extra.exists():
        requirements.append(extra)
    identity = json.dumps([str(ROOT), sys.executable, sys.version,
                           [path.read_text() for path in requirements]])
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    environment = cache / f"venv-{digest}"
    python = environment / "bin/python"
    ready = environment / "ready"
    if not ready.is_file():
        if not python.exists():
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        command = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
        for path in requirements:
            command.extend(["-r", str(path)])
        subprocess.run(command, check=True)
        ready.touch()
    subprocess.run([str(python), "-c", "import yaml, websockets, jinja2, jellyfish, schema, orjson"],
                   check=True)
    return python


def sync_worlds(engine: Path) -> None:
    custom = engine / "custom_worlds"
    custom.mkdir(exist_ok=True)
    supplied = {path.name: path for path in sorted(worlds_dir().glob("*.apworld"))}
    for path in sorted(ROOT.glob("*.apworld")):
        if path.name in supplied:
            print(f"Using {path} instead of the released {path.name}", flush=True)
        supplied[path.name] = path
    for previous in custom.glob("*.apworld"):
        previous.unlink()
    for name, source in supplied.items():
        shutil.copy2(source, custom / name)


def latest_campaign() -> Path | None:
    """The seed recorded in latest.json, if its files still exist."""
    try:
        campaign = ROOT / json.loads((ROOT / "latest.json").read_text())["campaign"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if campaign.is_file() and campaign.with_suffix(".graph.json").is_file():
        return campaign
    return None


LEFT_PLAYER_INTERVAL = (1, 3600)  # seconds between automatic unlocks for a player who has left


def valid_left_interval(value) -> bool:
    return type(value) is int and LEFT_PLAYER_INTERVAL[0] <= value <= LEFT_PLAYER_INTERVAL[1]


def room_settings() -> dict:
    """Room-wide settings edited from the website (room-settings.json beside start.sh)."""
    settings = {"prefer_other_players": True, "left_player_interval": 60}
    path = ROOT / SETTINGS_FILE
    if path.is_file():
        try:
            stored = json.loads(path.read_text())
        except ValueError as error:
            raise ValueError(f"{path} is not valid JSON: {error}") from error
        if isinstance(stored, dict) and isinstance(stored.get("prefer_other_players"), bool):
            settings["prefer_other_players"] = stored["prefer_other_players"]
        if isinstance(stored, dict) and valid_left_interval(stored.get("left_player_interval")):
            settings["left_player_interval"] = stored["left_player_interval"]
    return settings


def save_room_settings(settings: dict) -> None:
    """Updates the given settings, keeping the others."""
    try:
        current = room_settings()
    except ValueError:
        current = {}
    temporary = ROOT / f".{SETTINGS_FILE}.tmp"
    temporary.write_text(json.dumps({**current, **settings}, indent=2) + "\n")
    temporary.replace(ROOT / SETTINGS_FILE)


def generate(python: Path, engine: Path, files: list[Path], environment: dict[str, str], **options) -> Path:
    return generate_inputs(python, engine, [(path.name, path.read_bytes()) for path in files], environment, **options)


def generate_inputs(python: Path, engine: Path, inputs: list[tuple[str, bytes]], environment: dict[str, str],
                    on_line=None, on_process=None, prefer_other_players: bool | None = None) -> Path:
    """Generates a new seed from (file name, YAML bytes) pairs. Output lines are streamed to `on_line`."""
    if not inputs:
        raise ValueError("At least one player is required.")
    if prefer_other_players is None:
        prefer_other_players = room_settings()["prefer_other_players"]
    runs = ROOT / "runs"
    runs.mkdir(exist_ok=True)
    run = runs / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    players = run / "players"
    players.mkdir(parents=True)
    output = run / "output"
    output.mkdir()
    for index, (source, content) in enumerate(inputs):
        # Archipelago ignores dotfiles; include explicitly supplied hidden YAMLs too.
        name = f"{index}-{source}" if source.startswith(".") else source
        (players / name).write_bytes(content)
    print(f"Generating a NEW seed from {len(inputs)} YAML file(s):", flush=True)
    for source, _content in inputs:
        print(f"  {source}", flush=True)
    if prefer_other_players:
        print("Preferring checks that unlock items for other players.", flush=True)
    command = [str(python), str(APP / "spoiler_graph.py")]
    if not prefer_other_players:
        command.append("--allow-self-checks")
    command += [str(players), str(output)]
    log_path = run / "generation.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(command, cwd=engine, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", bufsize=1)
        if on_process:
            on_process(process)
        for line in process.stdout:
            log.write(line)
            if on_line:
                on_line(line.rstrip("\n"))
        returncode = process.wait()
    if returncode:
        if not on_line:
            print(log_path.read_text()[-12000:], file=sys.stderr)
        raise RuntimeError(f"Generation failed. Full log: {log_path}")
    archives = list(output.glob("AP_*.zip"))
    if len(archives) != 1 or not archives[0].with_suffix(".graph.json").is_file():
        raise RuntimeError(f"Expected one new seed ZIP and tracker graph. Inspect {log_path}")
    campaign = archives[0]
    if campaign.with_suffix(".apsave").exists():
        raise RuntimeError("Refusing a seed that already has saved progress.")
    (ROOT / "latest.json").write_text(json.dumps({
        "campaign": str(campaign.relative_to(ROOT)),
        "players": [source for source, _content in inputs],
    }, indent=2) + "\n")
    return campaign


def serve(python: Path, engine: Path, campaign: Path | None, args: argparse.Namespace,
          environment: dict[str, str]) -> None:
    command = [str(python), "-u", str(APP / "room.py"), "--runtime", str(engine),
               "--web-port", str(args.web_port), "--ap-port", str(args.ap_port)]
    if campaign is not None:
        command += ["--campaign", str(campaign)]
    process = subprocess.Popen(command, env=environment, start_new_session=True)

    def stop(signum, _frame):
        if process.poll() is None:
            process.send_signal(signum)

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        code = process.wait()
        if code:
            raise RuntimeError(f"Room exited with status {code}")
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a fresh seed from all adjacent YAMLs and host it.")
    parser.add_argument("--web-port", type=int, default=8085)
    parser.add_argument("--ap-port", type=int, default=38281)
    parser.add_argument("--generate-only", action="store_true", help="Generate and validate a new seed without hosting")
    parser.add_argument("--resume", action="store_true",
                        help="Host the seed from latest.json with its saved progress instead of generating a new one")
    parser.add_argument("--allow-empty", action="store_true",
                        help="Without player YAMLs, start an empty room where admins add players on the website")
    args = parser.parse_args()
    campaign = latest_campaign() if args.resume and not args.generate_only else None
    files = []
    if campaign is None:
        try:
            files = player_files(ROOT)
        except ValueError:
            if not args.allow_empty or args.generate_only:
                raise
    cache = cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "launcher.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("This folder is already generating or hosting a room. Stop it with Ctrl+C first.") from error
        if not args.generate_only:
            check_ports(args.web_port, args.ap_port)
        engine = runtime(cache)
        print(f"MultiworldGG {sources.runtime_version(engine) or 'unknown version'}", flush=True)
        python = python_environment(cache)
        update_worlds()
        sync_worlds(engine)
        environment = {**os.environ, "SKIP_REQUIREMENTS_UPDATE": "1", "MULTIWORLD_ROOT": str(ROOT)}
        if campaign is not None:
            print(f"Resuming seed: {campaign.name}", flush=True)
        elif files:
            campaign = generate(python, engine, files, environment)
            print(f"Fresh seed: {campaign.name}\nSaved in: {campaign.parent.parent}", flush=True)
        else:
            print("No player YAMLs yet. Admins add players with Edit on the website.", flush=True)
        if not args.generate_only:
            print("Stop with Ctrl+C." + ("" if args.resume else " Starting again creates another NEW seed."), flush=True)
            serve(python, engine, campaign, args, environment)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print(f"MultiWorldGG: {error}", file=sys.stderr)
        sys.exit(1)
