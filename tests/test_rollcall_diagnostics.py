import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp

from tests.fake_tron_server import FakeTronServer
from troTHU import rollcall_diagnostics as diagnostics, tron
from troTHU.environment_config import EnvironmentConfigError


class RollcallDiagnosticsHttpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await FakeTronServer().start()
        self.requests = []
        trace = aiohttp.TraceConfig()

        async def record_request(session, trace_context, params):
            self.requests.append((params.method, params.url.path))

        trace.on_request_start.append(record_request)
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True), trace_configs=[trace])
        await self.server.login_session(self.session)
        self.requests.clear()

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()
        self.assertTrue(all(method == "GET" for method, path in self.requests), self.requests)
        self.assertFalse(self.server.number_attempts)
        self.assertFalse(self.server.radar_answers)
        self.assertFalse(self.server.qr_answers)

    async def inspect(self, **kwargs):
        return await diagnostics.inspect_rollcalls(self.session, endpoints=self.server.endpoints(), user="user1", **kwargs)

    async def test_empty_valid_feed_is_not_an_error(self):
        result = await self.inspect()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "no_active_rollcall")
        self.assertEqual(result["activities"], [])
        self.assertEqual(len(self.requests), 1)

    async def test_radar_number_qr_are_reported_without_submitting(self):
        for flag, kind in (("is_number", "number"), ("is_radar", "radar"), ("is_qrcode", "qrcode")):
            with self.subTest(kind=kind):
                self.server.rollcalls = [{flag: True, "rollcall_id": 88}]
                result = await self.inspect()
                self.assertEqual(result["status"], "detected")
                self.assertEqual(result["selected"]["type"], kind)
                self.assertEqual(result["gate"]["state"], "waiting_for_rate" if kind == "radar" else "disabled")
                self.assertEqual(result["gate"]["enabled"], kind == "radar")
                self.assertTrue(result["read_only"])
                self.assertFalse(result["submission_attempted"])

    async def test_rate_gate_uses_aggregate_without_confirming_someone_elses_presence(self):
        self.server.rollcalls = [{"is_radar": True, "rollcall_id": 88}]
        self.server.student_rollcalls = [
            {"user_no": "user1", "rollcall_status": "absent"},
            {"user_no": "other", "rollcall_status": "on_call_fine"},
        ]
        result = await self.inspect()
        self.assertEqual(result["gate"]["state"], "ready")
        self.assertEqual(result["gate"]["present_rate_percent"], 50.0)
        self.assertFalse(result["gate"]["personal_confirmed"])

    async def test_personal_confirmation_is_reported(self):
        self.server.rollcalls = [{"is_number": True, "rollcall_id": 88}]
        self.server.student_rollcalls = [{"user_no": "user1", "rollcall_status": "on_call_fine"}]
        result = await self.inspect()
        self.assertEqual(result["gate"]["state"], "already_confirmed")
        self.assertTrue(result["gate"]["personal_confirmed"])

    async def test_already_confirmed_feed_needs_no_roster_read(self):
        self.server.rollcalls = [{"is_number": True, "rollcall_id": 88, "status": "on_call_fine"}]
        result = await self.inspect()
        self.assertEqual(result["status"], "no_active_rollcall")
        self.assertEqual(result["gate"]["state"], "already_confirmed")
        self.assertEqual(len(self.requests), 1)

    async def test_api_errors_are_distinct_and_not_retried(self):
        for code, expected in ((401, "login_expired"), (403, "forbidden"), (429, "rate_limited"), (503, "service_unavailable"), (404, "http_error")):
            with self.subTest(code=code):
                self.requests.clear()
                self.server.queue_response("rollcalls", status=code, text="secret response")
                result = await self.inspect()
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["http_status"], code)
                self.assertNotIn("secret response", json.dumps(result))
                self.assertEqual(len(self.requests), 1)

    async def test_redirect_is_not_followed(self):
        self.server.queue_response("rollcalls", status=302, headers={"Location": "https://example.invalid/login"})
        result = await self.inspect()
        self.assertEqual(result["status"], "login_expired")
        self.assertEqual(len(self.requests), 1)

    async def test_bad_json_or_envelope_is_not_an_empty_activity_list(self):
        for payload in ([], {}, {"rollcalls": None}, {"rollcalls": [None]}):
            with self.subTest(payload=payload):
                self.server.queue_response("rollcalls", json_data=payload)
                result = await self.inspect()
                self.assertEqual(result["status"], "invalid_response")
        self.server.queue_response("rollcalls", text="<html>login</html>")
        self.assertEqual((await self.inspect())["status"], "invalid_response")

    async def test_unreadable_roster_explains_why_monitor_waits(self):
        self.server.rollcalls = [{"is_radar": True, "rollcall_id": 88}]
        self.server.queue_response("student_rollcalls", status=403)
        result = await self.inspect()
        self.assertEqual(result["status"], "detected")
        self.assertEqual(result["gate"]["state"], "rate_unavailable")
        self.assertEqual(result["gate"]["progress_status"], "forbidden")

    async def test_qr_does_not_wait_when_roster_is_unavailable(self):
        self.server.rollcalls = [{"is_qrcode": True, "rollcall_id": 88}]
        self.server.queue_response("student_rollcalls", status=403)
        result = await self.inspect()
        self.assertEqual(result["gate"]["state"], "disabled")
        self.assertFalse(result["gate"]["enabled"])
        self.assertEqual(result["gate"]["progress_status"], "forbidden")

    async def test_number_does_not_wait_when_roster_is_unavailable(self):
        self.server.rollcalls = [{"is_number": True, "rollcall_id": 88}]
        self.server.queue_response("student_rollcalls", status=403)
        result = await self.inspect()
        self.assertEqual(result["gate"]["state"], "disabled")
        self.assertFalse(result["gate"]["enabled"])
        self.assertEqual(result["gate"]["progress_status"], "forbidden")

    async def test_bad_roster_shape_does_not_claim_read_succeeded(self):
        self.server.rollcalls = [{"is_radar": True, "rollcall_id": 88}]
        self.server.queue_response("student_rollcalls", json_data={"error": "fixture"})
        result = await self.inspect()
        self.assertEqual(result["gate"]["state"], "rate_unavailable")
        self.assertEqual(result["gate"]["progress_status"], "invalid_response")

    async def test_existing_disabled_gate_setting_is_only_reported(self):
        self.server.rollcalls = [{"is_number": True, "rollcall_id": 88}]
        self.server.queue_response("student_rollcalls", status=403)
        result = await self.inspect(ignore_gate=True)
        self.assertEqual(result["gate"]["state"], "disabled")
        self.assertFalse(result["gate"]["enabled"])

    async def test_report_excludes_codes_tokens_and_roster_names(self):
        secret = "fixture-secret-value"
        self.server.rollcalls = [{"is_number": True, "rollcall_id": 88, "numberCode": secret, "token": secret, "title": secret}]
        self.server.student_rollcalls = [{"user_no": secret, "name": secret, "rollcall_status": "absent"}]
        result = await self.inspect()
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn("number_code", json.dumps(result))

    async def test_unknown_activity_is_visible_but_not_dispatched(self):
        self.server.rollcalls = [{"type": "unknown-type", "rollcall_id": 88}]
        result = await self.inspect()
        self.assertEqual(result["status"], "detected")
        self.assertEqual(result["gate"]["state"], "unrecognized")
        self.assertEqual(len(self.requests), 1)

    async def test_network_timeout_has_clear_result(self):
        with patch.object(self.session, "get", side_effect=TimeoutError):
            result = await self.inspect()
        self.assertEqual(result["status"], "network_error")


class RollcallDiagnosticsCliTest(unittest.TestCase):
    def test_cli_dispatch_skips_bootstrap_and_monitor(self):
        with (
            patch.object(tron, "bootstrap_config") as bootstrap,
            patch.object(tron, "run_monitor_forever") as monitor,
            patch.object(diagnostics, "rollcall_status_command", return_value=0) as command,
        ):
            self.assertEqual(tron.main(["rollcall-status", "--profile", "demo", "--json"]), 0)
        command.assert_called_once_with(profile="demo", json_output=True)
        bootstrap.assert_not_called()
        monitor.assert_not_called()

    def test_config_is_loaded_read_only_and_restored_after_query(self):
        original = copy.deepcopy(tron.CONFIG)
        config = tron.normalize_config({"account": {"user": "fixture", "passwd": "secret"}})
        with (
            patch.object(tron, "load_config", return_value=config) as loader,
            patch.object(diagnostics, "_inspect_cached_session", AsyncMock(return_value=diagnostics._result("detected"))),
            patch("builtins.print") as printer,
        ):
            self.assertEqual(diagnostics.rollcall_status_command(json_output=True), 0)
        loader.assert_called_once_with(read_only=True)
        self.assertEqual(tron.CONFIG, original)
        self.assertNotIn("secret", printer.call_args.args[0])

    def test_invalid_profile_never_queries(self):
        with (
            patch.object(tron, "load_config", return_value=tron.normalize_config({})),
            patch.object(diagnostics, "_inspect_cached_session", AsyncMock()) as query,
            patch("builtins.print") as printer,
        ):
            self.assertEqual(diagnostics.rollcall_status_command(profile="missing", json_output=True), 1)
        query.assert_not_awaited()
        self.assertEqual(json.loads(printer.call_args.args[0])["status"], "profile_not_found")

    def test_config_error_is_redacted_and_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "missing.yaml"
            with (
                patch.object(tron, "CONFIG_PATH", path),
                patch.object(tron, "CONFIG_ADVANCED_PATH", Path(name) / "advanced.yaml"),
                patch("troTHU.config_runtime.read_environment_accounts", return_value=[]),
                patch("builtins.print") as printer,
            ):
                self.assertEqual(diagnostics.rollcall_status_command(json_output=True), 1)
            self.assertEqual(list(Path(name).iterdir()), [])
        self.assertEqual(json.loads(printer.call_args.args[0])["status"], "config_error")

    def test_missing_cookie_does_not_login_or_send_requests(self):
        import asyncio
        with (
            patch.object(tron, "provider_is_daily_allowed", return_value=True),
            patch.object(tron, "load_session_cookies", return_value=False),
            patch.object(tron, "login", AsyncMock()) as login,
            patch.object(diagnostics, "inspect_rollcalls", AsyncMock()) as query,
        ):
            result = asyncio.run(diagnostics._inspect_cached_session())
        self.assertEqual(result["status"], "cookie_missing")
        login.assert_not_awaited()
        query.assert_not_awaited()
