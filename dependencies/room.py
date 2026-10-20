"""Serve one explicitly selected seed with a tracker and host-only console."""
import argparse
import asyncio
import collections
import json
import os
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import websockets

DIR = os.path.dirname(os.path.abspath(__file__))
AP = None
STATUS_NAMES = {0: "not connected", 5: "connected", 10: "ready", 20: "playing", 30: "goal reached"}

def load_checks(zip_path):
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


class Tracker:
    """One Tracker-tagged connection per slot: checked locations, received items, client status, feed."""

    def __init__(self, url, players):
        self.url, self.players = url, players
        self.state = {p["slot"]: {"checked": set(), "received": 0, "status": 0, "online": False, "games": 0}
                      for p in players}
        self.feed = collections.deque(maxlen=150)
        self.names = {"item": {}, "location": {}}  # game -> id -> name
        self.games = {p["slot"]: p["game"] for p in players}

    def run(self):
        loop = asyncio.new_event_loop()
        for p in self.players:
            loop.create_task(self._track(p, feed=p is self.players[0]))
        loop.run_forever()

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
                        "slot_data": False, "version": {"major": 0, "minor": 6, "build": 8, "class": "Version"}}]))
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
            return self.client_address[0] in ("127.0.0.1", "::1")

        def do_GET(self):
            path = unquote(self.path.split("?")[0])
            if path == "/":
                return self._send(open(os.path.join(DIR, "site.html"), "rb").read(), "text/html; charset=utf-8")
            if path == "/api/graph":
                return self._send(json.dumps(ctx["graph"]))
            if path == "/api/checks":
                return self._send(json.dumps(ctx["checks"]))
            if path == "/api/status":
                tr, srv = ctx["tracker"], ctx["server"]
                return self._send(json.dumps({
                    "server": {"running": srv.running, "port": srv.port, "lan": ctx["lan"],
                               "seed": ctx["graph"]["seed"]},
                    "players": {slot: {"checked": sorted(s["checked"]), "received": s["received"],
                                       "status": s["status"], "games": s["games"],
                                       "status_name": "goal reached" if s["status"] == 30
                                       else STATUS_NAMES.get(max(s["status"], 5), "playing") if s["games"]
                                       else "offline",
                                       "tracking": s["online"]}
                                for slot, s in tr.state.items()},
                    "feed": list(tr.feed)[-80:],
                    "admin": self._local(),
                    "log": list(srv.log)[-200:] if self._local() else [],
                    "downloads": ctx["downloads"],
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
            if self.path not in ("/api/command", "/api/check"):
                return self._send("not found", "text/plain", 404)
            if not self._local():
                return self._send(json.dumps({"error": "admin commands are only accepted from the host machine"}), status=403)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
            except (ValueError, UnicodeDecodeError):
                return self._send(json.dumps({"error": "Invalid JSON request"}), status=400)
            if self.path == "/api/check":
                slot, location = body.get("slot"), body.get("location")
                if type(slot) is not int or type(location) is not int or not any(
                        check["id"] == location for check in ctx["checks"].get(slot, [])):
                    return self._send(json.dumps({"error": "Unknown player or location"}), status=400)
                if not ctx["server"].running or not ctx["tracker"].state[slot]["online"]:
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
            ctx["server"].command(text)
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
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--web-port", type=int, default=8085)
    ap.add_argument("--ap-port", type=int, default=38285)
    args = ap.parse_args()
    AP = os.path.abspath(args.runtime)
    zip_path = os.path.abspath(args.campaign)
    graph_path = zip_path[:-4] + ".graph.json"
    graph = json.load(open(graph_path))
    with zipfile.ZipFile(zip_path) as z:
        downloads = [n for n in z.namelist() if not n.endswith(".archipelago")]
    downloads.append(os.path.basename(graph_path))

    server = Server(zip_path, args.ap_port)
    httpd = None

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        checks = load_checks(zip_path)
        server.start()
        deadline = time.monotonic() + 30
        while True:
            if not server.running:
                raise RuntimeError("Game server failed to start:\n" + "\n".join(server.log))
            try:
                with socket.create_connection(("127.0.0.1", args.ap_port), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Game server did not listen within 30 seconds:\n" + "\n".join(server.log))
                time.sleep(0.1)
        tracker = Tracker(f"ws://127.0.0.1:{args.ap_port}", graph["players"])
        threading.Thread(target=tracker.run, daemon=True).start()
        ctx = {"graph": graph, "graph_path": graph_path, "zip": zip_path, "downloads": downloads,
               "checks": checks, "server": server, "tracker": tracker, "lan": lan_ip()}
        httpd = WebServer(("0.0.0.0", args.web_port), make_handler(ctx))
        httpd.timeout = 0.5
        print(f"Website: http://{ctx['lan']}:{args.web_port}  (http://localhost:{args.web_port})", flush=True)
        print(f"Game server: ws://{ctx['lan']}:{args.ap_port}", flush=True)
        while server.running:
            httpd.handle_request()
        if server.proc.returncode:
            raise RuntimeError("Game server stopped:\n" + "\n".join(server.log))
    except KeyboardInterrupt:
        print("\nStopping room and saving progress...", flush=True)
    finally:
        if httpd is not None:
            httpd.server_close()
        if server.running:
            try:
                server.command("/exit")
            except (OSError, ValueError) as error:
                print(f"Could not request graceful shutdown: {error}", file=sys.stderr)
                server.proc.terminate()
            try:
                server.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.proc.terminate()
                try:
                    server.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.proc.kill()
                    server.proc.wait()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        sys.exit(f"Room failed: {error}")
