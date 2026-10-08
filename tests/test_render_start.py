import copy
import http.client
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import render_start
from tests.test_environment_config import ACCOUNTS
from troTHU import runtime_context as ctx
from troTHU.environment_config import EnvironmentConfigError, read_environment_accounts


def deployment_env(**overrides):
    return {"TRON_ACCOUNTS_JSON": json.dumps(ACCOUNTS), "DISCORD_BOT_TOKEN": "test-token", **overrides}


class RenderStartupTest(unittest.TestCase):
    def setUp(self):
        self.original_config = copy.deepcopy(ctx.CONFIG)

    def tearDown(self):
        ctx.CONFIG.clear()
        ctx.CONFIG.update(copy.deepcopy(self.original_config))

    def test_two_monitors_share_exactly_one_gateway(self):
        specs = render_start.build_process_specs({}, deployment_env())
        self.assertEqual([item.name for item in specs], ["monitor:primary", "monitor:secondary", "discord-gateway"])
        for index, spec in enumerate(specs[:2]):
            accounts = read_environment_accounts(spec.environment)
            self.assertEqual([a.name for a in accounts], [ACCOUNTS[index]["name"]])
            self.assertEqual(spec.command[0], sys.executable)
            self.assertEqual(spec.command[-2:], ("run", "--no-input"))
            self.assertNotIn(ACCOUNTS[index]["passwd"], repr(spec))
        self.assertEqual(len(read_environment_accounts(specs[-1].environment)), 2)

    def test_worker_mode_and_missing_token_do_not_start_gateway(self):
        for value in ("false", "0"):
            specs = render_start.build_process_specs({}, deployment_env(TRON_DISCORD_GATEWAY_ENABLED=value))
            self.assertEqual(len(specs), 2)
        self.assertEqual(len(render_start.build_process_specs({}, {"TRON_USER": "S300", "TRON_PASS": "p"})), 1)
        with self.assertRaises(EnvironmentConfigError):
            render_start.build_process_specs({}, {"TRON_DISCORD_GATEWAY_ENABLED": "true"})
        with self.assertRaises(EnvironmentConfigError):
            render_start.build_process_specs({}, deployment_env(TRON_DISCORD_GATEWAY_ENABLED="tru"))

    def test_gateway_respects_custom_token_environment_name(self):
        config = {"integrations": {"discord": {"token_env": "CUSTOM_TOKEN"}}}
        self.assertEqual(len(render_start.build_process_specs(config, deployment_env())), 2)
        self.assertEqual(len(render_start.build_process_specs(config, deployment_env(CUSTOM_TOKEN="p"))), 3)

    def test_supervisor_restarts_only_failed_child_with_backoff(self):
        specs = render_start.build_process_specs({}, deployment_env())
        supervisor = render_start.ProcessSupervisor(specs)
        children = [MagicMock() for _ in range(5)]
        for child in children:
            child.poll.return_value = None
        with patch.object(render_start.subprocess, "Popen", side_effect=children) as popen, redirect_stdout(io.StringIO()):
            supervisor.tick(0)
            self.assertEqual(supervisor.health()["status"], "ok")
            children[1].poll.return_value = 1
            supervisor.tick(1)
            self.assertEqual(supervisor.health()["running_processes"], 2)
            supervisor.tick(3)
            self.assertEqual(popen.call_count, 3)
            supervisor.tick(4)
            self.assertEqual(popen.call_count, 4)
            children[3].poll.return_value = 1
            supervisor.tick(5)
            supervisor.tick(10)
            self.assertEqual(popen.call_count, 4)
            supervisor.tick(11)
            self.assertEqual(popen.call_count, 5)
            self.assertIs(supervisor.children["discord-gateway"], children[2])

    def test_stop_terminates_all_children_and_kills_unresponsive_child(self):
        supervisor = render_start.ProcessSupervisor([])
        child = MagicMock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("test", 10), 0]
        supervisor.children["test"] = child
        supervisor.stop()
        child.terminate.assert_called_once()
        child.kill.assert_called_once()

    def test_health_http_reflects_process_failure_without_exposing_profiles(self):
        supervisor = render_start.ProcessSupervisor(render_start.build_process_specs({}, deployment_env()))
        server = ThreadingHTTPServer(("127.0.0.1", 0), render_start.make_handler(supervisor))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=3)
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            body = response.read().decode()
            self.assertEqual(response.status, 503)
            self.assertNotIn("primary", body)
            child = MagicMock()
            child.poll.return_value = None
            with patch.object(render_start.subprocess, "Popen", return_value=child), redirect_stdout(io.StringIO()):
                supervisor.tick(0)
            connection.request("GET", "/health")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["running_processes"], 3)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_check_command_is_redacted_and_does_not_start_services(self):
        output = io.StringIO()
        with patch.dict(os.environ, deployment_env()), patch("troTHU.runtime_context.load_config", return_value={"_simple": {"now": "S100"}}), \
                patch.object(render_start.subprocess, "Popen", side_effect=AssertionError("must not start")), \
                redirect_stdout(output):
            self.assertEqual(render_start.main(["--check"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["gateway_count"], 1)
        self.assertFalse(result["connects"])
        self.assertNotIn("test-token", output.getvalue())

    def test_invalid_environment_returns_actionable_redacted_error(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"TRON_ACCOUNTS_JSON": "{DO-NOT-PRINT"}), redirect_stderr(output):
            self.assertEqual(render_start.main(["--check"]), 2)
        self.assertIn("TRON_ACCOUNTS_JSON", output.getvalue())
        self.assertNotIn("DO-NOT-PRINT", output.getvalue())

    def test_real_check_entrypoint_loads_json_without_network_or_config_writes(self):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("TRON_", "DISCORD_", "TEST_"))}
        env.update(deployment_env(TRON_DISCORD_GATEWAY_ENABLED="false"))
        env["PYTHONIOENCODING"] = "utf-8"
        config_path = render_start.ROOT / "config.yaml"
        before = config_path.read_bytes() if config_path.exists() else None
        result = subprocess.run([sys.executable, str(render_start.ROOT / "render_start.py"), "--check"],
            cwd=render_start.ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["processes"], ["monitor:primary", "monitor:secondary"])
        self.assertEqual(report["gateway_count"], 0)
        after = config_path.read_bytes() if config_path.exists() else None
        self.assertEqual(after, before)
        self.assertNotIn("second-password", result.stdout)

    def test_probe_daemon_process_spec_and_env(self):
        specs_explicit = render_start.build_process_specs({}, deployment_env(TRON_PROBE_DAEMON_ENABLED="true"))
        self.assertIn("probe-daemon", [s.name for s in specs_explicit])

        specs_render = render_start.build_process_specs({}, deployment_env(RENDER_EXTERNAL_URL="https://example.com"))
        self.assertIn("probe-daemon", [s.name for s in specs_render])

        specs_disabled = render_start.build_process_specs({}, deployment_env(TRON_PROBE_DAEMON_ENABLED="false", RENDER="true"))
        self.assertNotIn("probe-daemon", [s.name for s in specs_disabled])

        with self.assertRaises(EnvironmentConfigError):
            render_start.build_process_specs({}, deployment_env(TRON_PROBE_DAEMON_ENABLED="bad_val"))

    def test_probe_log_endpoint_returns_json(self):
        supervisor = render_start.ProcessSupervisor([])
        server = ThreadingHTTPServer(("127.0.0.1", 0), render_start.make_handler(supervisor))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=3)
            connection.request("GET", "/api/probe_log")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            data = json.loads(response.read())
            self.assertEqual(data["status"], "ok")
            self.assertIn("entries", data)
            self.assertIn("daemon_status", data)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_dynamic_account_add_and_remove(self):
        with patch.dict(os.environ, deployment_env()):
            supervisor = render_start.ProcessSupervisor([])
            server = ThreadingHTTPServer(("127.0.0.1", 0), render_start.make_handler(supervisor))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                body = json.dumps({"user": "Test999", "passwd": "secretpassword", "school": "usc"}).encode("utf-8")
                connection.request("POST", "/api/account/add", body=body, headers={"Content-Type": "application/json"})
                resp = connection.getresponse()
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read())
                self.assertTrue(data["ok"])
                self.assertIn("Test999", data["accounts_json"])
                self.assertIn("monitor:Test999", [s.name for s in supervisor.specs])

                rem_body = json.dumps({"name": "Test999"}).encode("utf-8")
                connection.request("POST", "/api/account/remove", body=rem_body, headers={"Content-Type": "application/json"})
                rem_resp = connection.getresponse()
                self.assertEqual(rem_resp.status, 200)
                rem_data = json.loads(rem_resp.read())
                self.assertTrue(rem_data["ok"])
                self.assertNotIn("monitor:Test999", [s.name for s in supervisor.specs])
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                try:
                    (render_start.ROOT / "dynamic_accounts.json").unlink(missing_ok=True)
                except Exception:
                    pass

    def test_proxy_js_endpoint_and_cors_preflight(self):
        supervisor = render_start.ProcessSupervisor([])
        server = ThreadingHTTPServer(("127.0.0.1", 0), render_start.make_handler(supervisor))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection(*server.server_address, timeout=3)
            # Test GET /proxy.js
            conn.request("GET", "/proxy.js", headers={"Host": "test.server:8000"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertIn("application/javascript", resp.getheader("Content-Type"))
            self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "*")
            content = resp.read().decode("utf-8")
            self.assertIn("RollcallRelay", content)
            self.assertIn("http://test.server:8000", content)

            # Test OPTIONS CORS preflight
            conn.request("OPTIONS", "/api/submit")
            opt_resp = conn.getresponse()
            self.assertEqual(opt_resp.status, 204)
            self.assertEqual(opt_resp.getheader("Access-Control-Allow-Origin"), "*")
            self.assertIn("POST", opt_resp.getheader("Access-Control-Allow-Methods"))
            self.assertIn("PUT", opt_resp.getheader("Access-Control-Allow-Methods"))
            opt_resp.read()
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_rollcall_put_path_interception_payload(self):
        with patch.dict(os.environ, deployment_env()):
            supervisor = render_start.ProcessSupervisor([])
            server = ThreadingHTTPServer(("127.0.0.1", 0), render_start.make_handler(supervisor))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection(*server.server_address, timeout=3)
                # Test direct PUT to /api/rollcall/12345/answer_qr_rollcall with proxy json
                body = json.dumps({"data": "test_token_abc"}).encode("utf-8")
                with patch("troTHU.bot_handlers.BotHandlerBridge.qr_submit") as mock_submit:
                    async def fake_submit(*args, **kwargs):
                        return {"ok": True, "reply": "test success"}
                    mock_submit.side_effect = fake_submit

                    conn.request("PUT", "/api/rollcall/12345/answer_qr_rollcall", body=body, headers={"Content-Type": "application/json"})
                    resp = conn.getresponse()
                    raw_resp = resp.read().decode("utf-8")
                    self.assertEqual(resp.status, 200, f"Got status {resp.status} with body: {raw_resp}")
                    data = json.loads(raw_resp)
                    self.assertTrue(data["ok"])
                    self.assertIn("Access-Control-Allow-Origin", [h[0] for h in resp.getheaders()])
                    # Verify qr_submit was called with a payload containing rollcallId 12345 and data test_token_abc
                    self.assertTrue(mock_submit.called)
                    called_payload = mock_submit.call_args.kwargs.get("payload")
                    self.assertIn("12345", called_payload)
                    self.assertIn("test_token_abc", called_payload)
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_toggle_pause_account(self):
        with patch.dict(os.environ, deployment_env()):
            supervisor = render_start.ProcessSupervisor([])
            server = ThreadingHTTPServer(("127.0.0.1", 0), render_start.make_handler(supervisor))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection(*server.server_address, timeout=3)

                # 1. Pause 'primary'
                pause_body = json.dumps({"name": "primary", "paused": True}).encode("utf-8")
                conn.request("POST", "/api/account/toggle_pause", body=pause_body, headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(data["ok"])
                self.assertTrue(data["paused"])
                self.assertIn("primary", data["paused_accounts"])

                # 2. Check /api/status shows primary as paused
                conn.request("GET", "/api/status")
                st_resp = conn.getresponse()
                self.assertEqual(st_resp.status, 200)
                st_data = json.loads(st_resp.read().decode("utf-8"))
                primary_info = next((a for a in st_data.get("accounts", []) if a["name"] == "primary"), None)
                self.assertIsNotNone(primary_info)
                self.assertTrue(primary_info["paused"])
                self.assertEqual(primary_info["monitor_state"], "paused")

                # 3. Test /api/submit skips paused account
                sub_body = json.dumps({"profile": "all", "payload": "12345"}).encode("utf-8")
                with patch("troTHU.bot_handlers.BotHandlerBridge.qr_submit") as mock_submit:
                    async def fake_submit(*args, **kwargs):
                        return {"ok": True, "reply": "ok"}
                    mock_submit.side_effect = fake_submit
                    conn.request("POST", "/api/submit", body=sub_body, headers={"Content-Type": "application/json"})
                    sub_resp = conn.getresponse()
                    sub_data = json.loads(sub_resp.read().decode("utf-8"))
                    details = sub_data.get("details", [])
                    primary_detail = next((d for d in details if d["profile"] == "primary"), None)
                    self.assertIsNotNone(primary_detail)
                    self.assertEqual(primary_detail["result"]["status"], "paused")

                # 4. Unpause 'primary'
                unpause_body = json.dumps({"name": "primary", "paused": False}).encode("utf-8")
                conn.request("POST", "/api/account/toggle_pause", body=unpause_body, headers={"Content-Type": "application/json"})
                unpause_resp = conn.getresponse()
                unpause_data = json.loads(unpause_resp.read().decode("utf-8"))
                self.assertTrue(unpause_data["ok"])
                self.assertFalse(unpause_data["paused"])
                self.assertNotIn("primary", unpause_data["paused_accounts"])

                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                try:
                    render_start.PAUSED_ACCOUNTS_FILE.unlink(missing_ok=True)
                except Exception:
                    pass



