import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from websockets.sync.client import connect

spec = importlib.util.spec_from_file_location("room_site", Path(__file__).with_name("room.py"))
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


class ChecklistTests(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.state = {1: {"online": True, "checked": set()}}

        def command(text):
            self.commands.append(text)
            self.state[1]["checked"].add(42)

        self.context = {
            "checks": {1: [{"id": 42, "name": "A location", "item": "An item", "receiver": "Other"}]},
            "graph": {"players": [{"slot": 1, "name": "Player With Spaces"}]},
            "tracker": SimpleNamespace(state=self.state),
            "server": SimpleNamespace(running=True, command=command),
        }
        self.server = site.WebServer(("127.0.0.1", 0), site.make_handler(self.context))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, body):
        request = Request(self.url + "/api/check", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_lists_all_checks(self):
        with urlopen(self.url + "/api/checks") as response:
            self.assertEqual(json.load(response), {"1": self.context["checks"][1]})

    def test_confirmed_unlock_and_duplicate_are_idempotent(self):
        self.assertEqual(self.request({"slot": 1, "location": 42}), (200, {"ok": True}))
        self.assertEqual(self.commands, ["/send_location 'Player With Spaces' 42"])
        self.assertEqual(self.request({"slot": 1, "location": 42}),
                         (200, {"ok": True, "already_checked": True}))
        self.assertEqual(len(self.commands), 1)

    def test_invalid_locations_and_types_do_not_send_commands(self):
        for body in ([], {}, {"slot": 2, "location": 42}, {"slot": 1, "location": 43},
                     {"slot": True, "location": 42}, {"slot": "1", "location": 42}):
            self.assertEqual(self.request(body)[0], 400)
        self.assertEqual(self.commands, [])

    def test_unlock_for_a_replaced_seed_is_rejected(self):
        self.context["graph"]["seed"] = "new"
        self.assertEqual(self.request({"slot": 1, "location": 42, "seed": "old"})[0], 409)
        self.assertEqual(self.commands, [])

    def test_disconnected_tracker_and_stopped_server_reject_unlocks(self):
        self.state[1]["online"] = False
        self.assertEqual(self.request({"slot": 1, "location": 42})[0], 503)
        self.state[1]["online"] = True
        self.context["server"].running = False
        self.assertEqual(self.request({"slot": 1, "location": 42})[0], 503)
        self.assertEqual(self.commands, [])


YAML = b"""# keep me
name: Craig
game: Pathogenic
description: test
Pathogenic:
  death_link: true  # comment
"""


class EditorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="room editor ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "Craig.yaml").write_bytes(YAML)
        (self.root / "Other.yaml").write_bytes(b"name: Other\ngame: Peglin\n")
        (self.root / "Multi.yaml").write_bytes(b"name: A\ngame: Peglin\n---\nname: B\ngame: Peglin\n")
        patcher = patch.object(site.launcher(), "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.games = {"Pathogenic": {}, "Peglin": {}}

    def plan(self, players, prefer=True):
        return site.plan_config(self.root, {"players": players, "prefer_other_players": prefer}, self.games)

    def test_reads_editable_and_complex_players(self):
        players = {p["file"]: p for p in site.read_players(self.root)}
        self.assertEqual(players["Craig.yaml"]["options"], {"death_link": True})
        self.assertTrue(players["Craig.yaml"]["editable"])
        self.assertFalse(players["Multi.yaml"]["editable"])

    def test_unchanged_files_keep_their_bytes_and_nothing_is_written_before_commit(self):
        inputs, commit, settings = self.plan([{"file": "Craig.yaml", "name": "Craig", "game": "Pathogenic"}], prefer=False)
        self.assertEqual(dict(inputs)["Craig.yaml"], YAML)
        self.assertEqual(sorted(dict(inputs)), ["Craig.yaml", "Multi.yaml", "Other.yaml"])
        inputs, commit, settings = self.plan([
            {"file": "Craig.yaml", "name": "Craig2", "game": "Pathogenic", "set": {"coin_checks": 5}, "unset": ["death_link"]},
            {"file": "Other.yaml", "name": "Other", "game": "Pathogenic"},
            {"file": "Multi.yaml", "remove": True},
            {"file": None, "name": "New Player", "game": "Peglin", "set": {"death_link": True}},
        ], prefer=False)
        self.assertEqual((self.root / "Craig.yaml").read_bytes(), YAML)
        self.assertEqual(sorted(dict(inputs)), ["Craig.yaml", "New_Player.yaml", "Other.yaml"])
        commit()
        import yaml
        craig = yaml.safe_load((self.root / "Craig.yaml").read_text())
        self.assertEqual(craig, {"name": "Craig2", "game": "Pathogenic", "description": "test",
                                 "Pathogenic": {"coin_checks": 5}})
        other = yaml.safe_load((self.root / "Other.yaml").read_text())
        self.assertEqual(other, {"name": "Other", "game": "Pathogenic", "Pathogenic": {}})
        self.assertTrue((self.root / "inactive-players" / "Multi.yaml").is_file())
        self.assertFalse((self.root / "Multi.yaml").exists())
        self.assertEqual(yaml.safe_load((self.root / "New_Player.yaml").read_text())["Peglin"], {"death_link": True})
        self.assertEqual(json.loads((self.root / "room-settings.json").read_text()), {"prefer_other_players": False})

    def test_invalid_submissions(self):
        for players in ([{"file": "Craig.yaml", "name": "", "game": "Pathogenic"}],
                        [{"file": "Craig.yaml", "name": "x" * 17, "game": "Pathogenic"}],
                        [{"file": "Craig.yaml", "name": "Same", "game": "Pathogenic"},
                         {"file": "Other.yaml", "name": "same", "game": "Peglin"}],
                        [{"file": "Craig.yaml", "name": "Craig", "game": "Unknown"}],
                        [{"file": "../Craig.yaml", "name": "Craig", "game": "Pathogenic"}],
                        [{"file": "Multi.yaml", "name": "Craig", "game": "Pathogenic"}],
                        [{"file": "Craig.yaml", "name": "Craig", "game": "Pathogenic", "set": []}],
                        [{"file": name, "remove": True} for name in ("Craig.yaml", "Other.yaml", "Multi.yaml")]):
            with self.assertRaises(ValueError, msg=players):
                self.plan(players)


class RoomSocketTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="room socket ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "Craig.yaml").write_bytes(YAML)
        patcher = patch.object(site.launcher(), "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.context = {"graph": {"seed": "S1", "players": []}}
        self.room = site.Room(self.context, str(self.root), 0, root=self.root)
        self.room.state.update(phase="running", busy=False)
        self.room.options_schema = lambda: {"Pathogenic": {"options": []}}
        self.server = site.WebServer(("127.0.0.1", 0), site.make_handler(self.context))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = f"127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, body, content_type="application/json"):
        request = Request(f"http://{self.url}/api/config", data=json.dumps(body).encode(),
                          headers={"Content-Type": content_type})
        try:
            with urlopen(request) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_websocket_receives_state_logs_and_reload(self):
        with connect(f"ws://{self.url}/ws") as socket:
            self.assertEqual(json.loads(socket.recv(timeout=5))["seed"], "S1")
            self.assertEqual(json.loads(socket.recv(timeout=5)), {"type": "log", "lines": []})
            self.room.update(phase="generating", progress=0.1, busy=True)
            state = json.loads(socket.recv(timeout=5))
            self.assertEqual((state["phase"], state["progress"], state["busy"]), ("generating", 0.1, True))
            self.room._generation_line("Filling the multiworld with 10 items.")
            self.assertEqual(json.loads(socket.recv(timeout=5))["lines"], ["Filling the multiworld with 10 items."])
            self.assertEqual(json.loads(socket.recv(timeout=5))["message"], "Randomizing items")
            self.room.broadcast({"type": "reload", "seed": "S2"})
            self.assertEqual(json.loads(socket.recv(timeout=5))["type"], "reload")

    def test_foreign_origin_socket_gets_no_admin_log(self):
        with connect(f"ws://{self.url}/ws", origin="http://evil.example") as socket:
            json.loads(socket.recv(timeout=5))
            self.room._generation_line("secret")
            with self.assertRaises(TimeoutError):
                socket.recv(timeout=0.3)

    def test_save_validates_then_regenerates_once(self):
        calls = []
        self.room.regenerate = lambda *args: calls.append(args) or True
        self.assertEqual(self.post({"players": []}, "text/plain")[0], 403)
        self.assertEqual(self.post({"players": [{"file": "Craig.yaml", "name": "", "game": "Pathogenic"}]})[0], 400)
        self.assertEqual(self.post({"players": [{"file": "Craig.yaml", "name": "Craig", "game": "Pathogenic"}]}),
                         (202, {"ok": True}))
        self.assertEqual(len(calls), 1)
        self.room.state["busy"] = True
        self.assertEqual(self.post({"players": []})[0], 409)
        with urlopen(f"http://{self.url}/api/config") as response:
            config = json.load(response)
        self.assertEqual(config["players"][0]["name"], "Craig")
        self.assertTrue(config["prefer_other_players"])


if __name__ == "__main__":
    unittest.main()
