"""Serve a seed with a tracker, host-only console and a website editor that can regenerate the room."""
import argparse
import asyncio
import base64
import collections
import copy
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

import websockets

import oidc
import sources

DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = Path(os.environ.get("MULTIWORLD_ROOT") or Path(DIR).parent).resolve()
# Inside a container, proxied requests may arrive from a loopback address; set 0 so only signed-in admins are trusted.
TRUST_LOCALHOST = os.environ.get("MULTIWORLD_TRUST_LOCALHOST", "1") != "0"
EMPTY_GRAPH = {"seed": None, "players": [], "plans": {"all": {"goal_sphere": {}, "targets": [], "spheres": []}}}
AP = None
STATUS_NAMES = {0: "not connected", 5: "connected", 10: "ready", 20: "playing", 30: "goal reached"}
MAX_NAME_LENGTH = 16  # Archipelago's slot name limit

def load_checks(zip_path):
    if AP not in sys.path:
        sys.path.insert(0, AP)
    from MultiServer import Context
    with zipfile.ZipFile(zip_path) as archive:
        name = next(name for name in archive.namelist() if name.endswith(".archipelago"))
        data = Context.decompress(archive.read(name))
    packages = data["datapackage"]
    items = {game: {value: name for name, value in package["item_name_to_id"].items()}
             for game, package in packages.items()}
    locations = {game: {value: name for name, value in package["location_name_to_id"].items()}
                 for game, package in packages.items()}
    return {slot: [{"id": location, "name": locations[data["slot_info"][slot].game][location],
                    "item": items[data["slot_info"][receiver].game][item],
                    "receiver": data["slot_info"][receiver].name}
                   for location, (item, receiver, flags) in sorted(checks.items())]
            for slot, checks in data["locations"].items()}


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Server:
    """MultiServer subprocess with its console attached to a pipe."""

    def __init__(self, zip_path, port):
        self.zip_path, self.port = zip_path, port
        self.log = collections.deque(maxlen=400)
        self.lock = threading.Lock()
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "MultiServer.py", self.zip_path,
             "--host", "0.0.0.0", "--port", str(self.port)],
            cwd=AP, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env={**os.environ, "SKIP_REQUIREMENTS_UPDATE": "1"})
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.log.append(line.rstrip("\n"))

    def command(self, text):
        with self.lock:
            self.log.append(f"> {text}")
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        """Gracefully exits MultiServer so it writes its save, escalating if it hangs."""
        if not self.running:
            return
        try:
            self.command("/exit")
        except (OSError, ValueError) as error:
            print(f"Could not request graceful shutdown: {error}", file=sys.stderr)
            self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


class Tracker:
    """One Tracker-tagged connection per slot: checked locations, received items, client status, feed."""

    def __init__(self, url, players):
        self.url, self.players = url, players
        self.state = {p["slot"]: {"checked": set(), "received": 0, "status": 0, "online": False, "games": 0}
                      for p in players}
        self.feed = collections.deque(maxlen=150)
        self.names = {"item": {}, "location": {}}  # game -> id -> name
        self.games = {p["slot"]: p["game"] for p in players}
        self.loop = None
        self.stopped = False

    def run(self):
        loop = self.loop = asyncio.new_event_loop()
        tasks = [loop.create_task(self._track(p, feed=p is self.players[0])) for p in self.players]
        try:
            if not self.stopped:
                loop.run_forever()
        finally:
            for task in tasks:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.close()

    def stop(self):
        self.stopped = True
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass

    async def _track(self, player, feed):
        slot = player["slot"]
        st = self.state[slot]
        while True:
            try:
                async with websockets.connect(self.url, max_size=None, ping_interval=20) as ws:
                    await ws.recv()
                    await ws.send(json.dumps([{
                        "cmd": "Connect", "game": "", "name": player["name"], "password": None,
                        "uuid": str(uuid.uuid4()), "items_handling": 7, "tags": ["Tracker"],
                        "slot_data": False, "version": client_version()}]))
                    key = f"_read_client_status_0_{slot}"
                    async for raw in ws:
                        for msg in json.loads(raw):
                            cmd = msg.get("cmd")
                            if cmd == "Connected":
                                st["online"] = True
                                st["checked"] = set(msg.get("checked_locations", []))
                                requests = [{"cmd": "SetNotify", "keys": [key]}, {"cmd": "Get", "keys": [key]}]
                                if feed:
                                    requests.append({"cmd": "GetDataPackage", "games": sorted(set(self.games.values()))})
                                await ws.send(json.dumps(requests))
                            elif cmd == "ConnectionRefused":
                                raise ConnectionError(msg)
                            elif cmd == "RoomUpdate":
                                st["checked"].update(msg.get("checked_locations", []))
                            elif cmd == "ReceivedItems":
                                if msg["index"] == 0:
                                    st["received"] = len(msg["items"])
                                else:
                                    st["received"] += len(msg["items"])
                            elif cmd in ("Retrieved", "SetReply"):
                                if cmd == "SetReply" and msg.get("key") == key:
                                    st["status"] = msg.get("value") or 0
                                elif cmd == "Retrieved" and key in msg.get("keys", {}):
                                    st["status"] = msg["keys"][key] or 0
                            elif cmd == "DataPackage":
                                for game, data in msg["data"]["games"].items():
                                    self.names["item"][game] = {v: k for k, v in data["item_name_to_id"].items()}
                                    self.names["location"][game] = {v: k for k, v in data["location_name_to_id"].items()}
                            elif cmd == "PrintJSON" and feed:
                                if self._presence(msg):
                                    continue
                                self.feed.append({"time": time.time(), "parts": self._parts(msg)})
            except (OSError, websockets.WebSocketException, ConnectionError, ValueError) as error:
                if st["online"] or not st.get("error"):
                    print(f"Tracker for {player['name']} disconnected: {error}", file=sys.stderr, flush=True)
                st["error"] = str(error)
                st["online"] = False
                await asyncio.sleep(3)

    def _presence(self, msg):
        """Counts real game clients per slot; returns True for messages hidden from the feed."""
        kind, slot = msg.get("type"), msg.get("slot")
        text = "".join(p.get("text", "") for p in msg.get("data", []))
        if kind == "Tutorial":
            return True
        if kind == "Join" and slot in self.state:
            if set(msg.get("tags", [])) & {"Tracker", "TextOnly", "HintGame"}:
                return True
            self.state[slot]["games"] += 1
        elif kind == "Part" and slot in self.state:
            if "has left the game" not in text:
                return True
            self.state[slot]["games"] = max(0, self.state[slot]["games"] - 1)
        return False

    def _parts(self, msg):
        parts = []
        for part in msg.get("data", []):
            kind, text = part.get("type", "text"), part.get("text", "")
            if kind == "player_id":
                slot = int(text)
                text = next((p["name"] for p in self.players if p["slot"] == slot), text)
                parts.append({"t": text, "k": "player", "slot": slot})
            elif kind == "item_id":
                game = self.games.get(part.get("player"), "")
                text = self.names["item"].get(game, {}).get(int(text), text)
                flags = part.get("flags", 0)
                parts.append({"t": text, "k": "prog" if flags & 1 else "useful" if flags & 2 else "item"})
            elif kind == "location_id":
                game = self.games.get(part.get("player"), "")
                parts.append({"t": self.names["location"].get(game, {}).get(int(text), text), "k": "location"})
            else:
                parts.append({"t": text, "k": "text"})
        return parts


_launch = None


def client_version():
    """The tracker announces the runtime's Archipelago version."""
    version = (sources.runtime_version(AP) if AP else None) or "0.6.8"
    major, minor, build = ([int(part) for part in re.findall(r"\d+", version)][:3] + [0, 0, 0])[:3]
    return {"major": major, "minor": minor, "build": build, "class": "Version"}


def launcher():
    """The launcher module (generation, world sync, room settings), loaded from this folder."""
    global _launch
    if _launch is None:
        spec = importlib.util.spec_from_file_location("multiworld_launch", os.path.join(DIR, "launch.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _launch = module
    return _launch


# ---------------------------------------------------------------- WebSocket (RFC 6455, server side, text only)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_frame(payload, opcode=0x1):
    if isinstance(payload, str):
        payload = payload.encode()
    size = len(payload)
    if size < 126:
        header = bytes([0x80 | opcode, size])
    elif size < 1 << 16:
        header = bytes([0x80 | opcode, 126]) + size.to_bytes(2, "big")
    else:
        header = bytes([0x80 | opcode, 127]) + size.to_bytes(8, "big")
    return header + payload


def ws_read(stream):
    """Returns (opcode, payload) of one client frame, or None when the connection ended."""
    head = stream.read(2)
    if len(head) < 2:
        return None
    opcode, size = head[0] & 0x0F, head[1] & 0x7F
    if size == 126:
        size = int.from_bytes(stream.read(2), "big")
    elif size == 127:
        size = int.from_bytes(stream.read(8), "big")
    if size > 1 << 20:
        return None
    mask = stream.read(4) if head[1] & 0x80 else b""
    payload = stream.read(size)
    if len(payload) < size:
        return None
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


class SocketClient:
    def __init__(self, connection, admin):
        self.connection, self.admin = connection, admin
        self.lock = threading.Lock()

    def send(self, text, opcode=0x1):
        with self.lock:
            self.connection.sendall(ws_frame(text, opcode))


# ---------------------------------------------------------------- Player YAML editing

def read_players(root):
    """Describes every player YAML beside start.sh for the editor."""
    import yaml
    try:
        files = launcher().player_files(root)
    except ValueError:
        return []
    players = []
    for path in files:
        entry = {"file": path.name, "editable": False, "reason": None, "name": "", "game": "", "options": {}}
        players.append(entry)
        try:
            documents = [document for document in yaml.safe_load_all(path.read_text()) if document is not None]
        except (yaml.YAMLError, UnicodeDecodeError) as error:
            entry["reason"] = f"Invalid YAML: {error}"
            continue
        if len(documents) != 1 or not isinstance(documents[0], dict):
            entry["reason"] = "Contains several players or a non-standard layout; edit the file directly."
            continue
        document = documents[0]
        name, game = document.get("name", ""), document.get("game")
        if isinstance(name, str):
            entry["name"] = name
        if isinstance(game, str):
            entry["game"] = game
        section = document.get(game, {}) if isinstance(game, str) else None
        if not isinstance(name, str) or not isinstance(game, str) or not isinstance(section or {}, dict):
            entry["reason"] = "Uses weighted or random names/games; edit the file directly."
            continue
        entry.update(editable=True, options=json.loads(json.dumps(section or {}, default=str)))
    return players


def file_name_for(name, taken):
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "Player"
    candidate, number = f"{stem}.yaml", 2
    while candidate.lower() in taken:
        candidate, number = f"{stem}-{number}.yaml", number + 1
    return candidate


def plan_config(root, body, games=None):
    """Validates an editor submission. Returns (generation inputs, commit function, room settings)."""
    import yaml
    if not isinstance(body.get("players"), list) or not isinstance(body.get("prefer_other_players", True), bool):
        raise ValueError("Expected a list of players")
    try:
        current = {path.name: path for path in launcher().player_files(root)}
    except ValueError:
        current = {}
    taken = {name.lower() for name in current}
    inputs, writes, removals, seen, names = {}, {}, [], set(), set()
    for player in body["players"]:
        if not isinstance(player, dict):
            raise ValueError("Invalid player entry")
        file = player.get("file")
        if file is not None and (not isinstance(file, str) or file not in current or file in seen):
            raise ValueError(f"Unknown or duplicate player file: {file}")
        if file is not None:
            seen.add(file)
            original_bytes = current[file].read_bytes()
        if player.get("remove"):
            if file is None:
                continue
            removals.append(file)
            continue
        if file is not None and player.get("unchanged"):
            inputs[file] = original_bytes
            continue
        name, game = player.get("name"), player.get("game")
        changes, resets = player.get("set", {}), player.get("unset", [])
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Every player needs a name")
        name = name.strip()
        if len(name) > MAX_NAME_LENGTH:
            raise ValueError(f"Player name {name!r} is longer than {MAX_NAME_LENGTH} characters")
        if name.lower() in names:
            raise ValueError(f"Player name {name!r} is used twice")
        names.add(name.lower())
        if not isinstance(game, str) or not game or (games is not None and game not in games):
            raise ValueError(f"Unknown game for {name}: {game!r}")
        if not isinstance(changes, dict) or not isinstance(resets, list) or not all(
                isinstance(key, str) for key in [*changes, *resets]):
            raise ValueError(f"Invalid settings for {name}")
        if file is None:
            document, original = {"name": name, "game": game, "description": "Created in the website editor."}, None
            file = file_name_for(name, taken)
            taken.add(file.lower())
        else:
            documents = [d for d in yaml.safe_load_all(original_bytes) if d is not None]
            if len(documents) != 1 or not isinstance(documents[0], dict):
                raise ValueError(f"{file} cannot be edited on the website; edit the file directly")
            original = documents[0]
            document = copy.deepcopy(original)
        previous_game = document.get("game")
        if previous_game != game:
            if isinstance(previous_game, str):
                document.pop(previous_game, None)  # Settings of the old game do not apply to the new one.
            document["game"] = game
        document["name"] = name
        section = document.get(game) or {}
        if not isinstance(section, dict):
            raise ValueError(f"{file} has a non-standard {game} section; edit the file directly")
        for key in resets:
            section.pop(key, None)
        section.update(changes)
        document[game] = section
        if document == original:
            inputs[file] = original_bytes
        else:
            inputs[file] = writes[file] = yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode()
    for file, path in current.items():
        if file not in seen:
            inputs[file] = path.read_bytes()
    if not inputs:
        raise ValueError("At least one player is required")
    settings = {"prefer_other_players": body.get("prefer_other_players", True)}

    def commit():
        for file, content in writes.items():
            temporary = root / f".{file}.tmp"
            temporary.write_bytes(content)
            temporary.replace(root / file)
        if removals:
            inactive = root / "inactive-players"
            inactive.mkdir(exist_ok=True)
            for file in removals:
                target, number = inactive / file, 2
                while target.exists():
                    target, number = inactive / f"{Path(file).stem}-{number}{Path(file).suffix}", number + 1
                (root / file).replace(target)
        launcher().save_room_settings(settings)

    return sorted(inputs.items(), key=lambda item: item[0]), commit, settings


# ---------------------------------------------------------------- Room lifecycle

# Generation output line -> (progress, label). Progress only moves forward.
PHASES = [
    ("Generating a NEW seed", 0.04, "Reading player files"),
    ("Archipelago Version", 0.08, "Loading games"),
    ("Creating MultiWorld", 0.2, "Creating worlds"),
    ("Creating Items", 0.28, "Creating items"),
    ("Calculating Access Rules", 0.34, "Calculating access rules"),
    ("Running Item Plando", 0.4, "Placing fixed items"),
    ("Filling the multiworld", 0.45, "Randomizing items"),
    ("Balancing multiworld progression", 0.55, "Balancing progression"),
    ("Cross-player placement", 0.62, "Moving items to other players"),
    ("Beginning output", 0.7, "Writing seed files"),
    ("Creating final archive", 0.8, "Packing the seed"),
    ("Done. Enjoy", 0.84, "Seed created"),
    ("plan ", 0.88, "Building the path-to-goal graph"),
    ("Wrote ", 0.94, "Seed ready"),
]


class Room:
    """Owns the running seed (game server + tracker) and replaces it with freshly generated ones."""

    def __init__(self, ctx, engine, port, root=ROOT):
        self.ctx, self.engine, self.port, self.root = ctx, engine, port, Path(root)
        ctx["room"] = self
        self.lock = threading.Lock()
        self.clients = set()
        self.state = {"phase": "starting", "message": "Starting the game server", "progress": 0.0,
                      "error": None, "busy": True}
        self.generation_log = collections.deque(maxlen=500)
        self.process = None
        self.closing = False
        self.schema_lock = threading.Lock()
        self.schema = (None, None)
        # Players who have left: slot -> {"since": time of the last grant or of checking the box,
        # "sent": location -> time it was sent and not yet confirmed}. Kept for the current seed only.
        self.left, self.left_seed = {}, None
        try:
            self.left_interval = launcher().room_settings()["left_player_interval"]
        except ValueError:
            self.left_interval = 60

    # -- clients
    def snapshot(self):
        with self.lock:
            return {"type": "state", **self.state, "seed": self.ctx.get("graph", {}).get("seed")}

    def attach(self, client):
        with self.lock:
            self.clients.add(client)
        client.send(json.dumps(self.snapshot()))
        if client.admin:
            client.send(json.dumps({"type": "log", "lines": list(self.generation_log)}))

    def detach(self, client):
        with self.lock:
            self.clients.discard(client)

    def broadcast(self, message, admin_only=False):
        text = json.dumps(message)
        with self.lock:
            clients = [client for client in self.clients if client.admin or not admin_only]
        for client in clients:
            try:
                client.send(text)
            except OSError:
                self.detach(client)

    def update(self, **changes):
        with self.lock:
            self.state.update(changes)
        self.broadcast(self.snapshot())

    # -- options schema for the editor
    def options_schema(self):
        custom = Path(self.engine) / "custom_worlds"
        worlds = sorted(
            (path.name, path.stat().st_size, path.stat().st_mtime) for path in custom.glob("*.apworld")
        ) if custom.is_dir() else []
        signature = hashlib.sha256(json.dumps(
            [sources.RUNTIME_REPO, sources.runtime_version(Path(self.engine)), worlds]
        ).encode()).hexdigest()[:16]
        with self.schema_lock:
            if self.schema[0] == signature:
                return self.schema[1]
            cache = self.root / ".cache" / f"options-{signature}.json"
            if not cache.is_file():
                cache.parent.mkdir(exist_ok=True)
                temporary = cache.with_suffix(".tmp")
                self.read_options(temporary)
                temporary.replace(cache)
            self.schema = (signature, json.loads(cache.read_text())["games"])
            return self.schema[1]

    SCHEMA_TIMEOUT = 300
    SCHEMA_UPDATE_INTERVAL = 0.25

    def read_options(self, output):
        """Runs options_schema.py, relaying its progress to admins at most every SCHEMA_UPDATE_INTERVAL seconds."""
        last = {"stage": None, "sent": 0.0, "step": None}

        def report(step, force=False):
            now = time.monotonic()
            if force or step["stage"] != last["stage"] or now - last["sent"] >= self.SCHEMA_UPDATE_INTERVAL:
                last.update(stage=step["stage"], sent=now, step=step)
                self.broadcast({"type": "schema", **step}, admin_only=True)

        report({"stage": "starting", "done": 0, "total": 0, "label": ""}, force=True)
        process = subprocess.Popen([sys.executable, os.path.join(DIR, "options_schema.py"), str(output)],
                                   cwd=self.engine, env={**os.environ, "SKIP_REQUIREMENTS_UPDATE": "1"},
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace")
        killed = threading.Event()

        def kill():
            killed.set()
            process.kill()

        timer = threading.Timer(self.SCHEMA_TIMEOUT, kill)
        timer.start()
        messages, step = collections.deque(maxlen=200), None
        try:
            for line in process.stdout:
                if line.startswith("PROGRESS "):
                    try:
                        step = json.loads(line[len("PROGRESS "):])
                    except ValueError:
                        messages.append(line)
                        continue
                    report(step)
                else:
                    messages.append(line)
            process.wait()
        finally:
            timer.cancel()
            process.stdout.close()
        if killed.is_set():
            raise subprocess.TimeoutExpired(process.args, self.SCHEMA_TIMEOUT)
        if step is not None and step is not last["step"]:
            report(step, force=True)
        if process.returncode or not output.is_file():
            raise RuntimeError("Could not read game options:\n" + "".join(messages)[-6000:])

    # -- seed lifecycle
    def idle(self):
        """No seed yet: an empty room whose admins add players in the editor to create the first seed."""
        self.ctx.update(graph=copy.deepcopy(EMPTY_GRAPH), graph_path=None, zip=None, downloads=[], checks={},
                        server=Server(None, self.port), tracker=Tracker(f"ws://127.0.0.1:{self.port}", []))

    def start_campaign(self, zip_path):
        """Starts MultiServer for a seed, waits until it listens and attaches a fresh tracker."""
        zip_path = os.path.abspath(zip_path)
        graph_path = zip_path[:-4] + ".graph.json"
        with open(graph_path) as source:
            graph = json.load(source)
        with zipfile.ZipFile(zip_path) as archive:
            downloads = [name for name in archive.namelist() if not name.endswith(".archipelago")]
        downloads.append(os.path.basename(graph_path))
        checks = load_checks(zip_path)
        server = Server(zip_path, self.port)
        server.start()
        deadline = time.monotonic() + 300
        try:
            while True:
                if not server.running:
                    raise RuntimeError("Game server failed to start:\n" + "\n".join(server.log))
                try:
                    with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Game server did not listen within 300 seconds:\n" + "\n".join(server.log))
                    time.sleep(0.1)
        except BaseException:
            server.stop()
            raise
        tracker = Tracker(f"ws://127.0.0.1:{self.port}", graph["players"])
        threading.Thread(target=tracker.run, daemon=True).start()
        with self.lock:
            if self.left_seed != graph.get("seed"):
                self.left, self.left_seed = {}, graph.get("seed")
        self.ctx.update(graph=graph, graph_path=graph_path, zip=zip_path, downloads=downloads,
                        checks=checks, server=server, tracker=tracker)

    def stop_current(self):
        tracker, server = self.ctx.get("tracker"), self.ctx.get("server")
        if tracker is not None:
            tracker.stop()
        if server is not None:
            server.stop()

    def regenerate(self, inputs, commit, settings):
        with self.lock:
            if self.state["busy"] or self.closing:
                return False
            self.state["busy"] = True
        self.generation_log.clear()
        threading.Thread(target=self._rebuild, args=(inputs, commit, settings), daemon=True).start()
        return True

    def control_server(self, action):
        with self.lock:
            if self.state["busy"] or self.closing:
                return False
            self.state["busy"] = True
        threading.Thread(target=self._control_server, args=(action,), daemon=True).start()
        return True

    def _control_server(self, action):
        self.update(phase="stopping" if action == "stop" else "starting",
                    message="Saving and stopping the game server" if action == "stop"
                    else "Resuming the current seed", progress=0.0, error=None)
        try:
            if action == "stop":
                self.stop_current()
                for player in self.ctx["tracker"].state.values():
                    player.update(online=False, games=0)
            elif not self.ctx["server"].running:
                if not self.ctx.get("zip"):
                    raise ValueError("There is no seed yet. Use Edit players & settings to create the first one.")
                self.stop_current()
                self.start_campaign(self.ctx["zip"])
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            self.update(phase="error", busy=False, message="Could not change the game server state.",
                        error=str(error))
            return
        running = self.ctx["server"].running
        self.update(phase="running" if running else "stopped", busy=False, progress=1.0,
                    error=None, message="Current seed resumed. Players can reconnect." if running
                    else "Game server stopped. Start server to resume this seed, or edit to create a new one.")

    def _generation_line(self, line):
        self.generation_log.append(line)
        self.broadcast({"type": "log", "lines": [line]}, admin_only=True)
        for marker, progress, label in PHASES:
            if marker in line and progress > self.state["progress"]:
                self.update(progress=progress, message=label)

    def _set_process(self, process):
        self.process = process
        if self.closing:
            process.terminate()

    def _rebuild(self, inputs, commit, settings):
        self.update(phase="generating", message="Generating a new seed", progress=0.01, error=None)
        try:
            launch = launcher()
            launch.sync_worlds(Path(self.engine))
            campaign = launch.generate_inputs(
                Path(sys.executable), Path(self.engine), inputs, {**os.environ, "SKIP_REQUIREMENTS_UPDATE": "1"},
                on_line=self._generation_line, on_process=self._set_process,
                prefer_other_players=settings["prefer_other_players"])
            commit()
        except Exception as error:
            if self.closing:
                return
            running = self.ctx.get("server") is not None and self.ctx["server"].running
            self.update(phase="running" if running else "error", progress=0.0, busy=False,
                        message="Generation failed. Nothing was changed" + (
                            "; the previous seed is still running." if running else "."),
                        error=str(error) + ("\n\n" + "\n".join(list(self.generation_log)[-25:])
                                            if self.generation_log else ""))
            return
        finally:
            self.process = None
        if self.closing:
            return
        try:
            self.update(phase="stopping", progress=0.96, message="Stopping the previous game server")
            self.stop_current()
            self.update(phase="starting", progress=0.98, message="Starting the new game server")
            self.start_campaign(str(campaign))
        except Exception as error:
            self.update(phase="error", progress=0.0, busy=False, message="The new game server failed to start.",
                        error=str(error))
            return
        self.update(phase="running", progress=1.0, busy=False, error=None,
                    message="New seed is running. Players must reconnect and start fresh.")
        self.broadcast({"type": "reload", "seed": self.ctx["graph"]["seed"]})

    # -- players who have left: their checks for other players unlock one at a time
    LEFT_RETRY = 30  # seconds before an unconfirmed automatic unlock may be sent again

    def left_candidates(self, slot):
        """Unchecked locations in a player's world whose items go to other players, earliest progression first."""
        graph = self.ctx.get("graph", {})
        player = next((p for p in graph.get("players", []) if p["slot"] == slot), None)
        tracked = self.ctx["tracker"].state.get(slot) if self.ctx.get("tracker") else None
        if player is None or tracked is None:
            return []
        order = {}
        for index, sphere in enumerate(graph.get("plans", {}).get("all", {}).get("spheres", [])):
            for node in sphere:
                if node.get("finder") == slot and node.get("location_id") is not None:
                    order.setdefault(node["location_id"], index)
        checked = tracked["checked"]
        candidates = [check for check in self.ctx.get("checks", {}).get(slot, [])
                      if check["receiver"] != player["name"] and check["id"] not in checked]
        return sorted(candidates, key=lambda check: (order.get(check["id"], float("inf")), check["id"]))

    def set_left(self, slot, left):
        with self.lock:
            if not left:
                self.left.pop(slot, None)
            elif slot not in self.left:
                self.left[slot] = {"since": time.monotonic(), "sent": {}}

    def set_left_interval(self, seconds):
        if not launcher().valid_left_interval(seconds):
            low, high = launcher().LEFT_PLAYER_INTERVAL
            raise ValueError(f"The interval must be a whole number of seconds from {low} to {high}")
        launcher().save_room_settings({"left_player_interval": seconds})
        self.left_interval = seconds

    def left_status(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            entries = {slot: entry["since"] for slot, entry in self.left.items()}
        return {slot: {"next_in": max(0, round(self.left_interval - (now - since), 1)),
                       "remaining": len(self.left_candidates(slot))}
                for slot, since in entries.items()}

    def tick_left(self, now=None):
        """Grants one remaining check for other players per departed player whose interval elapsed."""
        now = time.monotonic() if now is None else now
        server, tracker = self.ctx.get("server"), self.ctx.get("tracker")
        if server is None or tracker is None or not server.running:
            return
        with self.lock:
            due = [(slot, entry) for slot, entry in self.left.items() if now - entry["since"] >= self.left_interval]
        for slot, entry in due:
            tracked = tracker.state.get(slot)
            if not tracked or not tracked["online"]:
                continue
            pending = entry["sent"]
            for location in [location for location in pending if location in tracked["checked"]]:
                del pending[location]
            candidates = [check for check in self.left_candidates(slot)
                          if check["id"] not in pending or now - pending[check["id"]] >= self.LEFT_RETRY]
            if not candidates:
                continue
            check = candidates[0]
            name = next(p["name"] for p in self.ctx["graph"]["players"] if p["slot"] == slot)
            try:
                server.command(f'/send_location {shlex.quote(name)} {check["id"]}')
            except (OSError, ValueError) as error:
                print(f"Could not unlock {check['name']} for {name}: {error}", file=sys.stderr, flush=True)
                continue
            with self.lock:
                pending[check["id"]] = now
                entry["since"] = now

    def watch(self):
        """Reports a game server that stopped on its own."""
        server = self.ctx.get("server")
        if server is not None and not server.running and self.state["phase"] == "running" and not self.state["busy"]:
            self.update(phase="error", message="The game server stopped unexpectedly. Start server to resume, or edit to start a new seed.",
                        error="\n".join(list(server.log)[-25:]))

    def close(self):
        self.closing = True
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        self.stop_current()


def make_handler(ctx):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body, ctype="application/json", status=200, headers=None):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _local(self):
            return TRUST_LOCALHOST and self.client_address[0] in ("127.0.0.1", "::1")

        def _cookie(self, name):
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except CookieError:
                return None
            return jar[name].value if name in jar else None

        def _scheme(self):
            proto = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
            return proto if proto in ("http", "https") else "http"

        def _base_url(self):
            return ctx["oidc"].base_url(self.headers.get("Host", "localhost"), self._scheme())

        def _user(self):
            """The identity-provider user signed in on this browser, if login is configured."""
            if not ctx.get("oidc"):
                return None
            return ctx["oidc"].user(self._cookie(oidc.SESSION_COOKIE))

        def _admin(self):
            if self._local():
                return True
            user = self._user()
            return bool(user and user["admin"])

        def _set_cookie(self, name, value, max_age):
            secure = "; Secure" if self._scheme() == "https" else ""
            return f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"

        def _same_origin(self):
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            return origin is None or origin in (f"http://{host}", f"https://{host}")

        def _auth(self, path, query):
            auth = ctx.get("oidc")
            if auth is None:
                return self._send("login is not configured", "text/plain", 404)
            try:
                if path == "/auth/login":
                    url, state = auth.begin(self._base_url())
                    return self._send("", "text/plain", 303, {
                        "Location": url, "Set-Cookie": self._set_cookie(oidc.LOGIN_COOKIE, state, oidc.LOGIN_TIMEOUT)})
                if "error" in query:
                    raise oidc.AuthError(query.get("error_description", query["error"])[0])
                session = auth.finish(query.get("state", [""])[0], self._cookie(oidc.LOGIN_COOKIE),
                                      query.get("code", [""])[0])
            except oidc.AuthError as error:
                page = f"<!doctype html><title>Login failed</title><p>Login failed: {html.escape(str(error))}</p>" \
                       '<p><a href="/">Back to the room</a></p>'
                return self._send(page, "text/html; charset=utf-8", 400)
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", self._set_cookie(oidc.SESSION_COOKIE, session, oidc.SESSION_LIMIT))
            self.send_header("Set-Cookie", self._set_cookie(oidc.LOGIN_COOKIE, "", 0))
            self.end_headers()

        def _json(self, body, status=200):
            return self._send(json.dumps(body), status=status)

        def _websocket(self):
            key = self.headers.get("Sec-WebSocket-Key")
            if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
                return self._send("expected a WebSocket upgrade", "text/plain", 400)
            room = ctx.get("room")
            if room is None:
                return self._send("room updates are unavailable", "text/plain", 503)
            self.protocol_version = "HTTP/1.1"  # WebSocket clients reject an HTTP/1.0 upgrade response.
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", ws_accept(key))
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            client = SocketClient(self.connection, admin=self._admin() and self._same_origin())
            try:
                room.attach(client)
                while (frame := ws_read(self.rfile)) is not None:
                    opcode, payload = frame
                    if opcode == 0x8:
                        client.send(payload[:2], 0x8)
                        break
                    if opcode == 0x9:
                        client.send(payload, 0xA)
            except OSError:
                pass
            finally:
                room.detach(client)

        def _tunnel(self):
            """Relays a game client's WebSocket to the game server, so it is reachable through the website's
            port (e.g. wss://host:443 behind a TLS proxy)."""
            self.protocol_version = "HTTP/1.1"  # WebSocket clients only parse HTTP/1.1 error responses.
            self.close_connection = True
            room = ctx.get("room")
            server = ctx.get("server")
            if room is None or server is None or not server.running:
                return self._send("The game server is not running", "text/plain", 503)
            try:
                upstream = socket.create_connection(("127.0.0.1", room.port), timeout=5)
            except OSError:
                return self._send("The game server is not reachable", "text/plain", 502)
            upstream.settimeout(None)
            self.close_connection = True
            # Reverse proxies may forward HTTP/1.0, but the upstream WebSocket handshake requires HTTP/1.1.
            head = f"{self.command} {self.path} HTTP/1.1\r\n" + "".join(
                f"{key}: {value}\r\n" for key, value in self.headers.items()) + "\r\n"
            try:
                upstream.sendall(head.encode("latin-1"))
            except (OSError, UnicodeEncodeError):
                upstream.close()
                return self._send("Could not reach the game server", "text/plain", 502)

            def pipe(source, target, read):
                try:
                    while data := read(65536):
                        target.sendall(data)
                except (OSError, ValueError):
                    pass
                finally:
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            back = threading.Thread(target=pipe, args=(upstream, self.connection, upstream.recv), daemon=True)
            back.start()
            # Bytes the client sent after the handshake may already sit in rfile's buffer, so read through it.
            pipe(self.connection, upstream, self.rfile.read1)
            back.join()
            upstream.close()

        def _editor_get(self, path, query):
            room = ctx.get("room")
            if not self._admin():
                return self._json({"error": "Editing is only available on the host machine or to admins"}, 403)
            if room is None:
                return self._json({"error": "Editing is unavailable"}, 503)
            if path == "/api/config":
                return self._json({"players": read_players(room.root),
                                   "prefer_other_players": launcher().room_settings()["prefer_other_players"],
                                   "left_player_interval": room.left_interval})
            try:
                schema = room.options_schema()
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                return self._json({"error": str(error)}, 500)
            if path == "/api/games":
                return self._json(sorted(schema))
            game = query.get("game", [""])[0]
            if game not in schema:
                return self._json({"error": f"Unknown game: {game}"}, 404)
            return self._json(schema[game])

        def _editor_save(self, body):
            room = ctx.get("room")
            if room is None:
                return self._json({"error": "Editing is unavailable"}, 503)
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json" \
                    or not self._same_origin():
                return self._json({"error": "Expected a same-origin JSON request"}, 403)
            if room.state["busy"]:
                return self._json({"error": "The room is already being regenerated"}, 409)
            try:
                games = room.options_schema()
                inputs, commit, settings = plan_config(room.root, body, games)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                return self._json({"error": str(error)}, 400)
            if not room.regenerate(inputs, commit, settings):
                return self._json({"error": "The room is already being regenerated"}, 409)
            return self._json({"ok": True}, 202)

        def do_GET(self):
            path = unquote(self.path.split("?")[0])
            query = parse_qs(self.path.partition("?")[2])
            if path == "/ws":
                return self._websocket()
            if self.headers.get("Upgrade", "").lower() == "websocket":
                return self._tunnel()
            if path in ("/auth/login", "/auth/callback"):
                return self._auth(path, query)
            if path in ("/api/config", "/api/games", "/api/options"):
                return self._editor_get(path, query)
            if path == "/":
                return self._send(open(os.path.join(DIR, "site.html"), "rb").read(), "text/html; charset=utf-8")
            if path == "/api/graph":
                return self._send(json.dumps(ctx["graph"]))
            if path == "/api/checks":
                return self._send(json.dumps(ctx["checks"]))
            if path == "/api/status":
                tr, srv, room = ctx["tracker"], ctx["server"], ctx.get("room")
                user, local = self._user(), self._local()
                admin = local or bool(user and user["admin"])
                auth = {"login": bool(ctx.get("oidc")) and not local and user is None,
                        "user": user["name"] if user else None}
                return self._send(json.dumps({
                    "server": {"running": srv.running, "port": srv.port, "lan": ctx["lan"],
                               "address": ctx.get("game_address"), "seed": ctx["graph"]["seed"]},
                    "versions": ctx.get("versions"),
                    "players": {slot: {"checked": sorted(s["checked"]), "received": s["received"],
                                       "status": s["status"], "games": s["games"],
                                       "status_name": "goal reached" if s["status"] == 30
                                       else STATUS_NAMES.get(max(s["status"], 5), "playing") if s["games"]
                                       else "offline",
                                       "tracking": s["online"]}
                                for slot, s in tr.state.items()},
                    "feed": list(tr.feed)[-80:],
                    "admin": admin,
                    "auth": auth,
                    "log": list(srv.log)[-200:] if admin else [],
                    "downloads": ctx["downloads"],
                    "room": room.snapshot() if room else None,
                    "left": room.left_status() if room and admin else {},
                    "left_interval": room.left_interval if room else None,
                }))
            if path.startswith("/download/"):
                name = path[len("/download/"):]
                if name not in ctx["downloads"]:
                    return self._send("not found", "text/plain", 404)
                if name.endswith(".graph.json"):
                    data = open(ctx["graph_path"], "rb").read()
                else:
                    with zipfile.ZipFile(ctx["zip"]) as z:
                        data = z.read(name)
                return self._send(data, "application/octet-stream",
                                  headers={"Content-Disposition": f'attachment; filename="{name}"'})
            self._send("not found", "text/plain", 404)

        def do_POST(self):
            if self.path == "/auth/logout" and ctx.get("oidc"):
                if not self._same_origin():
                    return self._send("Expected a same-origin request", "text/plain", 403)
                target = ctx["oidc"].logout(self._cookie(oidc.SESSION_COOKIE), self._base_url()) or "/"
                return self._send("", "text/plain", 303, {
                    "Location": target, "Set-Cookie": self._set_cookie(oidc.SESSION_COOKIE, "", 0)})
            if self.path not in ("/api/command", "/api/check", "/api/config", "/api/server", "/api/left",
                                 "/api/room-settings"):
                return self._send("not found", "text/plain", 404)
            if not self._admin():
                return self._send(json.dumps({"error": "admin commands are only accepted from the host machine "
                                                       "or a signed-in admin"}), status=403)
            if not self._local() and (not self._same_origin() or self.headers.get("Content-Type", "")
                                      .split(";")[0].strip() != "application/json"):
                return self._json({"error": "Expected a same-origin JSON request"}, 403)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
            except (ValueError, UnicodeDecodeError):
                return self._send(json.dumps({"error": "Invalid JSON request"}), status=400)
            if self.path == "/api/config":
                return self._editor_save(body)
            if self.path in ("/api/left", "/api/room-settings"):
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json" \
                        or not self._same_origin():
                    return self._json({"error": "Expected a same-origin JSON request"}, 403)
                room = ctx.get("room")
                if room is None:
                    return self._json({"error": "Room controls are unavailable"}, 503)
                if self.path == "/api/room-settings":
                    try:
                        room.set_left_interval(body.get("left_player_interval"))
                    except (OSError, ValueError) as error:
                        return self._json({"error": str(error)}, 400)
                    return self._json({"ok": True, "left_player_interval": room.left_interval})
                if body.get("seed") != ctx["graph"]["seed"]:
                    return self._json({"error": "The room seed changed. Reload before trying again."}, 409)
                slot = body.get("slot")
                if type(slot) is not int or type(body.get("left")) is not bool \
                        or not any(player["slot"] == slot for player in ctx["graph"]["players"]):
                    return self._json({"error": "Unknown player or invalid value"}, 400)
                if not body["left"] and slot in room.left and not room.left_candidates(slot):
                    return self._json({"error": "All of this player's checks for others are unlocked."}, 409)
                room.set_left(slot, body["left"])
                return self._json({"ok": True})
            if self.path == "/api/server":
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json" \
                        or not self._same_origin():
                    return self._json({"error": "Expected a same-origin JSON request"}, 403)
                room = ctx.get("room")
                if room is None:
                    return self._json({"error": "Server controls are unavailable"}, 503)
                if body.get("seed") != ctx["graph"]["seed"]:
                    return self._json({"error": "The room seed changed. Reload before trying again."}, 409)
                if body.get("action") not in ("start", "stop"):
                    return self._json({"error": "Expected start or stop action"}, 400)
                if not room.control_server(body["action"]):
                    return self._json({"error": "The room is busy. Wait for the current operation."}, 409)
                return self._json({"ok": True}, 202)
            if self.path == "/api/check":
                slot, location = body.get("slot"), body.get("location")
                if "seed" in body and body["seed"] != ctx["graph"]["seed"]:
                    return self._send(json.dumps({"error": "The room now runs a different seed. Reload the page."}), status=409)
                if type(slot) is not int or type(location) is not int or not any(
                        check["id"] == location for check in ctx["checks"].get(slot, [])):
                    return self._send(json.dumps({"error": "Unknown player or location"}), status=400)
                tracked = ctx["tracker"].state.get(slot)
                if not ctx["server"].running or not tracked or not tracked["online"]:
                    return self._send(json.dumps({"error": "Server or tracker is not connected"}), status=503)
                if location in ctx["tracker"].state[slot]["checked"]:
                    return self._send(json.dumps({"ok": True, "already_checked": True}))
                player = next(player for player in ctx["graph"]["players"] if player["slot"] == slot)
                try:
                    ctx["server"].command(f'/send_location {shlex.quote(player["name"])} {location}')
                except (OSError, ValueError) as error:
                    return self._send(json.dumps({"error": f"Could not send check: {error}"}), status=503)
                until = time.monotonic() + 5
                while time.monotonic() < until:
                    if location in ctx["tracker"].state[slot]["checked"]:
                        return self._send(json.dumps({"ok": True}))
                    time.sleep(0.05)
                return self._send(json.dumps({"error": "Check sent but confirmation timed out. Refresh before retrying."}), status=504)
            text = str(body.get("command", "")).strip()
            if not text or "\n" in text:
                return self._send(json.dumps({"error": "empty command"}), status=400)
            if not ctx["server"].running:
                return self._send(json.dumps({"error": "The game server is not running"}), status=503)
            try:
                ctx["server"].command(text)
            except (OSError, ValueError) as error:
                return self._send(json.dumps({"error": f"Could not send command: {error}"}), status=503)
            self._send(json.dumps({"ok": True}))

    return Handler


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), which can stall ~30s on macOS.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


def main():
    global AP
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", required=True)
    ap.add_argument("--campaign", help="Seed to host; without it the room starts empty")
    ap.add_argument("--web-port", type=int, default=8085)
    ap.add_argument("--ap-port", type=int, default=38285)
    args = ap.parse_args()
    AP = os.path.abspath(args.runtime)
    ctx = {"lan": lan_ip(), "oidc": oidc.load(ROOT), "game_address": os.environ.get("MULTIWORLD_GAME_ADDRESS") or None,
           "versions": {"archipelago": sources.runtime_version(AP),
                        "worlds": [{key: world.get(key) for key in ("repo", "tag", "file")}
                                   for world in sources.installed_worlds(launcher().worlds_dir())]}}
    room = Room(ctx, AP, args.ap_port)
    httpd = None

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        if args.campaign:
            try:
                room.start_campaign(args.campaign)
                room.update(phase="running", progress=1.0, busy=False, message="Game server running")
            except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile) as error:
                print(f"Could not start {args.campaign}: {error}", file=sys.stderr, flush=True)
                room.idle()
                room.update(phase="error", progress=0.0, busy=False, error=str(error),
                            message="The saved seed could not be started. Edit players & settings to create a new one.")
        else:
            room.idle()
            room.update(phase="stopped", progress=0.0, busy=False,
                        message="No seed yet. Use Edit players & settings to add players and start the first seed.")
        httpd = WebServer(("0.0.0.0", args.web_port), make_handler(ctx))
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
        print(f"Website: http://{ctx['lan']}:{args.web_port}  (http://localhost:{args.web_port})", flush=True)
        print(f"Game server: ws://{ctx['lan']}:{args.ap_port}" + (
            f" (players connect to {ctx['game_address']})" if ctx["game_address"] else ""), flush=True)
        print(f"Edit players and start new seeds at http://localhost:{args.web_port}/ (Edit button)", flush=True)
        if ctx["oidc"]:
            print(f"Remote admins can log in through {ctx['oidc'].issuer}", flush=True)
        while True:
            time.sleep(0.5)
            room.watch()
            try:
                room.tick_left()
            except Exception as error:  # A failed automatic unlock must not take the room down.
                print(f"Automatic unlock failed: {error}", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        print("\nStopping room and saving progress...", flush=True)
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        room.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        sys.exit(f"Room failed: {error}")
