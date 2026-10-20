import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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

    def test_disconnected_tracker_and_stopped_server_reject_unlocks(self):
        self.state[1]["online"] = False
        self.assertEqual(self.request({"slot": 1, "location": 42})[0], 503)
        self.state[1]["online"] = True
        self.context["server"].running = False
        self.assertEqual(self.request({"slot": 1, "location": 42})[0], 503)
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main()
