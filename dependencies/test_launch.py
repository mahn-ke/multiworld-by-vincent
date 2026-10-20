import argparse
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("launcher", Path(__file__).with_name("launch.py"))
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="multiworld tests ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.deps = self.root / "dependencies"
        self.deps.mkdir()
        self.engine = self.root / "engine"
        self.engine.mkdir()
        self.environment = {}
        self.patches = [
            patch.object(launch, "ROOT", self.root),
            patch.object(launch, "DEPS", self.deps),
        ]
        for patched in self.patches:
            patched.start()
            self.addCleanup(patched.stop)

    def test_all_adjacent_yaml_extensions_only(self):
        for name in ("a.yaml", "b.yml", "C.YAML", ".hidden.yaml", "ignore.txt"):
            (self.root / name).touch()
        (self.deps / "not-a-player.yaml").touch()
        (self.root / "directory.yaml").mkdir()
        self.assertEqual({path.name for path in launch.player_files(self.root)},
                         {"a.yaml", "b.yml", "C.YAML", ".hidden.yaml"})

    def test_no_yaml_is_an_explicit_error(self):
        with self.assertRaisesRegex(ValueError, "No player YAMLs"):
            launch.player_files(self.root)

    def test_invalid_equal_and_occupied_ports(self):
        for web, game in ((0, 20), (65536, 20), (20, -1), (12345, 12345)):
            with self.assertRaises(ValueError):
                launch.check_ports(web, game)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(ValueError, "unavailable"):
                launch.check_ports(port, 2 if port != 2 else 3)

    def test_sync_worlds_and_duplicate_names(self):
        worlds = self.deps / "worlds"
        worlds.mkdir()
        (worlds / "one.apworld").write_bytes(b"one")
        (self.root / "two.apworld").write_bytes(b"two")
        launch.sync_worlds(self.engine)
        self.assertEqual(sorted(path.name for path in (self.engine / "custom_worlds").iterdir()),
                         ["one.apworld", "two.apworld"])
        (self.root / "one.apworld").touch()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            launch.sync_worlds(self.engine)
        self.assertEqual((self.engine / "custom_worlds" / "one.apworld").read_bytes(), b"one")

    def test_each_generation_has_isolated_inputs_and_relative_metadata(self):
        inputs = [self.root / "a.yaml", self.root / ".b.YML"]
        for source in inputs:
            source.write_text("game: Pathogenic")

        def generate(command, **kwargs):
            output = Path(command[-1])
            (output / "AP_123.zip").touch()
            (output / "AP_123.graph.json").write_text("{}")
            self.assertEqual(kwargs["cwd"], self.engine)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(launch.subprocess, "run", side_effect=generate):
            first = launch.generate(Path("python"), self.engine, inputs, self.environment)
            second = launch.generate(Path("python"), self.engine, inputs, self.environment)
        self.assertNotEqual(first, second)
        metadata = json.loads((self.root / "latest.json").read_text())
        self.assertFalse(Path(metadata["campaign"]).is_absolute())
        self.assertEqual(self.root / metadata["campaign"], second)
        snapshots = second.parent.parent / "players"
        self.assertEqual({path.read_text() for path in snapshots.iterdir()}, {"game: Pathogenic"})
        self.assertTrue(all(not path.name.startswith(".") for path in snapshots.iterdir()))
        self.assertFalse(second.with_suffix(".apsave").exists())

    def test_failed_or_incomplete_generation_never_reuses_an_old_seed(self):
        source = self.root / "a.yaml"
        source.touch()
        for returncode in (0, 1):
            with patch.object(launch.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], returncode)):
                with self.assertRaises(RuntimeError):
                    launch.generate(Path("python"), self.engine, [source], {})
        self.assertFalse((self.root / "latest.json").exists())

    def test_room_failure_propagates(self):
        args = argparse.Namespace(web_port=8085, ap_port=38285)
        with patch.object(launch.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 1
            popen.return_value.poll.return_value = 1
            with self.assertRaisesRegex(RuntimeError, "status 1"):
                launch.serve(Path("python"), self.engine, Path("new.zip"), args, {})
            command = popen.call_args.args[0]
            self.assertIn(str(self.engine), command)
            self.assertIn("new.zip", command)


if __name__ == "__main__":
    unittest.main()
