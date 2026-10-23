import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import Mock, patch
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


class DownloadTests(unittest.TestCase):
    def test_minecraft_data_file_is_publicly_downloadable(self):
        temporary = tempfile.TemporaryDirectory(prefix="room download ")
        self.addCleanup(temporary.cleanup)
        name = "AP_test_P1_Player.apmc"
        archive_path = Path(temporary.name) / "seed.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(name, b"minecraft data")
        context = {"downloads": [name], "zip": str(archive_path)}
        server = site.WebServer(("127.0.0.1", 0), site.make_handler(context))
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        with urlopen(f"http://127.0.0.1:{server.server_port}/download/{name}") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"minecraft data")
            self.assertIn(name, response.headers["Content-Disposition"])


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
        self.assertEqual(json.loads((self.root / "room-settings.json").read_text()),
                         {"prefer_other_players": False, "left_player_interval": 60})

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


class OptionsSchemaProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="options schema ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine = self.root / "engine"
        self.engine.mkdir()
        self.room = site.Room({"graph": {"seed": "S1", "players": []}}, str(self.engine), 0, root=self.root)
        self.sent = []
        self.room.broadcast = lambda message, admin_only=False: self.sent.append((message, admin_only))

    def fake_script(self, body):
        folder = self.root / "app"
        folder.mkdir(exist_ok=True)
        (folder / "options_schema.py").write_text("import json, sys, time\n" + body)
        patcher = patch.object(site, "DIR", str(folder))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_script_reports_every_world_and_game(self):
        (self.engine / "Options.py").write_text(
            "class Visibility:\n    simple_ui = 1\n    complex_ui = 2\n"
            "def get_option_groups(world, visibility):\n    return {}\n")
        worlds = self.engine / "worlds"
        for name in ("alpha", "beta", "_helpers"):
            (worlds / name).mkdir(parents=True)
            (worlds / name / "__init__.py").write_text("")
        (worlds / "__init__.py").write_text(
            "import importlib, types\n"
            "for name in ('alpha', 'beta'):\n    importlib.import_module('.' + name, 'worlds')\n"
            "importlib.import_module('._helpers', 'worlds')\n"
            "AutoWorldRegister = types.SimpleNamespace(world_types={\n"
            "    'Alpha': types.SimpleNamespace(hidden=False), 'Beta': types.SimpleNamespace(hidden=False),\n"
            "    'Hidden': types.SimpleNamespace(hidden=True)})\n")
        self.room.SCHEMA_UPDATE_INTERVAL = 0
        self.assertEqual(sorted(self.room.options_schema()), ["Alpha", "Beta"])
        steps = [(m["stage"], m["done"], m["total"], m["label"]) for m, admin in self.sent]
        self.assertTrue(all(admin for _, admin in self.sent))
        self.assertEqual(steps, [
            ("starting", 0, 0, ""), ("starting", 0, 0, ""),
            ("worlds", 0, 2, ""), ("worlds", 1, 2, "alpha"), ("worlds", 2, 2, "beta"),
            ("options", 1, 2, "Alpha"), ("options", 2, 2, "Beta"), ("saving", 2, 2, "")])
        self.sent.clear()
        self.room.options_schema()
        self.assertEqual(self.sent, [], "a cached schema loads without progress messages")

    def test_progress_is_throttled_but_stage_changes_and_the_last_step_get_through(self):
        self.fake_script(
            "for i in range(1, 51):\n"
            "    print('PROGRESS ' + json.dumps({'stage': 'worlds', 'done': i, 'total': 50, 'label': str(i)}))\n"
            "print('PROGRESS ' + json.dumps({'stage': 'options', 'done': 1, 'total': 2, 'label': 'A'}))\n"
            "print('PROGRESS ' + json.dumps({'stage': 'options', 'done': 2, 'total': 2, 'label': 'B'}))\n"
            "json.dump({'games': {'A': {}, 'B': {}}}, open(sys.argv[1], 'w'))\n")
        self.room.SCHEMA_UPDATE_INTERVAL = 100
        self.room.options_schema()
        steps = [(m["stage"], m["done"]) for m, _ in self.sent]
        self.assertEqual(steps, [("starting", 0), ("worlds", 1), ("options", 1), ("options", 2)])

    def test_failure_reports_output_without_progress_lines(self):
        self.fake_script("print('PROGRESS {\"stage\": \"starting\"}')\n"
                         "print('Skipping Broken: boom', file=sys.stderr)\nsys.exit(1)\n")
        with self.assertRaises(RuntimeError) as raised:
            self.room.options_schema()
        self.assertIn("Skipping Broken: boom", str(raised.exception))
        self.assertNotIn("PROGRESS", str(raised.exception))

    def test_timeout_kills_the_script(self):
        self.fake_script("time.sleep(30)\n")
        self.room.SCHEMA_TIMEOUT = 0.5
        with self.assertRaises(site.subprocess.TimeoutExpired):
            self.room.options_schema()


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

    def post(self, body, content_type="application/json", path="/api/config", origin=None):
        headers = {"Content-Type": content_type}
        if origin:
            headers["Origin"] = origin
        request = Request(f"http://{self.url}{path}", data=json.dumps(body).encode(), headers=headers)
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

    def test_server_control_requires_local_same_origin_json_and_current_seed(self):
        self.room.control_server = Mock(return_value=True)
        body = {"action": "stop", "seed": "S1"}
        self.assertEqual(self.post(body, path="/api/server"), (202, {"ok": True}))
        self.room.control_server.assert_called_once_with("stop")
        self.room.control_server.reset_mock()
        self.assertEqual(self.post(body, "text/plain", "/api/server")[0], 403)
        self.assertEqual(self.post(body, path="/api/server", origin="http://evil.example")[0], 403)
        self.assertEqual(self.post({"action": "stop", "seed": "old"}, path="/api/server")[0], 409)
        self.assertEqual(self.post({"action": "restart", "seed": "S1"}, path="/api/server")[0], 400)
        handler = self.server.RequestHandlerClass
        with patch.object(handler, "_local", return_value=False):
            self.assertEqual(self.post(body, path="/api/server")[0], 403)
        self.room.control_server.assert_not_called()
        self.room.control_server.return_value = False
        self.assertEqual(self.post(body, path="/api/server")[0], 409)

    def test_stop_and_resume_keep_the_same_campaign(self):
        server = SimpleNamespace(running=True)
        self.context.update(server=server, zip="existing.zip",
                            tracker=SimpleNamespace(state={1: {"online": True, "games": 1, "checked": {42}}}))

        def stop():
            server.running = False

        def start(campaign):
            self.assertEqual(campaign, "existing.zip")
            server.running = True

        self.room.stop_current = Mock(side_effect=stop)
        self.room.start_campaign = Mock(side_effect=start)
        self.room._control_server("stop")
        self.assertFalse(self.context["tracker"].state[1]["online"])
        self.assertEqual(self.context["tracker"].state[1]["checked"], {42})
        self.assertEqual(self.room.state["phase"], "stopped")
        self.room.watch()
        self.assertEqual(self.room.state["phase"], "stopped")
        self.room._control_server("start")
        self.room.start_campaign.assert_called_once_with("existing.zip")
        self.assertEqual(self.context["graph"]["seed"], "S1")
        self.assertEqual(self.room.state["phase"], "running")
        self.assertFalse(self.room.state["busy"])

    def test_server_control_busy_errors_and_start_idempotence(self):
        self.context["server"] = SimpleNamespace(running=True)
        self.room.start_campaign = Mock()
        self.room._control_server("start")
        self.room.start_campaign.assert_not_called()
        self.room.state["busy"] = True
        self.assertFalse(self.room.control_server("stop"))
        self.assertFalse(self.room.regenerate([], lambda: None, {}))
        self.room.state["busy"] = False
        self.room.stop_current = Mock(side_effect=OSError("save failed"))
        self.room._control_server("stop")
        self.assertEqual(self.room.state["error"], "save failed")
        self.assertFalse(self.room.state["busy"])

    def left_room(self):
        commands = []
        state = {1: {"online": True, "checked": {10}}, 2: {"online": True, "checked": set()}}
        self.context.update(
            graph={"seed": "S1", "players": [{"slot": 1, "name": "Gone Player"}, {"slot": 2, "name": "B"}],
                   "plans": {"all": {"spheres": [[{"finder": 1, "location_id": 13}], [{"finder": 1, "location_id": 11}]]}}},
            checks={1: [{"id": i, "name": f"L{i}", "item": "x", "receiver": r}
                        for i, r in ((10, "B"), (11, "B"), (12, "Gone Player"), (13, "B"), (14, "B"))]},
            server=SimpleNamespace(running=True, command=commands.append),
            tracker=SimpleNamespace(state=state))
        return commands, state

    def test_left_player_unlocks_checks_for_others_one_per_interval(self):
        commands, state = self.left_room()
        self.room.left_interval = 60
        # Unchecked, for other players, progression in sphere order first, then by id; self-items never.
        self.assertEqual([c["id"] for c in self.room.left_candidates(1)], [13, 11, 14])
        with patch.object(site.time, "monotonic", return_value=1000):
            self.room.set_left(1, True)
        self.room.tick_left(now=1059)
        self.assertEqual(commands, [])
        self.room.tick_left(now=1060)
        self.assertEqual(commands, ["/send_location 'Gone Player' 13"])
        self.room.tick_left(now=1100)
        self.assertEqual(len(commands), 1)
        # A short interval skips the still-unconfirmed unlock until the retry delay and sends the next one.
        self.room.left_interval = 5
        self.room.tick_left(now=1080)
        self.assertEqual(commands[-1], "/send_location 'Gone Player' 11")
        state[1]["checked"].update({11, 13})
        self.room.tick_left(now=1085)
        self.assertEqual(commands[-1], "/send_location 'Gone Player' 14")
        state[1]["checked"].add(14)
        self.assertEqual(self.room.left_status(now=1085), {1: {"next_in": 5, "remaining": 0}})
        self.room.tick_left(now=2000)
        self.assertEqual(len(commands), 3)
        self.assertNotIn(12, [int(c.split()[-1]) for c in commands])

    def test_left_player_pauses_when_offline_and_can_be_cleared(self):
        commands, state = self.left_room()
        self.room.left_interval = 1
        self.room.set_left(1, True)
        self.room.left[1]["since"] = 0
        state[1]["online"] = False
        self.room.tick_left(now=10)
        self.context["server"].running = False
        state[1]["online"] = True
        self.room.tick_left(now=10)
        self.assertEqual(commands, [])
        self.room.set_left(1, False)
        self.context["server"].running = True
        self.room.tick_left(now=10)
        self.assertEqual(commands, [])

    def test_left_and_interval_endpoints(self):
        self.left_room()
        self.assertEqual(self.post({"slot": 1, "left": True, "seed": "S1"}, path="/api/left"), (200, {"ok": True}))
        self.assertIn(1, self.room.left)
        for body in ({"slot": 9, "left": True, "seed": "S1"}, {"slot": "1", "left": True, "seed": "S1"},
                     {"slot": 1, "left": 1, "seed": "S1"}):
            self.assertEqual(self.post(body, path="/api/left")[0], 400)
        self.assertEqual(self.post({"slot": 1, "left": False, "seed": "old"}, path="/api/left")[0], 409)
        self.assertEqual(self.post({"slot": 1, "left": False, "seed": "S1"}, "text/plain", "/api/left")[0], 403)
        self.assertIn(1, self.room.left)
        self.assertEqual(self.post({"slot": 1, "left": False, "seed": "S1"}, path="/api/left")[0], 200)
        self.assertNotIn(1, self.room.left)
        self.context["tracker"].state[1]["checked"].update({11, 13, 14})
        self.assertEqual(self.post({"slot": 1, "left": True, "seed": "S1"}, path="/api/left")[0], 200)
        self.assertEqual(self.post({"slot": 1, "left": False, "seed": "S1"}, path="/api/left")[0], 409)
        self.assertIn(1, self.room.left)
        for value in (0, 3601, 1.5, "60", True, None):
            self.assertEqual(self.post({"left_player_interval": value}, path="/api/room-settings")[0], 400)
        self.assertEqual(self.post({"left_player_interval": 3600}, path="/api/room-settings"),
                         (200, {"ok": True, "left_player_interval": 3600}))
        self.assertEqual(json.loads((self.root / "room-settings.json").read_text())["left_player_interval"], 3600)
        with urlopen(f"http://{self.url}/api/config") as response:
            self.assertEqual(json.load(response)["left_player_interval"], 3600)
        handler = self.server.RequestHandlerClass
        with patch.object(handler, "_local", return_value=False):
            self.assertEqual(self.post({"left_player_interval": 5}, path="/api/room-settings")[0], 403)
            self.assertEqual(self.post({"slot": 1, "left": True, "seed": "S1"}, path="/api/left")[0], 403)
        self.assertEqual(self.room.left_interval, 3600)


if __name__ == "__main__":
    unittest.main()
