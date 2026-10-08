import copy
import unittest
import unittest.mock

import aiohttp
from yarl import URL

from troTHU import tron
from troTHU.providers import (
    DEFAULT_PROVIDER,
    get_provider,
    list_all_providers,
    list_supported_providers,
    normalize_provider_config,
    provider_support_report,
    provider_registry_config,
    tronclass_api_endpoints,
)
from troTHU.research_mode import normalize_research_mode_config
from tests.fake_tron_server import FakeTronServer


class ProviderConfigTest(unittest.TestCase):
    def test_thu_provider_is_ready_and_matches_legacy_urls(self) -> None:
        provider = get_provider("thu")

        self.assertTrue(provider.ready)
        self.assertEqual(provider.base_url, "https://ilearn.thu.edu.tw")
        self.assertIn("/api/radar/rollcalls", provider.rollcalls_url)
        self.assertTrue(provider.capabilities.number)
        self.assertTrue(provider.capabilities.radar)
        self.assertTrue(provider.capabilities.course_discovery)
        self.assertTrue(provider.capabilities.manual_qr)
        self.assertIn("/api/current-semester-info", provider.current_semester_url)
        self.assertIn("/api/my-courses", provider.courses_url)

    def test_aliases_and_unknown_provider_fall_back_to_thu(self) -> None:
        self.assertEqual(get_provider("Tunghai").key, DEFAULT_PROVIDER)
        self.assertEqual(get_provider("www.tronclass.com.tw").key, "tronclass")
        self.assertEqual(get_provider("not-a-provider").key, DEFAULT_PROVIDER)

    def test_registry_keeps_fju_hidden_and_tku_tronclass_visible(self) -> None:
        registry = provider_registry_config()

        self.assertEqual(registry["current"], "thu")
        self.assertFalse(registry["allow_experimental"])
        self.assertTrue(registry["available"]["thu"]["ready"])
        self.assertTrue(registry["available"]["fju"]["ready"])
        self.assertFalse(registry["available"]["fju"]["user_visible"])
        self.assertTrue(registry["available"]["tku"]["ready"])
        self.assertTrue(registry["available"]["tku"]["user_visible"])
        self.assertTrue(registry["available"]["tronclass"]["ready"])
        self.assertTrue(registry["available"]["tronclass"]["user_visible"])
        self.assertEqual(registry["available"]["fju"]["support_level"], "ready")
        self.assertEqual(registry["available"]["fju"]["auth_flow"], "manual_cookie_only")
        self.assertTrue(registry["available"]["fju"]["capabilities"]["radar"])
        self.assertEqual(registry["available"]["tku"]["support_level"], "ready")
        self.assertEqual(registry["available"]["tku"]["base_url"], "https://iclass.tku.edu.tw")
        self.assertEqual(registry["available"]["tku"]["auth_flow"], "tku_sso_browser")
        self.assertTrue(registry["available"]["tku"]["capabilities"]["radar"])
        self.assertEqual(registry["available"]["tronclass"]["base_url"], "https://www.tronclass.com.tw")
        self.assertEqual(registry["available"]["tronclass"]["auth_flow"], "public_cloud_email")
        self.assertTrue(registry["available"]["tronclass"]["capabilities"]["course_discovery"])
        self.assertTrue(registry["available"]["usc"]["ready"])
        self.assertTrue(registry["available"]["usc"]["user_visible"])
        self.assertEqual(registry["available"]["usc"]["base_url"], "https://tronclass.usc.edu.tw")
        self.assertEqual(registry["available"]["usc"]["auth_flow"], "usc_keycloak")
        self.assertTrue(registry["available"]["usc"]["capabilities"]["radar"])
        self.assertTrue(registry["available"]["usc"]["capabilities"]["course_discovery"])

    def test_supported_provider_registry_hides_fju_by_default(self) -> None:
        self.assertEqual(
            [provider.key for provider in list_supported_providers()],
            ["thu", "tku", "tronclass", "usc"],
        )
        self.assertEqual(
            [provider.key for provider in list_supported_providers(include_hidden=True)],
            ["fju", "thu", "tku", "tronclass", "usc"],
        )
        self.assertEqual([provider.key for provider in list_all_providers()], ["fju", "thu", "tku", "tronclass", "usc"])

    def test_tronclass_api_endpoint_builder_is_shared(self) -> None:
        endpoints = tronclass_api_endpoints("https://school.example/")

        self.assertEqual(
            endpoints["rollcalls_url"],
            "https://school.example/api/radar/rollcalls?api_version=1.1.0",
        )
        self.assertEqual(endpoints["current_semester_url"], "https://school.example/api/current-semester-info")
        self.assertEqual(endpoints["courses_url"], "https://school.example/api/my-courses?page=1&page_size=50")

    def test_normalize_provider_config_preserves_known_overrides(self) -> None:
        normalized = normalize_provider_config(
            {
                "current": "fju",
                "allow_experimental": True,
                "available": {
                    "fju": {
                        "base_url": "https://example.edu",
                        "current_semester_url": "https://example.edu/api/current-semester-info",
                        "notes": "lab only",
                    }
                },
            }
        )

        self.assertEqual(normalized["current"], "fju")
        self.assertTrue(normalized["allow_experimental"])
        self.assertEqual(normalized["available"]["fju"]["base_url"], "https://example.edu")
        self.assertEqual(
            normalized["available"]["fju"]["current_semester_url"],
            "https://example.edu/api/current-semester-info",
        )
        self.assertEqual(
            normalized["available"]["fju"]["rollcalls_url"],
            "https://example.edu/api/radar/rollcalls?api_version=1.1.0",
        )
        self.assertEqual(normalized["available"]["fju"]["notes"], "lab only")

    def test_unknown_provider_falls_back_with_warning_metadata(self) -> None:
        normalized = normalize_provider_config({"current": "nfu"})

        self.assertEqual(normalized["current"], DEFAULT_PROVIDER)
        self.assertEqual(normalized["requested"], "nfu")
        self.assertEqual(normalized["fallback_reason"], "unknown_provider")

    def test_provider_support_report_marks_fju_tku_tronclass_daily_ready_without_experimental_gate(self) -> None:
        fju = get_provider("fju")
        blocked = provider_support_report(fju)
        allowed = provider_support_report(fju, allow_experimental=True)
        tronclass = provider_support_report(get_provider("tronclass"))

        self.assertEqual(blocked["support_level"], "ready")
        self.assertTrue(blocked["daily_ready"])
        self.assertFalse(blocked["user_visible"])
        self.assertTrue(blocked["capabilities"]["radar"])
        self.assertTrue(allowed["daily_ready"])
        self.assertTrue(allowed["endpoint_configured"]["base_url"])
        self.assertTrue(tronclass["daily_ready"])
        self.assertTrue(tronclass["user_visible"])

    def test_tron_normalize_config_adds_provider_and_research_defaults(self) -> None:
        normalized = tron.normalize_config({"config": {"user-agent": []}})

        self.assertEqual(normalized["provider"]["current"], "thu")
        self.assertFalse(normalized["provider"]["allow_experimental"])
        self.assertIn("thu", normalized["provider"]["available"])
        self.assertFalse(normalized["research"]["enabled"])
        self.assertTrue(normalized["research"]["redact_sensitive"])


class ResearchModeConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_config = copy.deepcopy(tron.CONFIG)

    def tearDown(self) -> None:
        tron.CONFIG.clear()
        tron.CONFIG.update(copy.deepcopy(self.original_config))

    def test_research_flags_are_gated_by_enabled(self) -> None:
        normalized = normalize_research_mode_config(
            {
                "enabled": False,
                "allow_api_exploration": True,
                "allow_browser_capture": True,
                "log_raw_payloads": True,
            }
        )

        self.assertFalse(normalized["enabled"])
        self.assertFalse(normalized["allow_api_exploration"])
        self.assertFalse(normalized["allow_browser_capture"])
        self.assertFalse(normalized["log_raw_payloads"])

    def test_status_report_exposes_provider_and_research_boundary(self) -> None:
        tron.CONFIG.update(
            tron.normalize_config(
                {
                    "account": {"user": "s1", "passwd": ""},
                    "research": {"enabled": True, "allow_api_exploration": True},
                }
            )
        )

        report = tron.status_report()

        self.assertEqual(report["provider"]["key"], "thu")
        self.assertIn("provider_support", report)
        self.assertTrue(report["provider_support"]["daily_ready"])
        self.assertTrue(report["course_discovery"]["enabled"])
        self.assertTrue(report["research"]["enabled"])
        self.assertTrue(report["research"]["allow_api_exploration"])

    def test_doctor_report_marks_fju_daily_ready(self) -> None:
        tron.CONFIG.update(
            tron.normalize_config(
                {
                    "account": {"user": "s1", "passwd": ""},
                    "provider": {"current": "fju"},
                }
            )
        )

        report = tron.doctor_report()

        self.assertIn(report["status"], {"warn", "fail"})
        self.assertEqual(report["provider"]["key"], "fju")
        self.assertEqual(report["provider_support"]["support_level"], "ready")
        self.assertTrue(report["provider_support"]["daily_ready"])
        provider_checks = [item for item in report["checks"] if item["name"].startswith("provider")]
        self.assertTrue(all(item["status"] == "ok" for item in provider_checks))

    def _configure_provider_for_fake_server(self, provider_key: str, server: FakeTronServer) -> None:
        tron.CONFIG.clear()
        tron.CONFIG.update(
            tron.normalize_config(
                {
                    "account": {"user": "user1", "passwd": "pass1"},
                    "accounts": {
                        "current": "default",
                        "profiles": {
                            "default": {"user": "user1", "passwd": "pass1", "label": ""}
                        },
                    },
                    "provider": {
                        "current": provider_key,
                        "available": {
                            provider_key: {
                                "base_url": server.base_url,
                                "login_url": server.login_url,
                                "rollcalls_url": server.rollcalls_url,
                                "current_semester_url": server.current_semester_url,
                                "courses_url": server.courses_url,
                            }
                        },
                    },
                }
            )
        )

    def _seed_cookie_for_manual_provider(self, provider_key: str, server: FakeTronServer, session) -> None:
        if provider_key == "fju":
            session.cookie_jar.update_cookies(
                {"session": server.session_cookie},
                response_url=URL(server.base_url),
            )

    async def _discover_courses_with_provider(self, provider_key: str) -> dict:
        original_config = copy.deepcopy(tron.CONFIG)
        async with FakeTronServer() as server:
            server.courses = [{"id": 1, "name": "Synthetic Course"}]
            try:
                self._configure_provider_for_fake_server(provider_key, server)
                async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
                    self._seed_cookie_for_manual_provider(provider_key, server, session)
                    login_result = await tron.login(session)
                    self.assertTrue(login_result.ok)
                    client = tron.create_tron_http_client(session)
                    result = await tron.discover_courses(
                        session,
                        endpoints=client.endpoints,
                        request_ssl=tron.get_ssl_request_setting(),
                    )
                    return result.to_dict()
            finally:
                tron.CONFIG.clear()
                tron.CONFIG.update(original_config)

    async def _number_rollcall_with_provider(self, provider_key: str) -> dict:
        original_config = copy.deepcopy(tron.CONFIG)
        original_completed = dict(tron.COMPLETED_NUMBER_ROLLCALLS)
        async with FakeTronServer(correct_number_code="0000") as server:
            server.rollcalls = [{"rollcall_id": 42, "is_number": True, "status": "started"}]
            try:
                tron.COMPLETED_NUMBER_ROLLCALLS.clear()
                self._configure_provider_for_fake_server(provider_key, server)
                async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
                    self._seed_cookie_for_manual_provider(provider_key, server, session)
                    login_result = await tron.login(session)
                    self.assertTrue(login_result.ok)
                    with (
                        unittest.mock.patch.object(tron, "NUMBER_CODE_LIMIT", 1),
                        unittest.mock.patch.object(tron, "NUMBER_WORKER_COUNT", 1),
                        unittest.mock.patch.object(tron, "mes", unittest.mock.AsyncMock()),
                        unittest.mock.patch.object(tron, "log_print"),
                        unittest.mock.patch.object(tron, "status_print"),
                    ):
                        status = await tron.check_rollcall(session, 1)
                        await tron.number(session, 42)
                return {"status": status, "attempts": server.number_attempts}
            finally:
                tron.CONFIG.clear()
                tron.CONFIG.update(original_config)
                tron.COMPLETED_NUMBER_ROLLCALLS.clear()
                tron.COMPLETED_NUMBER_ROLLCALLS.update(original_completed)

    async def _qr_submit_with_provider(self, provider_key: str) -> dict:
        original_config = copy.deepcopy(tron.CONFIG)
        async with FakeTronServer() as server:
            try:
                self._configure_provider_for_fake_server(provider_key, server)
                async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
                    self._seed_cookie_for_manual_provider(provider_key, server, session)
                    login_result = await tron.login(session)
                    self.assertTrue(login_result.ok)
                    with (
                        unittest.mock.patch.object(tron, "mes", unittest.mock.AsyncMock()),
                        unittest.mock.patch.object(tron, "log_print"),
                        unittest.mock.patch.object(tron, "notify_event", unittest.mock.AsyncMock()),
                    ):
                        ok = await tron.submit_qr_payload(
                            session,
                            '{"rollcallId":77,"data":"synthetic-qr-data"}',
                        )
                return {"ok": ok, "answers": server.qr_answers}
            finally:
                tron.CONFIG.clear()
                tron.CONFIG.update(original_config)

    def test_fju_provider_endpoints_can_target_fake_server(self) -> None:
        result = __import__("asyncio").run(self._discover_courses_with_provider("fju"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["course_count"], 1)

    def test_tku_provider_endpoints_can_target_fake_server(self) -> None:
        result = __import__("asyncio").run(self._discover_courses_with_provider("tku"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["course_count"], 1)

    def test_tronclass_provider_endpoints_can_target_fake_server(self) -> None:
        result = __import__("asyncio").run(self._discover_courses_with_provider("tronclass"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["course_count"], 1)

    def test_fju_tku_tronclass_number_rollcall_uses_provider_base_url(self) -> None:
        for provider in ("fju", "tku", "tronclass"):
            with self.subTest(provider=provider):
                result = __import__("asyncio").run(self._number_rollcall_with_provider(provider))

                self.assertEqual(result["status"], "is_number")
                self.assertEqual(result["attempts"][0]["rollcall_id"], "42")
                self.assertEqual(result["attempts"][0]["body"]["numberCode"], "0000")

    def test_fju_tku_tronclass_qr_submit_uses_provider_base_url(self) -> None:
        for provider in ("fju", "tku", "tronclass"):
            with self.subTest(provider=provider):
                result = __import__("asyncio").run(self._qr_submit_with_provider(provider))

                self.assertTrue(result["ok"])
                self.assertEqual(result["answers"][0]["rollcall_id"], "77")
                self.assertEqual(result["answers"][0]["body"]["data"], "synthetic-qr-data")


class UscProviderTest(unittest.TestCase):
    """Tests for the Shih Chien University (USC) provider."""

    def test_usc_provider_is_ready_with_correct_fields(self) -> None:
        provider = get_provider("usc")

        self.assertEqual(provider.key, "usc")
        self.assertEqual(provider.base_url, "https://tronclass.usc.edu.tw")
        self.assertEqual(provider.login_url, "https://tronclass.usc.edu.tw/login")
        self.assertEqual(provider.auth_flow, "usc_keycloak")
        self.assertTrue(provider.ready)
        self.assertTrue(provider.user_visible)
        self.assertTrue(provider.capabilities.number)
        self.assertTrue(provider.capabilities.radar)
        self.assertTrue(provider.capabilities.qrcode)
        self.assertTrue(provider.capabilities.course_discovery)
        self.assertTrue(provider.capabilities.teacher_rollcall)
        self.assertTrue(provider.capabilities.manual_qr)
        self.assertTrue(provider.capabilities.local_scanner)
        self.assertTrue(provider.capabilities.direct_code_lookup)
        self.assertIn("/api/radar/rollcalls", provider.rollcalls_url)
        self.assertIn("/api/current-semester-info", provider.current_semester_url)
        self.assertIn("/api/my-courses", provider.courses_url)

    def test_usc_aliases_resolve_correctly(self) -> None:
        for alias in ("usc", "實踐", "實踐大學", "shihchien", "usc.edu.tw",
                      "tronclass.usc", "tronclass.usc.edu.tw"):
            with self.subTest(alias=alias):
                self.assertEqual(get_provider(alias).key, "usc",
                                 msg="alias {!r} did not resolve to 'usc'".format(alias))

    def test_usc_appears_in_user_visible_providers(self) -> None:
        visible_keys = [p.key for p in list_supported_providers()]
        self.assertIn("usc", visible_keys)

    def test_usc_provider_support_report_is_daily_ready(self) -> None:
        report = provider_support_report(get_provider("usc"))

        self.assertEqual(report["support_level"], "ready")
        self.assertTrue(report["daily_ready"])
        self.assertTrue(report["user_visible"])
        self.assertTrue(report["capabilities"]["radar"])
        self.assertTrue(report["endpoint_configured"]["base_url"])
        self.assertTrue(report["endpoint_configured"]["login_url"])

    def test_usc_api_endpoints_use_correct_base_url(self) -> None:
        provider = get_provider("usc")
        base = "https://tronclass.usc.edu.tw"

        self.assertEqual(
            provider.rollcalls_url,
            "{}/api/radar/rollcalls?api_version=1.1.0".format(base),
        )
        self.assertEqual(
            provider.current_semester_url,
            "{}/api/current-semester-info".format(base),
        )
        self.assertEqual(
            provider.courses_url,
            "{}/api/my-courses?page=1&page_size=50".format(base),
        )

    def test_usc_provider_endpoints_can_target_fake_server(self) -> None:
        """Full integration path: USC provider -> fake server -> course discovery."""
        import asyncio

        async def run() -> dict:
            original_config = copy.deepcopy(tron.CONFIG)
            async with FakeTronServer() as server:
                server.courses = [{"id": 10, "name": "USC Synthetic Course"}]
                try:
                    tron.CONFIG.clear()
                    tron.CONFIG.update(
                        tron.normalize_config(
                            {
                                "account": {"user": "user1", "passwd": "pass1"},
                                "accounts": {
                                    "current": "default",
                                    "profiles": {
                                        "default": {"user": "user1", "passwd": "pass1", "label": "USC"}
                                    },
                                },
                                "provider": {
                                    "current": "usc",
                                    "available": {
                                        "usc": {
                                            "base_url": server.base_url,
                                            "login_url": server.login_url,
                                            "rollcalls_url": server.rollcalls_url,
                                            "current_semester_url": server.current_semester_url,
                                            "courses_url": server.courses_url,
                                        }
                                    },
                                },
                            }
                        )
                    )
                    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
                        login_result = await tron.login(session)
                        self.assertTrue(login_result.ok, msg="Login failed: {}".format(login_result.status))
                        client = tron.create_tron_http_client(session)
                        result = await tron.discover_courses(
                            session,
                            endpoints=client.endpoints,
                            request_ssl=tron.get_ssl_request_setting(),
                        )
                        return result.to_dict()
                finally:
                    tron.CONFIG.clear()
                    tron.CONFIG.update(original_config)

        result = asyncio.run(run())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["course_count"], 1)

    def test_usc_number_rollcall_uses_correct_base_url(self) -> None:
        import asyncio

        async def run() -> dict:
            original_config = copy.deepcopy(tron.CONFIG)
            original_completed = dict(tron.COMPLETED_NUMBER_ROLLCALLS)
            async with FakeTronServer(correct_number_code="0000") as server:
                server.rollcalls = [{"rollcall_id": 55, "is_number": True, "status": "started"}]
                try:
                    tron.COMPLETED_NUMBER_ROLLCALLS.clear()
                    tron.CONFIG.clear()
                    tron.CONFIG.update(
                        tron.normalize_config(
                            {
                                "account": {"user": "user1", "passwd": "pass1"},
                                "accounts": {
                                    "current": "default",
                                    "profiles": {
                                        "default": {"user": "user1", "passwd": "pass1", "label": "USC"}
                                    },
                                },
                                "provider": {
                                    "current": "usc",
                                    "available": {
                                        "usc": {
                                            "base_url": server.base_url,
                                            "login_url": server.login_url,
                                            "rollcalls_url": server.rollcalls_url,
                                            "current_semester_url": server.current_semester_url,
                                            "courses_url": server.courses_url,
                                        }
                                    },
                                },
                            }
                        )
                    )
                    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
                        login_result = await tron.login(session)
                        self.assertTrue(login_result.ok)
                        with (
                            unittest.mock.patch.object(tron, "NUMBER_CODE_LIMIT", 1),
                            unittest.mock.patch.object(tron, "NUMBER_WORKER_COUNT", 1),
                            unittest.mock.patch.object(tron, "mes", unittest.mock.AsyncMock()),
                            unittest.mock.patch.object(tron, "log_print"),
                            unittest.mock.patch.object(tron, "status_print"),
                        ):
                            status = await tron.check_rollcall(session, 1)
                            await tron.number(session, 55)
                    return {"status": status, "attempts": server.number_attempts}
                finally:
                    tron.CONFIG.clear()
                    tron.CONFIG.update(original_config)
                    tron.COMPLETED_NUMBER_ROLLCALLS.clear()
                    tron.COMPLETED_NUMBER_ROLLCALLS.update(original_completed)

        result = asyncio.run(run())
        self.assertEqual(result["status"], "is_number")
        self.assertEqual(result["attempts"][0]["rollcall_id"], "55")
        self.assertEqual(result["attempts"][0]["body"]["numberCode"], "0000")

    def _usc_config_for_server(self, server) -> dict:
        """Return normalized config dict pointing at FakeTronServer as USC provider."""
        return tron.normalize_config(
            {
                "account": {"user": "user1", "passwd": "pass1"},
                "accounts": {
                    "current": "default",
                    "profiles": {
                        "default": {"user": "user1", "passwd": "pass1", "label": "USC"}
                    },
                },
                "provider": {
                    "current": "usc",
                    "available": {
                        "usc": {
                            "base_url": server.base_url,
                            "login_url": server.login_url,
                            "rollcalls_url": server.rollcalls_url,
                            "current_semester_url": server.current_semester_url,
                            "courses_url": server.courses_url,
                        }
                    },
                },
            }
        )

    def test_usc_qr_submit_uses_correct_base_url(self) -> None:
        """USC QR: POST raw QR payload string directly—no camera scan needed."""
        import asyncio

        async def run() -> dict:
            original_config = copy.deepcopy(tron.CONFIG)
            async with FakeTronServer() as server:
                try:
                    tron.CONFIG.clear()
                    tron.CONFIG.update(self._usc_config_for_server(server))
                    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
                        login_result = await tron.login(session)
                        self.assertTrue(login_result.ok, msg="Login failed: {}".format(login_result.status))
                        with (
                            unittest.mock.patch.object(tron, "mes", unittest.mock.AsyncMock()),
                            unittest.mock.patch.object(tron, "log_print"),
                            unittest.mock.patch.object(tron, "notify_event", unittest.mock.AsyncMock()),
                        ):
                            ok = await tron.submit_qr_payload(
                                session,
                                '{"rollcallId":88,"data":"usc-qr-payload-data"}',
                            )
                    return {"ok": ok, "answers": server.qr_answers}
                finally:
                    tron.CONFIG.clear()
                    tron.CONFIG.update(original_config)

        result = asyncio.run(run())
        self.assertTrue(result["ok"], msg="QR submit should succeed")
        self.assertEqual(result["answers"][0]["rollcall_id"], "88")
        self.assertEqual(result["answers"][0]["body"]["data"], "usc-qr-payload-data")

    def test_usc_radar_rollcall_submits_coordinates_to_correct_base_url(self) -> None:
        """USC Radar: algorithm-derived GPS coordinates are POSTed to USC base URL.
        FakeTronServer accepts any coordinate within success_radius; we set the target
        to 0,0 and probe from 0,0 so the very first attempt lands within range.
        """
        import asyncio

        async def run() -> dict:
            original_config = copy.deepcopy(tron.CONFIG)
            original_completed = dict(tron.COMPLETED_RADAR_ROLLCALLS)
            async with FakeTronServer() as server:
                # Place radar target at origin; accept any probe within 50 km so
                # the test is not sensitive to which coordinate the solver picks first.
                server.set_radar_target(0.0, 0.0, success_radius_meters=50_000)
                server.radar_success = True          # always accept the first probe
                server.rollcalls = [{
                    "rollcall_id": 66,
                    "is_radar": True,
                    "status": "started",
                }]
                try:
                    tron.COMPLETED_RADAR_ROLLCALLS.clear()
                    cfg = self._usc_config_for_server(server)
                    cfg["radar"]["strategy"] = "global_wgs84"
                    tron.CONFIG.update(cfg)
                    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
                        login_result = await tron.login(session)
                        self.assertTrue(login_result.ok, msg="Login failed: {}".format(login_result.status))
                        with (
                            unittest.mock.patch.object(tron, "mes", unittest.mock.AsyncMock()),
                            unittest.mock.patch.object(tron, "log_print"),
                            unittest.mock.patch.object(tron, "status_print"),
                        ):
                            status = await tron.check_rollcall(session, 1)
                    return {"status": status, "radar_answers": server.radar_answers}
                finally:
                    tron.CONFIG.clear()
                    tron.CONFIG.update(original_config)
                    tron.COMPLETED_RADAR_ROLLCALLS.clear()
                    tron.COMPLETED_RADAR_ROLLCALLS.update(original_completed)

        result = asyncio.run(run())
        self.assertEqual(result["status"], "is_radar",
                         msg="Expected radar rollcall to be detected and attempted")
        self.assertGreater(len(result["radar_answers"]), 0,
                           msg="At least one radar coordinate should have been submitted")
        first = result["radar_answers"][0]
        self.assertEqual(first["rollcall_id"], "66")
        self.assertIn("latitude", first["body"],
                      msg="Radar answer body must include latitude")
        self.assertIn("longitude", first["body"],
                      msg="Radar answer body must include longitude")

