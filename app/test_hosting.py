"""Container hosting: downloaded runtime and games, the empty room and the game-server tunnel."""
import asyncio
import hashlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

import websockets
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

spec = importlib.util.spec_from_file_location("room_hosting", Path(__file__).with_name("room.py"))
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)
sources = site.sources


class FakeGitHub:
    """Serves the GitHub API and release downloads used by sources.py."""

    def __init__(self):
        self.repos = [{"full_name": "org/peglin"}, {"full_name": "org/empty"}, {"full_name": "org/old", "archived": True},
                      {"full_name": "org/tools"}]
        self.files = {"https://dl/peglin.apworld": b"peglin world"}
        self.files["https://dl/SHA256SUMS"] = (hashlib.sha256(b"peglin world").hexdigest() + "  peglin.apworld\n").encode()
        self.releases = {
            "org/peglin": {"tag_name": "v1.2.0", "assets": [
                {"name": "peglin.apworld", "browser_download_url": "https://dl/peglin.apworld"},
                {"name": "PeglinMultiworld.zip", "browser_download_url": "https://dl/plugin.zip"},
                {"name": "SHA256SUMS", "browser_download_url": "https://dl/SHA256SUMS"}]},
            "org/tools": {"tag_name": "v1", "assets": []},
        }

    def get_json(self, url):
        if "/orgs/org/repos" in url:
            return self.repos
        repo = url.split("/repos/")[1].split("/releases")[0]
        if repo not in self.releases:
            raise HTTPError(url, 404, "Not Found", {}, None)
        return self.releases[repo]

    def download(self, url, target):
        Path(target).write_bytes(self.files[url])


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        for name in ("get_json", "download"):
            patcher = patch.object(sources, name, getattr(self.github, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_fetches_latest_release_apworlds_with_manifest(self):
        target = self.root / "worlds"
        target.mkdir()
        (target / "removed.apworld").touch()
        manifest = sources.fetch_worlds("org", target)
        self.assertEqual(sorted(path.name for path in target.iterdir()), ["peglin.apworld", "worlds.json"])
        self.assertEqual(manifest, [{"repo": "org/peglin", "tag": "v1.2.0", "file": "peglin.apworld",
                                     "sha256": hashlib.sha256(b"peglin world").hexdigest()}])
        self.assertEqual(sources.installed_worlds(target), manifest)
        # Readable by other users: the Docker image fetches as root and runs the room unprivileged.
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
    def test_checksum_mismatch_and_duplicates_keep_the_previous_download(self):
        target = self.root / "worlds"
        sources.fetch_worlds("org", target)
        self.github.files["https://dl/peglin.apworld"] = b"tampered"
        with self.assertRaisesRegex(ValueError, "SHA256SUMS"):
            sources.fetch_worlds("org", target)
        self.github.files["https://dl/peglin.apworld"] = b"peglin world"
        self.github.releases["org/tools"]["assets"] = [{"name": "peglin.apworld", "browser_download_url": "x"}]
        with self.assertRaisesRegex(ValueError, "Several repositories"):
            sources.fetch_worlds("org", target)
        self.assertEqual((target / "peglin.apworld").read_bytes(), b"peglin world")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["worlds"])

    def test_extracts_github_source_archive(self):
        archive = self.root / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name in ("Generate.py", "MultiServer.py", "LICENSE", "worlds/__init__.py"):
                info = tarfile.TarInfo(f"Archipelago-0.7.0/{name}")
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
        runtime = sources.extract_runtime(archive, self.root / "runtime", "0.7.0")
        self.assertTrue((runtime / "Generate.py").is_file())
        self.assertTrue((runtime / "custom_worlds").is_dir())
        self.assertEqual(sources.runtime_version(runtime), "0.7.0")
        with self.assertRaisesRegex(ValueError, "Unexpected release tag"):
            sources.safe_tag("../../etc")

    def test_runtime_release_and_archive_use_multiworldgg(self):
        with patch.object(sources, "get_json", return_value={"tag_name": "0.7.274"}) as lookup:
            self.assertEqual(sources.latest_runtime_tag(), "0.7.274")
        lookup.assert_called_once_with("https://api.github.com/repos/MultiworldGG/MultiworldGG/releases/latest")
        with patch.object(sources, "download") as download, \
                patch.object(sources, "extract_runtime") as extract:
            sources.download_runtime("0.7.274", self.root / "runtime")
        self.assertEqual(download.call_args.args[0],
                         "https://github.com/MultiworldGG/MultiworldGG/archive/refs/tags/0.7.274.tar.gz")
        self.assertEqual(extract.call_args.args[1:], (self.root / "runtime", "0.7.274"))

    def test_options_cache_is_invalidated_when_runtime_changes(self):
        engine = self.root / "runtime"
        engine.mkdir()
        marker = engine / "ARCHIPELAGO_VERSION"
        marker.write_text("0.7.273")
        room = site.Room({}, str(engine), 38999, root=self.root)

        def read_schema(command, **kwargs):
            Path(command[-1]).write_text(json.dumps({"games": {"Balatro": {"options": []}}}))
            return Mock(returncode=0)

        with patch.object(site.subprocess, "run", side_effect=read_schema) as read:
            self.assertIn("Balatro", room.options_schema())
            self.assertIn("Balatro", room.options_schema())
            self.assertEqual(read.call_count, 1)
            marker.write_text("0.7.274")
            self.assertIn("Balatro", room.options_schema())
            self.assertEqual(read.call_count, 2)
        self.assertEqual(len(list((self.root / ".cache").glob("options-*.json"))), 2)


class ServerStartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.campaign = self.root / "AP_test.zip"
        with site.zipfile.ZipFile(self.campaign, "w"):
            pass
        self.campaign.with_suffix(".graph.json").write_text(json.dumps({"seed": "test", "players": []}))
        self.room = site.Room({}, str(self.root), 38999, root=self.root)
        self.server = Mock(running=True, log=[])

    def test_cold_world_scan_can_take_more_than_thirty_seconds(self):
        with patch.object(site, "load_checks", return_value={}), \
                patch.object(site, "Server", return_value=self.server), \
                patch.object(site, "Tracker"), \
                patch.object(site.time, "monotonic", side_effect=[0, 60, 120]), \
                patch.object(site.time, "sleep"), \
                patch.object(site.socket, "create_connection",
                             side_effect=[OSError("loading"), OSError("loading"), MagicMock()]):
            self.room.start_campaign(self.campaign)
        self.assertIs(self.room.ctx["server"], self.server)
        self.server.stop.assert_not_called()

    def test_unresponsive_game_server_still_times_out_and_stops(self):
        with patch.object(site, "load_checks", return_value={}), \
                patch.object(site, "Server", return_value=self.server), \
                patch.object(site.time, "monotonic", side_effect=[0, 301]), \
                patch.object(site.socket, "create_connection", side_effect=OSError("loading")):
            with self.assertRaisesRegex(RuntimeError, "within 300 seconds"):
                self.room.start_campaign(self.campaign)
        self.server.stop.assert_called_once()


class HostingTests(unittest.TestCase):
    def start(self, context):
        server = site.WebServer(("127.0.0.1", 0), site.make_handler(context))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(stop)
        return server.server_port

    def get(self, port, path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_empty_room_serves_status_and_cannot_start_a_server(self):
        context = {"lan": "192.0.2.1", "oidc": None, "game_address": "room.example:443",
                   "versions": {"archipelago": "0.6.8", "worlds": []}}
        room = site.Room(context, "/nonexistent", 38999, root=tempfile.gettempdir())
        room.idle()
        room.update(phase="stopped", busy=False)
        port = self.start(context)
        status = json.loads(self.get(port, "/api/status")[1])
        self.assertEqual(status["server"], {"running": False, "port": 38999, "lan": "192.0.2.1",
                                            "address": "room.example:443", "seed": None})
        self.assertEqual((status["players"], status["downloads"]), ({}, []))
        self.assertEqual(status["versions"]["archipelago"], "0.6.8")
        self.assertEqual(json.loads(self.get(port, "/api/graph")[1])["players"], [])
        self.assertTrue(room.control_server("start"))
        for _ in range(100):
            if not room.state["busy"]:
                break
            threading.Event().wait(0.02)
        self.assertEqual(room.state["phase"], "error")
        self.assertIn("no seed yet", room.state["error"])

    def test_untrusted_localhost_is_not_admin(self):
        port = self.start({"lan": "x", "oidc": None, "graph": {"seed": None}, "checks": {}, "downloads": [],
                           "tracker": site.Tracker("ws://x", []), "server": site.Server(None, 1)})
        self.assertTrue(json.loads(self.get(port, "/api/status")[1])["admin"])
        with patch.object(site, "TRUST_LOCALHOST", False):
            self.assertFalse(json.loads(self.get(port, "/api/status")[1])["admin"])
            self.assertEqual(self.get(port, "/api/config")[0], 403)

    def test_game_clients_reach_the_game_server_through_the_website(self):
        async def scenario():
            async def echo(socket):
                async for message in socket:
                    await socket.send(f"{socket.request.path} {message}")

            async with serve(echo, "127.0.0.1", 0) as game:
                game_port = game.sockets[0].getsockname()[1]
                room = type("R", (), {"port": game_port})()
                running = type("S", (), {"running": True})()
                web = self.start({"room": room, "server": running})
                async with connect(f"ws://127.0.0.1:{web}/") as client:
                    await client.send("hello")
                    first = await client.recv()
                    await client.send("x" * 200000)
                    second = await client.recv()
                running.running = False
                with self.assertRaises(websockets.InvalidStatus):
                    async with connect(f"ws://127.0.0.1:{web}/"):
                        pass
                return first, second

        first, second = asyncio.run(scenario())
        self.assertEqual(first, "/ hello")
        self.assertEqual(len(second), len("/ ") + 200000)


if __name__ == "__main__":
    unittest.main()
