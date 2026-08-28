import importlib.util
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("slurmboard", ROOT / "slurmboard.py")
SLURMBOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SLURMBOARD)


class ParseSSHHostsTests(unittest.TestCase):
    def test_reads_concrete_hosts_and_includes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config.d"
            config_dir.mkdir()
            (root / "config").write_text(
                "Include config.d/*.conf\n"
                "Host roihu-gpu another-cluster\n"
                "  HostName login.example.org\n"
                "Host *.example.org !blocked.example.org\n",
                encoding="utf-8",
            )
            (config_dir / "clusters.conf").write_text(
                "Host=included-cluster\n"
                "  HostName included.example.org\n",
                encoding="utf-8",
            )

            hosts = SLURMBOARD.parse_ssh_hosts(root / "config")

        self.assertEqual(hosts, ["included-cluster", "roihu-gpu", "another-cluster"])

    def test_include_cycles_do_not_duplicate_hosts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config").write_text(
                "Host first\nInclude other\n", encoding="utf-8"
            )
            (root / "other").write_text(
                "Host second first\nInclude config\n", encoding="utf-8"
            )

            hosts = SLURMBOARD.parse_ssh_hosts(root / "config")

        self.assertEqual(hosts, ["first", "second"])


class HealthEndpointTests(unittest.TestCase):
    def test_health_does_not_collect_slurm_data(self):
        handler = object.__new__(SLURMBOARD.Handler)
        handler.path = "/health"
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()

        with mock.patch.object(
            SLURMBOARD, "render_page", side_effect=AssertionError("collected data")
        ):
            handler.do_GET()

        handler.send_response.assert_called_once_with(200)
        self.assertEqual(handler.wfile.getvalue(), b"ok\n")


class LauncherConnectionTests(unittest.TestCase):
    def test_connect_uses_alias_tunnel_and_streams_this_script(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config"
            config.write_text("Host roihu-gpu\n", encoding="utf-8")
            state = SLURMBOARD.LauncherState(
                ROOT / "slurmboard.py", config, remote_port=65535,
                state_path=Path(temp_dir) / "launcher.json",
            )
            process = mock.Mock()
            process.poll.return_value = None
            process.stderr = []

            with mock.patch.object(
                SLURMBOARD, "_available_loopback_port", return_value=65534
            ), mock.patch.object(
                SLURMBOARD.subprocess, "Popen", return_value=process
            ) as popen, mock.patch.object(
                SLURMBOARD.threading, "Thread"
            ) as thread:
                state.connect("roihu-gpu")

            command = popen.call_args.args[0]
            self.assertEqual(command[-2], "roihu-gpu")
            self.assertIn("127.0.0.1:65534:127.0.0.1:65535", command)
            self.assertIn("--host 127.0.0.1 --port 65535", command[-1])
            self.assertTrue(popen.call_args.kwargs["stdin"].closed)
            self.assertNotIn("env", popen.call_args.kwargs)
            self.assertEqual(thread.call_count, 2)

    def test_connect_caches_successful_password_via_ssh_askpass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config"
            config.write_text("Host kosh_3080\n", encoding="utf-8")
            state = SLURMBOARD.LauncherState(
                ROOT / "slurmboard.py", config, remote_port=65535,
                state_path=Path(temp_dir) / "launcher.json",
            )
            process = mock.Mock()
            process.poll.return_value = None
            process.stderr = []

            with mock.patch.object(
                SLURMBOARD, "_available_loopback_port", return_value=65534
            ), mock.patch.object(
                SLURMBOARD.subprocess, "Popen", return_value=process
            ) as popen, mock.patch.object(
                SLURMBOARD.threading, "Thread"
            ):
                state.connect("kosh_3080", password="correct horse battery staple")

            command = popen.call_args.args[0]
            environment = popen.call_args.kwargs["env"]
            password_file = Path(environment["SLURMBOARD_ASKPASS_FILE"])
            askpass_file = Path(environment["SSH_ASKPASS"])
            auth_dir = password_file.parent

            self.assertIn("BatchMode=no", command)
            self.assertIn("NumberOfPasswordPrompts=1", command)
            self.assertNotIn("correct horse battery staple", " ".join(command))
            self.assertNotIn("correct horse battery staple", environment.values())
            self.assertEqual(password_file.read_text(encoding="utf-8"),
                             "correct horse battery staple")
            self.assertEqual(stat.S_IMODE(password_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(askpass_file.stat().st_mode), 0o700)
            self.assertNotIn("correct horse battery staple", json.dumps(state.view()))
            self.assertFalse(state.view()["hosts"][0]["password_cached"])

            state._mark_connected(state.connections["kosh_3080"])
            self.assertTrue(state.view()["hosts"][0]["password_cached"])
            self.assertEqual(
                state.password_cache["kosh_3080"], "correct horse battery staple"
            )
            state._clear_auth(state.connections["kosh_3080"])
            self.assertFalse(auth_dir.exists())

            state.forget_password("kosh_3080")
            self.assertFalse(state.view()["hosts"][0]["password_cached"])

    def test_connect_reuses_cached_password_when_field_is_blank(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config"
            config.write_text("Host kosh_3080\n", encoding="utf-8")
            state = SLURMBOARD.LauncherState(
                ROOT / "slurmboard.py", config, remote_port=65535,
                state_path=Path(temp_dir) / "launcher.json",
            )
            state.password_cache["kosh_3080"] = "session secret"
            process = mock.Mock()
            process.poll.return_value = None
            process.stderr = []

            with mock.patch.object(
                SLURMBOARD, "_available_loopback_port", return_value=65534
            ), mock.patch.object(
                SLURMBOARD.subprocess, "Popen", return_value=process
            ) as popen, mock.patch.object(SLURMBOARD.threading, "Thread"):
                state.connect("kosh_3080")

            command = popen.call_args.args[0]
            environment = popen.call_args.kwargs["env"]
            password_file = Path(environment["SLURMBOARD_ASKPASS_FILE"])
            connection = state.connections["kosh_3080"]
            self.assertIn("BatchMode=no", command)
            self.assertEqual(password_file.read_text(encoding="utf-8"),
                             "session secret")
            self.assertTrue(connection["used_cached_password"])
            self.assertIsNone(connection["pending_password"])
            state._clear_auth(connection)

    def test_connect_selects_a_remote_port_automatically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config"
            config.write_text("Host roihu-gpu\n", encoding="utf-8")
            state = SLURMBOARD.LauncherState(
                ROOT / "slurmboard.py", config, remote_port=0,
                state_path=Path(temp_dir) / "launcher.json",
            )
            process = mock.Mock()
            process.poll.return_value = None
            process.stderr = []

            with mock.patch.object(
                SLURMBOARD.secrets, "randbelow", return_value=123
            ), mock.patch.object(
                SLURMBOARD, "_available_loopback_port", return_value=54000
            ), mock.patch.object(
                SLURMBOARD.subprocess, "Popen", return_value=process
            ) as popen, mock.patch.object(
                SLURMBOARD.threading, "Thread"
            ):
                state.connect("roihu-gpu")

            command = popen.call_args.args[0]
            selected_port = 49152 + 123
            self.assertIn(
                f"127.0.0.1:54000:127.0.0.1:{selected_port}", command
            )
            self.assertIn(f"--port {selected_port}", command[-1])

    def test_connect_forwards_a_custom_quota_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config"
            config.write_text("Host general-hpc\n", encoding="utf-8")
            state = SLURMBOARD.LauncherState(
                ROOT / "slurmboard.py", config, remote_port=65535,
                state_path=Path(temp_dir) / "launcher.json",
                quota_command="site-quota --format compact",
            )
            process = mock.Mock()
            process.poll.return_value = None
            process.stderr = []

            with mock.patch.object(
                SLURMBOARD, "_available_loopback_port", return_value=54000
            ), mock.patch.object(
                SLURMBOARD.subprocess, "Popen", return_value=process
            ) as popen, mock.patch.object(SLURMBOARD.threading, "Thread"):
                state.connect("general-hpc")

            remote_command = popen.call_args.args[0][-1]
            self.assertIn(
                "--quota-command 'site-quota --format compact'", remote_command
            )

    def test_port_collision_has_a_retry_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config"
            config.write_text("Host roihu-gpu\n", encoding="utf-8")
            state = SLURMBOARD.LauncherState(
                ROOT / "slurmboard.py", config, remote_port=0,
                state_path=Path(temp_dir) / "launcher.json",
            )
            process = mock.Mock()
            process.poll.return_value = 1
            process.wait.return_value = 1
            connection = {
                "host": "roihu-gpu",
                "local_port": 54000,
                "remote_port": 54123,
                "process": process,
                "status": "connecting",
                "error": "",
                "stderr": ["OSError: [Errno 98] Address already in use"],
                "intentional_stop": False,
            }

            with mock.patch.object(SLURMBOARD.time, "sleep"):
                state._monitor(connection)

            self.assertEqual(connection["status"], "error")
            self.assertIn("Click Connect", connection["error"])
            self.assertNotIn("Traceback", connection["error"])


class LauncherPinTests(unittest.TestCase):
    def test_pins_sort_first_and_persist_between_launcher_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config"
            state_path = root / "launcher.json"
            config.write_text(
                "Host roihu-gpu\nHost lumi\nHost mahti\n", encoding="utf-8"
            )
            state = SLURMBOARD.LauncherState(
                ROOT / "slurmboard.py", config, remote_port=0,
                state_path=state_path,
            )

            state.set_pinned("lumi", True)
            state.set_pinned("roihu-gpu", True)

            first_view = state.view()["hosts"]
            self.assertEqual(
                [host["host"] for host in first_view],
                ["lumi", "roihu-gpu", "mahti"],
            )
            self.assertEqual(
                [host["pinned"] for host in first_view], [True, True, False]
            )

            restarted = SLURMBOARD.LauncherState(
                ROOT / "slurmboard.py", config, remote_port=0,
                state_path=state_path,
            )
            self.assertEqual(
                [host["host"] for host in restarted.view()["hosts"]],
                ["lumi", "roihu-gpu", "mahti"],
            )

            restarted.set_pinned("lumi", False)
            self.assertEqual(
                [host["host"] for host in restarted.view()["hosts"]],
                ["roihu-gpu", "lumi", "mahti"],
            )

    def test_unknown_host_cannot_be_pinned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config"
            config.write_text("Host lumi\n", encoding="utf-8")
            state = SLURMBOARD.LauncherState(
                ROOT / "slurmboard.py", config, remote_port=0,
                state_path=root / "launcher.json",
            )

            with self.assertRaisesRegex(ValueError, "not a concrete alias"):
                state.set_pinned("missing", True)


class LauncherPasswordUITests(unittest.TestCase):
    def test_launcher_offers_a_session_cached_password_field(self):
        page = SLURMBOARD._LAUNCHER_PAGE

        self.assertIn("SSH password (optional)", page)
        self.assertIn("passwordInput.type = 'password'", page)
        self.assertIn("passwordInput.autocomplete = 'current-password'", page)
        self.assertIn("Cached in memory until the launcher quits.", page)
        self.assertIn("Cached after a successful connection", page)
        self.assertIn("Forget cached password", page)
        self.assertIn("passwordInput.value = '';", page)

    def test_connect_endpoint_passes_password_without_returning_it(self):
        state = mock.Mock()
        state.token = "token"
        state.view.return_value = {"hosts": []}
        body = json.dumps({"host": "kosh_3080", "password": "secret"}).encode()
        handler = object.__new__(SLURMBOARD.LauncherHandler)
        handler.path = "/api/connect"
        handler.state = state
        handler.headers = {
            "X-Slurmboard-Token": "token",
            "Content-Length": str(len(body)),
        }
        handler.rfile = io.BytesIO(body)
        handler._json = mock.Mock()

        handler.do_POST()

        state.connect.assert_called_once_with("kosh_3080", password="secret")
        handler._json.assert_called_once_with(200, {"hosts": []})

    def test_forget_password_endpoint_clears_the_host_cache(self):
        state = mock.Mock()
        state.token = "token"
        state.view.return_value = {"hosts": []}
        body = json.dumps({"host": "kosh_3080"}).encode()
        handler = object.__new__(SLURMBOARD.LauncherHandler)
        handler.path = "/api/forget-password"
        handler.state = state
        handler.headers = {
            "X-Slurmboard-Token": "token",
            "Content-Length": str(len(body)),
        }
        handler.rfile = io.BytesIO(body)
        handler._json = mock.Mock()

        handler.do_POST()

        state.forget_password.assert_called_once_with("kosh_3080")
        handler._json.assert_called_once_with(200, {"hosts": []})


class LauncherStartupTests(unittest.TestCase):
    def test_occupied_preferred_port_falls_back_to_an_available_port(self):
        args = mock.Mock(
            remote_port=0,
            ssh_config="/missing/config",
            launcher_port=65432,
            no_browser=True,
        )
        fallback_server = mock.Mock()
        fallback_server.server_address = ("127.0.0.1", 54321)
        fallback_server.serve_forever.side_effect = KeyboardInterrupt

        with mock.patch.object(
            SLURMBOARD, "ThreadingHTTPServer",
            side_effect=[OSError(SLURMBOARD.errno.EADDRINUSE, "in use"), fallback_server],
        ) as server, mock.patch.object(
            SLURMBOARD, "LauncherState"
        ) as state:
            SLURMBOARD.run_launcher(args)

        self.assertEqual(
            server.call_args_list,
            [
                mock.call(("127.0.0.1", 65432), SLURMBOARD.LauncherHandler),
                mock.call(("127.0.0.1", 0), SLURMBOARD.LauncherHandler),
            ],
        )
        state.return_value.close.assert_called_once_with()
        fallback_server.server_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
