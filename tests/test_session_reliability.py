import json
import tempfile
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from yarl import URL

from tests import test_bot_handlers as fixtures
from troTHU import tron
from troTHU.account_store import cookie_path, load_session_cookies, save_session_cookies


class CookieCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_keeps_sso_domains_paths_and_secure_flags_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            async with aiohttp.ClientSession() as source:
                for domain, path, value in (
                    ("school.example", "/", "site-session"),
                    ("school.example", "/api", "api-session"),
                    ("identity.example", "/", "sso-session"),
                ):
                    cookie = SimpleCookie()
                    cookie["session"] = value
                    cookie["session"]["domain"] = domain
                    cookie["session"]["path"] = path
                    cookie["session"]["secure"] = True
                    cookie["session"]["httponly"] = True
                    source.cookie_jar.update_cookies(cookie, response_url=URL("https://" + domain + path))
                save_session_cookies(source, base, "default")

            async with aiohttp.ClientSession() as restored:
                self.assertTrue(load_session_cookies(restored, base, "default"))
                for url, value in (
                    ("https://school.example/", "site-session"),
                    ("https://school.example/api/rollcalls", "api-session"),
                    ("https://identity.example/", "sso-session"),
                ):
                    self.assertEqual(restored.cookie_jar.filter_cookies(URL(url))["session"].value, value)
                self.assertFalse(restored.cookie_jar.filter_cookies(URL("http://school.example/")))
                self.assertFalse(restored.cookie_jar.filter_cookies(URL("https://unrelated.example/")))
                self.assertTrue(all(cookie["httponly"] for cookie in restored.cookie_jar))

    async def test_old_cache_shape_and_domainless_manual_export_still_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = cookie_path(Path(directory), "default")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps([{"key": "session", "value": "legacy", "domain": "", "path": "/"}]), encoding="utf-8")
            async with aiohttp.ClientSession() as session:
                self.assertTrue(load_session_cookies(session, Path(directory), "default"))
                self.assertEqual(session.cookie_jar.filter_cookies(URL("https://school.example/"))["session"].value, "legacy")

    async def test_expired_persistent_cookie_is_not_restored_as_valid_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = cookie_path(Path(directory), "default")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps([{
                "key": "session", "value": "expired", "domain": "school.example", "path": "/",
                "expires": "Thu, 01 Jan 1970 00:00:01 GMT",
            }]), encoding="utf-8")
            async with aiohttp.ClientSession() as session:
                load_session_cookies(session, Path(directory), "default")
                self.assertFalse(session.cookie_jar.filter_cookies(URL("https://school.example/")))

    async def test_invalid_rows_do_not_prevent_valid_cookie_from_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = cookie_path(Path(directory), "default")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps([
                None, {"key": "invalid key", "value": "ignored"},
                {"key": "session", "value": "valid", "domain": "school.example", "path": "/"},
            ]), encoding="utf-8")
            async with aiohttp.ClientSession() as session:
                self.assertTrue(load_session_cookies(session, Path(directory), "default"))
                self.assertEqual(session.cookie_jar.filter_cookies(URL("https://school.example/"))["session"].value, "valid")


class SubmissionSessionRecoveryTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BotHandlersTest.asyncSetUp
    asyncTearDown = fixtures.BotHandlersTest.asyncTearDown
    session_factory = fixtures.BotHandlersTest.session_factory
    runtime = fixtures.BotHandlersTest.runtime

    def stale_cache(self):
        path = cookie_path(self.temp_dir, "default")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([{"key": "session", "value": "expired-session", "domain": "", "path": "/"}]), encoding="utf-8")

    async def submit(self, payload):
        with (
            patch.object(tron, "get_active_http_endpoints", self.server.endpoints),
            patch.object(tron, "notify_event", AsyncMock()),
            patch.object(tron, "log_print"),
        ):
            return await self.runtime().handle_text("qr " + payload, adapter="line", source_user_id="line-user")

    async def test_stale_number_session_refreshes_and_submits_same_code_once(self):
        self.stale_cache()
        self.server.rollcalls = [{"is_number": True, "rollcall_id": 88}]
        with patch.object(tron, "login", AsyncMock(wraps=tron.login)) as login:
            result = await self.submit("0001")
        self.assertTrue(result.ok)
        login.assert_awaited_once()
        self.assertEqual(len(self.server.number_attempts), 1)
        self.assertEqual(self.server.number_attempts[0]["body"]["numberCode"], "0001")

    async def test_stale_qr_session_refreshes_and_finishes_without_manual_reauth(self):
        self.stale_cache()
        with patch.object(tron, "login", AsyncMock(wraps=tron.login)) as login:
            result = await self.submit('{"rollcallId":88,"data":"fixture"}')
        self.assertTrue(result.ok)
        login.assert_awaited_once()
        self.assertEqual(len(self.server.qr_answers), 1)

    async def test_session_refresh_failure_stops_before_submission(self):
        self.stale_cache()
        self.server.queue_response("submit_login", status=200, text="no session")
        with patch.object(tron, "login", AsyncMock(wraps=tron.login)) as login:
            result = await self.submit("0001")
        self.assertFalse(result.ok)
        self.assertEqual(result.data["status"], "login_failed")
        login.assert_awaited_once()
        self.assertFalse(self.server.number_attempts)

    async def test_valid_cookie_is_reused_without_password_login(self):
        async with self.session_factory() as session:
            await self.server.login_session(session)
            save_session_cookies(session, self.temp_dir, "default")
        self.server.rollcalls = [{"is_number": True, "rollcall_id": 88}]
        with patch.object(tron, "login", AsyncMock(wraps=tron.login)) as login:
            result = await self.submit("0001")
        self.assertTrue(result.ok)
        login.assert_not_awaited()

    async def test_unconfirmed_submission_is_not_replayed_after_verification_failure(self):
        self.server.rollcalls = [{"is_number": True, "rollcall_id": 88}]
        with (
            patch.object(tron, "login", AsyncMock(wraps=tron.login)) as login,
            patch.object(tron, "verify_rollcall_on_call_fine", AsyncMock(side_effect=tron.UnauthorizedError("expired"))),
        ):
            result = await self.submit("0001")
        self.assertFalse(result.ok)
        self.assertEqual(result.data["status"], "submitted_unconfirmed")
        self.assertEqual(len(self.server.number_attempts), 1)
        login.assert_awaited_once()

    async def test_unchanged_unauthorized_feed_does_not_loop_login(self):
        self.stale_cache()
        self.server.queue_response("rollcalls", status=401)
        with patch.object(tron, "login", AsyncMock(wraps=tron.login)) as login:
            result = await self.submit("0001")
        self.assertFalse(result.ok)
        self.assertEqual(result.data["status"], "login_failed")
        login.assert_awaited_once()
        self.assertFalse(self.server.number_attempts)

