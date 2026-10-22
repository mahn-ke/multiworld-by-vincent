"""Portable fresh-seed launcher. All persistent paths are relative to this bundle."""

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
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent
DEPS = ROOT / "dependencies"
SETTINGS_FILE = "room-settings.json"


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
    archive = DEPS / "archipelago-0.6.8.tar.gz"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()[:16]
    target = cache / f"runtime-{digest}"
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix="extract-", dir=cache) as temporary:
            staging = Path(temporary)
            with tarfile.open(archive) as source:
                source.extractall(staging, filter="data")
            for name in ("Generate.py", "MultiServer.py", "worlds", "LICENSE"):
                if not (staging / name).exists():
                    raise ValueError(f"Bundled runtime is missing {name}")
            staging.rename(target)
    if not (target / "Generate.py").is_file() or not (target / "MultiServer.py").is_file():
        raise ValueError(f"Runtime is incomplete: {target}")
    return target


def python_environment(cache: Path) -> Path:
    requirements = [DEPS / "requirements.txt"]
    extra = DEPS / "requirements-extra.txt"
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
    supplied = sorted((DEPS / "worlds").glob("*.apworld"))
    supplied.extend(sorted(ROOT.glob("*.apworld")))
    names = [path.name for path in supplied]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate .apworld filename in root and dependencies/worlds; keep only one copy.")
    for previous in custom.glob("*.apworld"):
        previous.unlink()
    for source in supplied:
        shutil.copy2(source, custom / source.name)


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
    command = [str(python), str(DEPS / "spoiler_graph.py")]
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


def serve(python: Path, engine: Path, campaign: Path, args: argparse.Namespace,
          environment: dict[str, str]) -> None:
    command = [str(python), "-u", str(DEPS / "room.py"), "--runtime", str(engine),
               "--campaign", str(campaign), "--web-port", str(args.web_port),
               "--ap-port", str(args.ap_port)]
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
    args = parser.parse_args()
    files = player_files(ROOT)
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    with (cache / "launcher.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("This folder is already generating or hosting a room. Stop it with Ctrl+C first.") from error
        if not args.generate_only:
            check_ports(args.web_port, args.ap_port)
        engine = runtime(cache)
        python = python_environment(cache)
        sync_worlds(engine)
        environment = {**os.environ, "SKIP_REQUIREMENTS_UPDATE": "1"}
        campaign = generate(python, engine, files, environment)
        print(f"Fresh seed: {campaign.name}\nSaved in: {campaign.parent.parent}", flush=True)
        if not args.generate_only:
            print("Stop with Ctrl+C. Starting again creates another NEW seed.", flush=True)
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
