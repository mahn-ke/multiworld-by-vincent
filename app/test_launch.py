import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("launcher", Path(__file__).with_name("launch.py"))
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


class FakeProcess:
    """Stands in for the generator subprocess: runs `effect(command)` and streams `lines`."""

    def __init__(self, command, effect=None, returncode=0, lines=("progress line\n",), **kwargs):
        self.command, self.kwargs, self.returncode = command, kwargs, returncode
        if effect:
            effect(command, **kwargs)
        self.stdout = iter(lines)

    def wait(self):
        return self.returncode


def fake_popen(effect=None, returncode=0):
    return lambda command, **kwargs: FakeProcess(command, effect, returncode, **kwargs)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="multiworld tests ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.deps = self.root / "app"
        self.deps.mkdir()
        self.engine = self.root / "engine"
        self.engine.mkdir()
        self.worlds = self.root / "released-worlds"
        self.worlds.mkdir()
        self.environment = {}
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("MULTIWORLD_", "ARCHIPELAGO_"))}
        environment["MULTIWORLD_WORLDS"] = str(self.worlds)
        self.patches = [
            patch.object(launch, "ROOT", self.root),
            patch.dict(os.environ, environment, clear=True),
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

    def test_sync_worlds_prefers_local_copies_over_released_ones(self):
        (self.worlds / "one.apworld").write_bytes(b"one")
        (self.root / "two.apworld").write_bytes(b"two")
        (self.engine / "custom_worlds").mkdir()
        (self.engine / "custom_worlds" / "stale.apworld").touch()
        launch.sync_worlds(self.engine)
        self.assertEqual(sorted(path.name for path in (self.engine / "custom_worlds").iterdir()),
                         ["one.apworld", "two.apworld"])
        (self.root / "one.apworld").write_bytes(b"local")
        launch.sync_worlds(self.engine)
        self.assertEqual((self.engine / "custom_worlds" / "one.apworld").read_bytes(), b"local")

    def test_runtime_preset_download_and_offline_fallback(self):
        cache = self.root / ".cache"
        cache.mkdir()
        for name in ("Generate.py", "MultiServer.py"):
            (self.engine / name).touch()
        with patch.dict(os.environ, {"MULTIWORLD_RUNTIME": str(self.engine)}):
            self.assertEqual(launch.runtime(cache), self.engine)

        def download(tag, target):
            target.mkdir()
            for name in ("Generate.py", "MultiServer.py"):
                (target / name).touch()
            (target / "ARCHIPELAGO_VERSION").write_text(tag)
            return target

        with patch.object(launch.sources, "latest_archipelago_tag", return_value="0.7.0"), \
                patch.object(launch.sources, "download_runtime", side_effect=download) as fetched:
            self.assertEqual(launch.runtime(cache), cache / "runtime-0.7.0")
            self.assertEqual(launch.runtime(cache), cache / "runtime-0.7.0")
        fetched.assert_called_once()
        with patch.dict(os.environ, {"ARCHIPELAGO_VERSION": "0.6.9"}), \
                patch.object(launch.sources, "download_runtime", side_effect=download):
            self.assertEqual(launch.runtime(cache), cache / "runtime-0.6.9")
        with patch.object(launch.sources, "latest_archipelago_tag", side_effect=OSError("offline")):
            self.assertIn(launch.runtime(cache).name, ("runtime-0.7.0", "runtime-0.6.9"))
            for path in cache.glob("runtime-*"):
                (path / "ARCHIPELAGO_VERSION").unlink()
            with self.assertRaisesRegex(RuntimeError, "latest Archipelago release"):
                launch.runtime(cache)

    def test_latest_campaign_requires_existing_seed_files(self):
        self.assertIsNone(launch.latest_campaign())
        campaign = self.root / "runs" / "r" / "output" / "AP_1.zip"
        campaign.parent.mkdir(parents=True)
        (self.root / "latest.json").write_text(json.dumps({"campaign": "runs/r/output/AP_1.zip"}))
        self.assertIsNone(launch.latest_campaign())
        campaign.touch()
        campaign.with_suffix(".graph.json").write_text("{}")
        self.assertEqual(launch.latest_campaign(), campaign)

    def run_main(self, *argv):
        served = Mock()
        with patch.object(launch.sys, "argv", ["launch.py", *argv]), \
                patch.object(launch, "cache_dir", return_value=self.root / ".cache"), \
                patch.object(launch, "check_ports"), \
                patch.object(launch, "runtime", return_value=self.engine), \
                patch.object(launch, "python_environment", return_value=Path("python")), \
                patch.object(launch, "update_worlds"), \
                patch.object(launch, "sync_worlds"), \
                patch.object(launch, "generate", return_value=self.root / "new.zip") as generated, \
                patch.object(launch, "serve", served):
            launch.main()
        return served.call_args.args[2] if served.called else None, generated

    def test_main_resumes_generates_or_starts_empty(self):
        with self.assertRaisesRegex(ValueError, "No player YAMLs"):
            self.run_main()
        campaign, generated = self.run_main("--allow-empty")
        self.assertIsNone(campaign)
        generated.assert_not_called()
        (self.root / "a.yaml").touch()
        campaign, generated = self.run_main("--resume")
        self.assertEqual(campaign, self.root / "new.zip")
        saved = self.root / "runs" / "AP_1.zip"
        saved.parent.mkdir()
        saved.touch()
        saved.with_suffix(".graph.json").touch()
        (self.root / "latest.json").write_text(json.dumps({"campaign": "runs/AP_1.zip"}))
        campaign, generated = self.run_main("--resume")
        self.assertEqual(campaign, saved)
        generated.assert_not_called()
        campaign, generated = self.run_main()
        self.assertEqual(campaign, self.root / "new.zip")

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

        with patch.object(launch.subprocess, "Popen", side_effect=fake_popen(generate)):
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
            with patch.object(launch.subprocess, "Popen", side_effect=fake_popen(returncode=returncode)):
                with self.assertRaises(RuntimeError):
                    launch.generate(Path("python"), self.engine, [source], {})
        self.assertFalse((self.root / "latest.json").exists())

    def test_streams_output_and_prefers_other_players_by_default(self):
        commands, lines = [], []

        def generate(command, **kwargs):
            commands.append(command)
            output = Path(command[-1])
            (output / "AP_9.zip").touch()
            (output / "AP_9.graph.json").write_text("{}")

        inputs = [("b.yaml", b"name: B"), ("a.yaml", b"name: A")]
        with patch.object(launch.subprocess, "Popen", side_effect=fake_popen(generate)):
            campaign = launch.generate_inputs(Path("python"), self.engine, inputs, {}, on_line=lines.append)
            self.assertEqual(lines, ["progress line"])
            self.assertNotIn("--allow-self-checks", commands[-1])
            self.assertEqual((campaign.parent.parent / "players" / "b.yaml").read_bytes(), b"name: B")
            self.assertIn("progress line", (campaign.parent.parent / "generation.log").read_text())
            launch.save_room_settings({"prefer_other_players": False})
            launch.generate_inputs(Path("python"), self.engine, inputs, {})
            self.assertIn("--allow-self-checks", commands[-1])
            launch.generate_inputs(Path("python"), self.engine, inputs, {}, prefer_other_players=True)
            self.assertNotIn("--allow-self-checks", commands[-1])

    def test_room_settings_default_and_validation(self):
        self.assertEqual(launch.room_settings(), {"prefer_other_players": True, "left_player_interval": 60})
        (self.root / launch.SETTINGS_FILE).write_text('{"left_player_interval": 0}')
        self.assertEqual(launch.room_settings()["left_player_interval"], 60)
        launch.save_room_settings({"left_player_interval": 3600})
        launch.save_room_settings({"prefer_other_players": False})
        self.assertEqual(launch.room_settings(), {"prefer_other_players": False, "left_player_interval": 3600})
        (self.root / launch.SETTINGS_FILE).write_text("{broken")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            launch.room_settings()

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
            with self.assertRaisesRegex(RuntimeError, "status 1"):
                launch.serve(Path("python"), self.engine, None, args, {})
            self.assertNotIn("--campaign", popen.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
