"""Container provisioning runs without keys, accounts or external network."""
import importlib.util
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).absolute().parent.parent
SPEC = importlib.util.spec_from_file_location("container_runner", ROOT / "docker/runner.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class ContainerTests(unittest.TestCase):
    @contextmanager
    def environment_setup(self, values=None):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            empty = {"WEIXIN_" + name: "" for name in (
                "TUNNEL_ID", "TUNNEL_RUNTIME_KEY", "TUNNEL_PROXY", "CALLBACK_PROXY",
                "CALLBACK_DNS", "MEDIA_PROXY", "MEDIA_DNS")}
            with patch.multiple(RUNNER, ROOT=data, CONFIG=data / "config/bridge.json",
                                PROFILE=data / "config/tunnel.yaml", KEY=data / "secrets/runtime.key"), \
                    patch.dict(os.environ, dict(empty, **(values or {}))):
                RUNNER.initialize()
                yield data

    def test_environment_bootstraps_tunnel_and_network_without_prompt_or_secret_output(self):
        secret = "fixture-runtime-key-from-private-dotenv"
        values = {"WEIXIN_TUNNEL_ID": "tunnel_" + "a" * 32,
                  "WEIXIN_TUNNEL_RUNTIME_KEY": secret,
                  "WEIXIN_TUNNEL_PROXY": "http://127.0.0.1:10800",
                  "WEIXIN_CALLBACK_PROXY": "http://127.0.0.1:10801",
                  "WEIXIN_CALLBACK_DNS": "google",
                  "WEIXIN_MEDIA_PROXY": "http://127.0.0.1:10802",
                  "WEIXIN_MEDIA_DNS": "system"}
        with self.environment_setup(values), patch.object(RUNNER, "run") as run, \
                patch.object(RUNNER.sys, "argv", ["runner.py"]), \
                patch.object(RUNNER.getpass, "getpass", side_effect=AssertionError("unexpected prompt")), \
                patch("builtins.print") as output:
            self.assertEqual(RUNNER.main(), 0)
            run.assert_called_once()
            self.assertNotIn("WEIXIN_TUNNEL_RUNTIME_KEY", os.environ)
            self.assertEqual(RUNNER.KEY.read_bytes(), secret.encode())
            profile = json.loads(RUNNER.PROFILE.read_text())
            self.assertEqual(profile["control_plane"]["api_key"], "file:" + str(RUNNER.KEY))
            self.assertEqual(profile["control_plane"]["http_proxy"], values["WEIXIN_TUNNEL_PROXY"])
            self.assertNotIn(secret, RUNNER.PROFILE.read_text())
            config = json.loads(RUNNER.CONFIG.read_text())
            for key in ("callback_proxy", "callback_dns", "media_proxy", "media_dns"):
                self.assertEqual(config[key], values["WEIXIN_" + key.upper()])
            for path in (RUNNER.KEY, RUNNER.PROFILE, RUNNER.CONFIG):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config["callback_hosts"], [])
            output.assert_not_called()

    def test_environment_preserves_existing_binding_subscription_and_unset_configuration(self):
        from wechat_dot_bridge.store import Store
        with self.environment_setup() as data:
            store = Store(data / "state")
            store.import_binding({"id": "fixture-account", "owner": "fixture-owner", "token": "fixture-token",
                                  "base_url": "https://ilinkai.weixin.qq.com"})
            store.db.execute("INSERT INTO event_metadata VALUES('fixture-policy','preserve')")
            store.close()
            before_db = (data / "state/bridge.sqlite3").read_bytes()
            config = json.loads(RUNNER.CONFIG.read_text())
            config.update(callback_hosts=["callback.example.com"], callback_proxy="http://127.0.0.1:10800")
            RUNNER.write_private(RUNNER.CONFIG, config)
            profile = RUNNER.tunnel_profile("tunnel_" + "b" * 32, "http://127.0.0.1:10801")
            profile["control_plane"]["fixture_extra_option"] = True
            RUNNER.write_private(RUNNER.PROFILE, profile)
            RUNNER.write_private(RUNNER.KEY, b"fixture-existing-runtime-key")
            before = {path: path.read_bytes() for path in (RUNNER.CONFIG, RUNNER.PROFILE, RUNNER.KEY)}
            RUNNER.apply_environment()
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            with patch.dict(os.environ, {"WEIXIN_CALLBACK_DNS": "google"}):
                RUNNER.apply_environment()
                modified = json.loads(RUNNER.CONFIG.read_text())
                self.assertEqual(modified, dict(config, callback_dns="google"))
                stamp = RUNNER.CONFIG.stat().st_mtime_ns
                RUNNER.apply_environment()
                self.assertEqual(RUNNER.CONFIG.stat().st_mtime_ns, stamp)
            self.assertEqual(RUNNER.PROFILE.read_bytes(), before[RUNNER.PROFILE])
            self.assertEqual(RUNNER.KEY.read_bytes(), before[RUNNER.KEY])
            self.assertEqual((data / "state/bridge.sqlite3").read_bytes(), before_db)

    def test_environment_can_rotate_key_without_replacing_profile_options_and_clear_proxies(self):
        with self.environment_setup():
            profile = RUNNER.tunnel_profile("tunnel_" + "c" * 32, "http://127.0.0.1:10800")
            profile["control_plane"]["fixture_extra_option"] = True
            RUNNER.write_private(RUNNER.PROFILE, profile)
            RUNNER.write_private(RUNNER.KEY, b"fixture-existing-runtime-key")
            config = json.loads(RUNNER.CONFIG.read_text())
            config.update(callback_proxy="http://127.0.0.1:10801", media_proxy="http://127.0.0.1:10802")
            RUNNER.write_private(RUNNER.CONFIG, config)
            values = {"WEIXIN_TUNNEL_RUNTIME_KEY": "fixture-rotated-runtime-key", "WEIXIN_TUNNEL_PROXY": "direct",
                      "WEIXIN_CALLBACK_PROXY": "direct", "WEIXIN_MEDIA_PROXY": "direct"}
            with patch.dict(os.environ, values):
                RUNNER.apply_environment()
            actual = json.loads(RUNNER.PROFILE.read_text())
            self.assertEqual(actual["control_plane"]["tunnel_id"], profile["control_plane"]["tunnel_id"])
            self.assertTrue(actual["control_plane"]["fixture_extra_option"])
            self.assertNotIn("http_proxy", actual["control_plane"])
            self.assertEqual(RUNNER.KEY.read_bytes(), b"fixture-rotated-runtime-key")
            modified = json.loads(RUNNER.CONFIG.read_text())
            self.assertIsNone(modified["callback_proxy"])
            self.assertIsNone(modified["media_proxy"])

    def test_invalid_environment_preserves_all_previous_private_files(self):
        invalid = ({"WEIXIN_TUNNEL_ID": "invalid"}, {"WEIXIN_TUNNEL_RUNTIME_KEY": "short"},
                   {"WEIXIN_TUNNEL_RUNTIME_KEY": "fixture-runtime-key with whitespace"},
                   {"WEIXIN_TUNNEL_PROXY": "http://remote.example.com:10800"},
                   {"WEIXIN_CALLBACK_PROXY": "http://127.0.0.1:10800/path"},
                   {"WEIXIN_CALLBACK_DNS": "unknown"}, {"WEIXIN_MEDIA_DNS": "unknown"})
        with self.environment_setup():
            RUNNER.write_private(RUNNER.PROFILE, RUNNER.tunnel_profile("tunnel_" + "d" * 32))
            RUNNER.write_private(RUNNER.KEY, b"fixture-existing-runtime-key")
            before = {path: path.read_bytes() for path in (RUNNER.CONFIG, RUNNER.PROFILE, RUNNER.KEY)}
            for values in invalid:
                with self.subTest(setting=next(iter(values))), patch.dict(os.environ, values):
                    with self.assertRaises(ValueError):
                        RUNNER.apply_environment()
                    self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_environment_rejects_unsafe_destination_before_any_private_file_changes(self):
        values = {"WEIXIN_TUNNEL_ID": "tunnel_" + "e" * 32,
                  "WEIXIN_TUNNEL_RUNTIME_KEY": "fixture-new-runtime-key",
                  "WEIXIN_CALLBACK_DNS": "google"}
        for destination in ("CONFIG", "PROFILE", "KEY"):
            with self.subTest(destination=destination), self.environment_setup(values) as data:
                RUNNER.write_private(RUNNER.PROFILE, RUNNER.tunnel_profile("tunnel_" + "f" * 32))
                RUNNER.write_private(RUNNER.KEY, b"fixture-existing-runtime-key")
                before = {path: path.read_bytes() for path in (RUNNER.CONFIG, RUNNER.PROFILE, RUNNER.KEY)}
                target = getattr(RUNNER, destination)
                protected = data / "protected"
                target.rename(protected)
                target.symlink_to(protected)
                with self.assertRaisesRegex(ValueError, "unsafe_runtime_config"):
                    RUNNER.apply_environment()
                self.assertEqual(protected.read_bytes(), before[target])
                for path in before.keys() - {target}:
                    self.assertEqual(path.read_bytes(), before[path])

    def test_profile_uses_file_secret_and_stdio_without_model_runtime(self):
        self.assertEqual(RUNNER.PROFILE.suffix, ".yaml")
        value = RUNNER.tunnel_profile("tunnel_" + "a" * 32, "http://127.0.0.1:10800")
        self.assertTrue(value["control_plane"]["api_key"].startswith("file:"))
        self.assertIn("serve", value["mcp"]["commands"][0]["command"])
        self.assertEqual(value["health"]["listen_addr"], "127.0.0.1:0")
        for identifier in ("invalid", "tunnel_../file"):
            with self.assertRaises(ValueError):
                RUNNER.tunnel_profile(identifier)

    def test_provisioning_supervisor_stays_live_and_restarts_with_same_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            environment = dict(os.environ, HOME=str(data / "home"), BRIDGE_DATA_DIR=str(data), BRIDGE_HEALTH_PORT=str(port), PYTHONPATH=str(ROOT))
            for _ in range(2):
                process = subprocess.Popen([sys.executable, str(ROOT / "docker/runner.py")], cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 12
                    while True:
                        try:
                            with urllib.request.urlopen("http://127.0.0.1:" + str(port) + "/healthz", timeout=1) as response:
                                state = json.load(response)
                            break
                        except Exception:
                            if time.monotonic() >= deadline or process.poll() is not None:
                                self.fail("provisioning supervisor did not become healthy")
                            time.sleep(0.1)
                    self.assertEqual(state["mode"], "waiting_for_tunnel")
                    self.assertTrue(state["process_healthy"])
                    self.assertFalse(state["ready"])
                    self.assertEqual((data / "state/bridge.sqlite3").stat().st_mode & 0o777, 0o600)
                    self.assertNotIn("token", json.dumps(state))
                finally:
                    process.send_signal(signal.SIGTERM)
                    try:
                        stdout, stderr = process.communicate(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                        self.fail("supervisor did not shut down gracefully")
                self.assertEqual(process.returncode, 0, stderr.decode())

    def test_key_is_written_privately_without_echo(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            profile, key = base / "profile.json", base / "runtime.key"
            profile.write_text(json.dumps(RUNNER.tunnel_profile("tunnel_" + "a" * 32)))
            with patch.object(RUNNER, "PROFILE", profile), patch.object(RUNNER, "KEY", key), patch.object(RUNNER.sys.stdin, "isatty", return_value=True), patch.object(RUNNER.getpass, "getpass", return_value="fixture-not-a-real-runtime-key"), patch("builtins.print") as output:
                RUNNER.setup_tunnel()
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("fixture-not-a-real-runtime-key", str(output.call_args_list))

    def test_owner_password_setup_writes_only_a_private_scrypt_record(self):
        from wechat_dot_bridge.owner_auth import read_password
        with tempfile.TemporaryDirectory() as temporary:
            password = Path(temporary) / "password.json"
            fixture = "fixture-password-only-for-container-tests"
            with patch.object(RUNNER, "PASSWORD", password), patch.object(RUNNER.sys.stdin, "isatty", return_value=True), patch.object(RUNNER.getpass, "getpass", side_effect=[fixture, fixture]), patch("builtins.print") as output:
                RUNNER.setup_password()
            self.assertEqual(password.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(fixture, password.read_text())
            self.assertNotIn(fixture, str(output.call_args_list))
            self.assertEqual(read_password(password)["version"], 1)

    def test_direct_transport_runs_same_worker_and_stops_gracefully(self):
        from wechat_dot_bridge.owner_auth import password_record
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            for suffix in ("", "config", "secrets"):
                (data / suffix).mkdir(mode=0o700)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                health_port = sock.getsockname()[1]
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                http_port = sock.getsockname()[1]
            password = data / "secrets/http-password.json"
            password.write_text(json.dumps(password_record("fixture-password-for-runtime-tests")))
            password.chmod(0o600)
            config = {"state_dir": str(data / "state"), "lock_dir": str(data / "locks"), "callback_hosts": [],
                      "mcp_transport": "direct", "http": {"public_url": "https://203.0.113.10", "listen_host": "127.0.0.1",
                      "listen_port": http_port, "password_file": str(password)}}
            (data / "config/bridge.json").write_text(json.dumps(config))
            (data / "config/bridge.json").chmod(0o600)
            environment = dict(os.environ, HOME=str(data / "home"), BRIDGE_DATA_DIR=str(data), BRIDGE_HEALTH_PORT=str(health_port), PYTHONPATH=str(ROOT))
            process = subprocess.Popen([sys.executable, str(ROOT / "docker/runner.py")], cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        with urllib.request.urlopen("http://127.0.0.1:" + str(health_port) + "/healthz", timeout=2) as response:
                            state = json.load(response)
                        if state["http_ready"]:
                            break
                    except Exception:
                        pass
                    if time.monotonic() >= deadline or process.poll() is not None:
                        self.fail("HTTP transport did not become ready")
                    time.sleep(0.1)
                self.assertEqual(state["mode"], "direct")
                self.assertFalse(state["tunnel_ready"])
                with urllib.request.urlopen("http://127.0.0.1:" + str(http_port) + "/.well-known/oauth-protected-resource/mcp") as response:
                    self.assertEqual(json.load(response)["resource"], "https://203.0.113.10/mcp")
            finally:
                process.send_signal(signal.SIGTERM)
                try:
                    stdout, stderr = process.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    self.fail("direct HTTP transport did not stop gracefully")
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertNotIn(b"fixture-password", stdout + stderr)

    def test_initialize_migrates_legacy_profile_without_replacing_runtime_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            data.mkdir(mode=0o700)
            (data / "config").mkdir(mode=0o700)
            (data / "secrets").mkdir(mode=0o700)
            key = data / "secrets/runtime.key"
            key.write_bytes(b"fixture-not-a-real-runtime-key")
            key.chmod(0o600)
            legacy = data / "config/tunnel.json"
            profile = data / "config/tunnel.yaml"
            value = RUNNER.tunnel_profile("tunnel_" + "a" * 32)
            legacy.write_text(json.dumps(value))
            with patch.object(RUNNER, "ROOT", data), patch.object(RUNNER, "CONFIG", data / "config/bridge.json"), patch.object(RUNNER, "PROFILE", profile):
                RUNNER.initialize()
            self.assertEqual(json.loads(profile.read_text()), value)
            self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
            self.assertEqual(key.read_bytes(), b"fixture-not-a-real-runtime-key")
