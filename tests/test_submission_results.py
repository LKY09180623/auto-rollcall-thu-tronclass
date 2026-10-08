"""Supplied-code and QR regressions against loopback HTTP only."""
import functools
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from tests import test_bot_handlers as fixtures
from troTHU import tron
from troTHU.account_runtime_store import load_runtime_state
from troTHU.bot_handlers import BotHandlerBridge
from troTHU.pending_qr import add_pending_qr, list_pending_qr


class SubmissionResultsTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BotHandlersTest.asyncSetUp
    asyncTearDown = fixtures.BotHandlersTest.asyncTearDown
    session_factory = fixtures.BotHandlersTest.session_factory
    runtime = fixtures.BotHandlersTest.runtime

    async def submit(self, payload, *, fanout=False):
        verify_once = functools.partial(tron.verify_rollcall_on_call_fine, attempts=1, delay_seconds=0)
        with (
            patch.object(tron, "get_active_http_endpoints", self.server.endpoints),
            patch.object(tron, "verify_rollcall_on_call_fine", verify_once),
            patch.object(tron, "log_print"),
            patch.object(tron, "notify_event", AsyncMock()),
        ):
            return await self.runtime().handle_text(
                "qr {}{}".format("all " if fanout else "", payload),
                adapter="discord" if fanout else "line",
                source_user_id="admin-1" if fanout else "line-user",
                channel_id="admin-channel" if fanout else "",
            )

    async def submit_guided(self, payload, expected_type, rollcall_id):
        bridge = BotHandlerBridge(tron.CONFIG, base_dir=self.temp_dir, session_factory=self.session_factory)
        with (
            patch.object(tron, "get_active_http_endpoints", self.server.endpoints),
            patch.object(tron, "log_print"),
            patch.object(tron, "notify_event", AsyncMock()),
        ):
            return await bridge.qr_submit(
                profile="default", payload=payload,
                command=SimpleNamespace(payload={"expected_type": expected_type, "expected_rollcall_id": str(rollcall_id)}),
            )

    def number_activity(self):
        self.server.rollcalls = [{"rollcall_id": 88, "is_number": True, "status": "in_progress"}]

    async def test_supplied_number_uses_number_endpoint_once_and_preserves_leading_zeroes(self):
        self.number_activity()
        self.server.rollcalls.insert(0, {"rollcall_id": 77, "is_radar": True, "status": "in_progress"})
        result = await self.submit("0001")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "ok")
        self.assertEqual(len(self.server.number_attempts), 1)
        self.assertEqual(self.server.number_attempts[0]["body"]["numberCode"], "0001")
        self.assertEqual(self.server.number_attempts[0]["rollcall_id"], "88")
        self.assertFalse(self.server.radar_answers)
        self.assertEqual(load_runtime_state(self.temp_dir).profiles["default"]["last_check"]["status"], "number_submitted")

    async def test_wrong_number_is_not_retried_or_reported_success(self):
        self.number_activity()
        result = await self.submit("9999")
        self.assertFalse(result.ok)
        self.assertEqual(result.data["status"], "wrong_code")
        self.assertEqual(len(self.server.number_attempts), 1)

    async def test_number_http_errors_have_distinct_failed_results(self):
        for http_status, status in ((401, "login_failed"), (403, "login_failed"), (429, "rate_limited"), (503, "request_failed"), (302, "login_failed")):
            with self.subTest(http_status=http_status):
                self.number_activity()
                self.server.number_attempts.clear()
                self.server.queue_response("number", status=http_status)
                if status == "login_failed":
                    self.server.queue_response("number", status=http_status)
                result = await self.submit("0001")
                self.assertFalse(result.ok)
                self.assertEqual(result.data["status"], status)
                self.assertEqual(len(self.server.number_attempts), 2 if status == "login_failed" else 1)

    async def test_number_does_not_choose_qr_or_completed_activity(self):
        self.server.rollcalls = [
            {"rollcall_id": 77, "is_qrcode": True, "status": "in_progress"},
            {"rollcall_id": 88, "is_number": True, "status": "on_call_fine"},
        ]
        result = await self.submit("0001")
        self.assertFalse(result.ok)
        self.assertEqual(result.data["status"], "no_rollcall")
        self.assertFalse(self.server.number_attempts)

    async def test_number_does_not_guess_between_two_activities(self):
        self.number_activity()
        self.server.rollcalls.append({"rollcall_id": 89, "is_number": True})
        result = await self.submit("0001")
        self.assertFalse(result.ok)
        self.assertEqual(result.data["status"], "ambiguous_rollcall")
        self.assertFalse(self.server.number_attempts)

    async def test_guided_number_targets_selected_activity(self):
        self.number_activity()
        self.server.rollcalls.append({"rollcall_id": 89, "is_number": True, "status": "in_progress"})
        result = await self.submit_guided("0001", "number", 88)
        self.assertTrue(result["ok"])
        self.assertEqual([item["rollcall_id"] for item in self.server.number_attempts], ["88"])
        self.assertEqual(self.server.number_attempts[0]["body"]["numberCode"], "0001")

    async def test_guided_number_rejects_changed_activity(self):
        self.number_activity()
        result = await self.submit_guided("0001", "number", 89)
        self.assertEqual(result["status"], "rollcall_mismatch")
        self.assertFalse(self.server.number_attempts)

    async def test_guided_qr_rejects_other_rollcall(self):
        result = await self.submit_guided('{"rollcallId":88,"data":"fixture"}', "qrcode", 89)
        self.assertEqual(result["status"], "rollcall_mismatch")
        self.assertFalse(self.server.qr_answers)

    async def test_guided_input_rejects_wrong_type(self):
        result = await self.submit_guided("0001", "qrcode", 88)
        self.assertEqual(result["status"], "invalid_payload")
        self.assertFalse(self.server.number_attempts)

    async def test_number_bad_activity_response_does_not_submit(self):
        self.server.queue_response("rollcalls", json_data={"rollcalls": None})
        result = await self.submit("0001")
        self.assertFalse(result.ok)
        self.assertEqual(result.data["status"], "request_failed")
        self.assertFalse(self.server.number_attempts)

    async def test_number_accepted_but_not_present_stays_unconfirmed(self):
        self.number_activity()
        self.server.queue_response("number", json_data={"success": True})
        result = await self.submit("0001")
        self.assertFalse(result.ok)
        self.assertEqual(result.data["status"], "submitted_unconfirmed")
        self.assertNotIn("已確認成功", result.reply)
        self.assertEqual(load_runtime_state(self.temp_dir).profiles["default"]["last_check"]["status"], "submitted_unconfirmed")

    async def test_number_verification_error_stays_unconfirmed(self):
        self.number_activity()
        with patch.object(tron, "verify_rollcall_on_call_fine", AsyncMock(side_effect=RuntimeError("fixture"))):
            result = await self.submit("0001")
        self.assertFalse(result.ok)
        self.assertEqual(result.data["status"], "submitted_unconfirmed")
        self.assertEqual(len(self.server.number_attempts), 1)

    async def test_qr_confirmed_removes_pending(self):
        add_pending_qr(self.temp_dir, profile="default", rollcall_id=88, provider="thu")
        result = await self.submit('{"rollcallId":88,"data":"fixture"}')
        self.assertTrue(result.ok)
        self.assertFalse(list_pending_qr(self.temp_dir))
        self.assertEqual(len(self.server.qr_answers), 1)

    async def test_qr_unconfirmed_keeps_pending_and_reports_false(self):
        add_pending_qr(self.temp_dir, profile="default", rollcall_id=88, provider="thu")
        self.server.queue_response("qr", json_data={"ok": True})
        result = await self.submit('{"rollcallId":88,"data":"fixture"}')
        self.assertFalse(result.ok)
        self.assertEqual(result.data["status"], "submitted_unconfirmed")
        self.assertEqual(len(list_pending_qr(self.temp_dir)), 1)
        self.assertEqual(len(self.server.qr_answers), 1)

    async def test_qr_expired_or_session_expired_is_not_success(self):
        for http_status, status in ((400, "request_failed"), (401, "login_failed"), (429, "request_failed")):
            with self.subTest(http_status=http_status):
                self.server.queue_response("qr", status=http_status, text="expired fixture")
                if status == "login_failed":
                    self.server.queue_response("qr", status=http_status, text="expired fixture")
                result = await self.submit('{"rollcallId":88,"data":"fixture"}')
                self.assertFalse(result.ok)
                self.assertEqual(result.data["status"], status)

    async def test_invalid_qr_does_not_submit(self):
        result = await self.submit('{"rollcallId":88}')
        self.assertFalse(result.ok)
        self.assertEqual(result.data["status"], "invalid_payload")
        self.assertFalse(self.server.qr_answers)

    async def test_qr_fanout_does_not_hide_an_unconfirmed_account(self):
        for profile in ("default", "alt"):
            add_pending_qr(self.temp_dir, profile=profile, rollcall_id=88, provider="thu")
        self.server.queue_response("qr", json_data={"ok": True})
        result = await self.submit('{"rollcallId":88,"data":"fixture"}', fanout=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.data["status"], "partial_failed")
        self.assertEqual([item["ok"] for item in result.data["results"]], [False, True])
        self.assertEqual([item.profile for item in list_pending_qr(self.temp_dir)], ["default"])
        self.assertEqual(len(self.server.qr_answers), 2)

    async def test_login_failure_returns_failed_command(self):
        for payload in ("0001", '{"rollcallId":88,"data":"fixture"}'):
            with self.subTest(payload_kind="number" if payload.isdigit() else "qr"):
                with patch.object(BotHandlerBridge, "ensure_login", AsyncMock(return_value=SimpleNamespace(ok=False, status="rejected"))):
                    result = await self.submit(payload)
                self.assertFalse(result.ok)
                self.assertEqual(result.data["status"], "login_failed")

    async def test_cli_qr_exit_code_tracks_confirmation_after_session_refresh(self):
        for refreshed in (False, True):
            for confirmed in (False, True):
                with self.subTest(refreshed=refreshed, confirmed=confirmed):
                    submission = AsyncMock(side_effect=[tron.UnauthorizedError("expired"), confirmed] if refreshed else [confirmed])
                    with (
                        patch.object(tron, "login", AsyncMock(return_value=SimpleNamespace(ok=True))),
                        patch.object(tron, "has_session_cookie", return_value=False),
                        patch.object(tron, "submit_qr_payload", submission),
                    ):
                        result = await tron.qr_command('{"rollcallId":88,"data":"fixture"}')
                    self.assertEqual(result, 0 if confirmed else 1)
                    self.assertEqual(submission.await_count, 2 if refreshed else 1)


class MqttSubmissionResultsTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_pending_record_still_uses_existing_direct_submission(self):
        from troTHU import mqtt_relay
        with (
            patch.object(tron, "list_profiles", return_value=[SimpleNamespace(name="default")]),
            patch.object(tron, "qr_fanout_result", AsyncMock(return_value={"ok": False, "status": "no_matches"})),
            patch.object(tron, "login", AsyncMock(return_value=SimpleNamespace(ok=True))),
            patch.object(tron, "submit_qr_payload", AsyncMock(return_value=True)) as submit,
            patch.object(tron, "log_print"),
        ):
            await mqtt_relay._execute_mqtt_qr("fixture")
        submit.assert_awaited_once()

    async def test_failed_fanout_does_not_resubmit_active_account(self):
        from troTHU import mqtt_relay
        for status in ("partial_failed", "parse_failed"):
            with (
                self.subTest(status=status),
                patch.object(tron, "qr_fanout_result", AsyncMock(return_value={"ok": False, "status": status})),
                patch.object(tron, "login", AsyncMock()) as login,
                patch.object(tron, "submit_qr_payload", AsyncMock()) as submit,
                patch.object(tron, "log_print"),
            ):
                await mqtt_relay._execute_mqtt_qr("fixture")
            login.assert_not_awaited()
            submit.assert_not_awaited()
