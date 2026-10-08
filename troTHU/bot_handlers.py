import contextlib
import copy
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Optional

try:
    import aiohttp
except ModuleNotFoundError:  # pragma: no cover - dependency-missing CLI fallback
    aiohttp = None  # type: ignore

try:
    from troTHU.account_store import (
        clear_session_cookies,
        cookie_cache_enabled,
        get_active_profile,
        load_session_cookies,
        save_session_cookies,
        switch_profile,
    )
    from troTHU.account_runtime_store import (
        load_runtime_state,
        mark_check_result,
        mark_login_result,
        mark_profile_error,
        runtime_profile_summary,
    )
    from troTHU.bot_status import (
        MAX_ACCOUNTS_IN_REPLY,
        build_profile_status_summary,
        format_accounts_reply,
        format_profile_status_reply,
    )
    from troTHU.bot_runtime import BotAuditEvent, BotRuntime, BotRuntimeHandlers
    from troTHU.pending_qr import list_pending_qr
except ImportError:  # pragma: no cover - script execution fallback
    from account_store import (
        clear_session_cookies,
        cookie_cache_enabled,
        get_active_profile,
        load_session_cookies,
        save_session_cookies,
        switch_profile,
    )
    from account_runtime_store import (
        load_runtime_state,
        mark_check_result,
        mark_login_result,
        mark_profile_error,
        runtime_profile_summary,
    )
    from bot_status import (
        MAX_ACCOUNTS_IN_REPLY,
        build_profile_status_summary,
        format_accounts_reply,
        format_profile_status_reply,
    )
    from bot_runtime import BotAuditEvent, BotRuntime, BotRuntimeHandlers
    from pending_qr import list_pending_qr


SessionFactory = Callable[[], Any]


class BotHandlerBridge:
    def __init__(
        self,
        config: Dict[str, Any],
        *,
        base_dir: Path,
        session_factory: Optional[SessionFactory] = None,
        tron_module: Any = None,
    ) -> None:
        self.config = config
        self.base_dir = Path(base_dir)
        self.session_factory = session_factory
        if tron_module is None:
            try:
                from troTHU import runtime_context as tron_module  # pylint: disable=import-outside-toplevel
            except ImportError:  # pragma: no cover - script execution fallback
                import runtime_context as tron_module  # type: ignore

        self.tron = tron_module

    def record_login_result(self, profile: str, result: Any) -> None:
        try:
            mark_login_result(self.base_dir, profile, result)
        except Exception:
            return

    def record_check_result(
        self,
        profile: str,
        status: str,
        *,
        rollcall_id: str = "",
        rollcall_type: str = "",
    ) -> None:
        try:
            mark_check_result(
                self.base_dir,
                profile,
                status,
                rollcall_id=rollcall_id,
                rollcall_type=rollcall_type,
            )
        except Exception:
            return

    def record_profile_error(self, profile: str, status: str, message: Any) -> None:
        try:
            mark_profile_error(self.base_dir, profile, status, message)
        except Exception:
            return

    @contextlib.contextmanager
    def profile_context(self, profile_name: str):
        original_config_profile = get_active_profile(self.config).name
        original_tron_profile = get_active_profile(self.tron.CONFIG).name
        original_base_dir = self.tron.BASE_DIR
        original_config_provider = copy.deepcopy(self.config.get("provider", {}))
        original_tron_provider = copy.deepcopy(self.tron.CONFIG.get("provider", {}))
        try:
            switch_profile(self.config, profile_name)
            if self.config is not self.tron.CONFIG:
                switch_profile(self.tron.CONFIG, profile_name)
            self.tron.BASE_DIR = self.base_dir
            yield
        finally:
            switch_profile(self.config, original_config_profile)
            if self.config is not self.tron.CONFIG:
                switch_profile(self.tron.CONFIG, original_tron_profile)
                self.tron.CONFIG["provider"] = original_tron_provider
            self.config["provider"] = original_config_provider
            self.tron.BASE_DIR = original_base_dir

    @contextlib.asynccontextmanager
    async def session_context(self) -> AsyncIterator[Any]:
        if self.session_factory is not None:
            candidate = self.session_factory()
        else:
            if aiohttp is None:
                raise RuntimeError("aiohttp is required for bot handlers")
            session_kwargs: Dict[str, Any] = {
                "connector": self.tron.create_http_connector(),
                "headers": {"User-Agent": self.tron.random_ua()},
            }
            timeout = self.tron.create_http_client_timeout()
            if timeout is not None:
                session_kwargs["timeout"] = timeout
            candidate = aiohttp.ClientSession(**session_kwargs)

        if hasattr(candidate, "__aenter__"):
            async with candidate as session:
                yield session
            return

        try:
            yield candidate
        finally:
            close = getattr(candidate, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result

    async def ensure_login(self, session: Any, *, force: bool = False):
        active = get_active_profile(self.config)
        if force:
            clear_session_cookies(self.base_dir, active.name)
            if hasattr(session, "cookie_jar"):
                session.cookie_jar.clear()
        elif cookie_cache_enabled(self.config):
            load_session_cookies(session, self.base_dir, active.name)
            if self.tron.has_session_cookie(session):
                result = self.tron.LoginResult(
                    status="success",
                    credential_source="cookie_cache",
                    user=active.user,
                    final_url="cookie-cache",
                )
                self.record_login_result(active.name, result)
                return result

        result = await self.tron.login(session)
        self.record_login_result(active.name, result)
        if result.ok and cookie_cache_enabled(self.config):
            save_session_cookies(session, self.base_dir, active.name)
        return result

    def _profile_status_details(self, profile: str, state: str) -> Dict[str, Any]:
        with self.profile_context(profile):
            active = get_active_profile(self.config)
            cookie = self.tron.cookie_report(active.name)
            pending = [
                item.to_dict()
                for item in list_pending_qr(self.base_dir)
                if item.profile == active.name
            ]
            bindings = self.tron.binding_summary(active.name)
            runtime_state = runtime_profile_summary(load_runtime_state(self.base_dir), active.name)
            course_discovery = self.tron.course_discovery_report()
            summary = build_profile_status_summary(
                active.name,
                state=state,
                cookie=cookie,
                runtime_state=runtime_state,
                pending_qr=pending,
                bindings=bindings,
                course_discovery=course_discovery,
            )
            last_login = {
                "status": self.tron.LAST_LOGIN_RESULT.status,
                "credential_source": self.tron.LAST_LOGIN_RESULT.credential_source,
                "user": self.tron.LAST_LOGIN_RESULT.user,
            }
        return {
            "summary": summary,
            "cookie": summary["cookie"],
            "pending_qr_count": summary["pending_qr_count"],
            "binding_count": summary["binding_count"],
            "adapter_counts": summary["adapter_counts"],
            "runtime_state": runtime_state,
            "last_login": last_login,
        }

    async def status(self, *, profile: str, state: str, command: Any) -> Dict[str, Any]:
        details = self._profile_status_details(profile, state)
        summary = details["summary"]
        return {
            "reply": format_profile_status_reply(summary),
            "profile": profile,
            "state": state,
            "cookie": details["cookie"],
            "pending_qr_count": details["pending_qr_count"],
            "binding_count": details["binding_count"],
            "adapter_counts": details["adapter_counts"],
            "runtime_state": details["runtime_state"],
            "last_login": details["last_login"],
            "status_summary": summary,
        }

    async def accounts(
        self,
        *,
        profiles: list[str],
        states: Dict[str, str],
        command: Any,
        admin: bool,
        total_count: int,
    ) -> Dict[str, Any]:
        summaries = [
            self._profile_status_details(profile, states.get(profile, "stopped"))["summary"]
            for profile in profiles
        ]
        truncated = len(summaries) > MAX_ACCOUNTS_IN_REPLY
        visible_summaries = summaries[:MAX_ACCOUNTS_IN_REPLY]
        return {
            "reply": format_accounts_reply(
                visible_summaries,
                total_count=total_count,
                visible_count=len(summaries),
                truncated=truncated,
            ),
            "profiles": list(profiles),
            "profile_summaries": visible_summaries,
            "total_count": total_count,
            "visible_count": len(summaries),
            "truncated": truncated,
            "admin": admin,
        }

    async def force_check(self, *, profile: str, command: Any, admin: bool) -> Dict[str, Any]:
        with self.profile_context(profile):
            async with self.session_context() as session:
                login_result = await self.ensure_login(session)
                if not login_result.ok:
                    self.record_profile_error(profile, "login_failed", login_result.status)
                    return {
                        "reply": "Force check failed: login {}.".format(login_result.status),
                        "status": "login_failed",
                        "login": login_result.status,
                    }
                result = await self.tron.check_rollcall(session, -1)
                self.record_check_result(profile, result)
        return {
            "reply": "Force check completed for {}: {}.".format(profile, result),
            "status": "ok",
            "result": result,
            "admin": admin,
        }

    async def reauth(self, *, profile: str, command: Any, admin: bool) -> Dict[str, Any]:
        with self.profile_context(profile):
            async with self.session_context() as session:
                result = await self.ensure_login(session, force=True)
                self.record_login_result(profile, result)
        return {
            "reply": "Reauth {} for {}.".format("succeeded" if result.ok else result.status, profile),
            "status": result.status,
            "ok": result.ok,
            "admin": admin,
        }

    async def qr_submit(self, *, profile: str, payload: str, command: Any) -> Dict[str, Any]:
        fanout = bool(getattr(command, "payload", {}).get("fanout"))
        if fanout:
            with self.profile_context(profile):
                async def submit_profile(_profile: str, raw_payload: str) -> int:
                    async with self.session_context() as session:
                        result = await self._submit_with_session_refresh(
                            session, _profile, lambda: self._submit_provided_qr(session, _profile, raw_payload),
                        )
                    return 0 if result["ok"] else 1

                result = await self.tron.qr_fanout_result(payload, submit_profile=submit_profile)
            status = result.get("status")
            reply = "QR fan-out {} for {} matching profile(s).".format(
                status,
                result.get("match_count", 0),
            )
            return {
                "reply": reply,
                **result,
            }

        stripped = payload.strip()
        is_number = stripped.isascii() and stripped.isdigit() and len(stripped) == 4
        command_payload = getattr(command, "payload", {}) or {}
        expected_type = str(command_payload.get("expected_type") or "")
        expected_rollcall_id = str(command_payload.get("expected_rollcall_id") or "")
        if expected_type and expected_type != ("number" if is_number else "qrcode"):
            return {"ok": False, "status": "invalid_payload", "reply": "輸入內容與目前點名類型不符。"}
        with self.profile_context(profile):
            async with self.session_context() as session:
                operation = (
                    (lambda: self._submit_provided_number(session, profile, stripped, expected_rollcall_id=expected_rollcall_id)) if is_number
                    else (lambda: self._submit_provided_qr(session, profile, payload, expected_rollcall_id=expected_rollcall_id))
                )
                return await self._submit_with_session_refresh(session, profile, operation)

    async def _submit_with_session_refresh(self, session: Any, profile: str, operation: Callable) -> Dict[str, Any]:
        """Refresh once only after an explicit authentication rejection."""
        for refresh in (False, True):
            login_result = await self.ensure_login(session, force=refresh)
            if not login_result.ok:
                self.record_profile_error(profile, "login_failed", login_result.status)
                return {
                    "ok": False, "status": "login_failed", "login": login_result.status,
                    "reply": "登入未成功（{}）；這次未完成點名，請稍後再試或重新登入。".format(login_result.status),
                }
            result = await operation()
            if result.get("status") != "login_failed" or refresh:
                return result

    async def _submit_provided_qr(self, session: Any, profile: str, payload: str, *, expected_rollcall_id: str = "") -> Dict[str, Any]:
        try:
            qr_data = self.tron.parse_qr_payload(payload)
            if not qr_data.rollcall_id or not qr_data.data:
                raise ValueError("Missing QR fields")
            if expected_rollcall_id and str(qr_data.rollcall_id) != expected_rollcall_id:
                return {"ok": False, "status": "rollcall_mismatch", "reply": "這份 QR 屬於另一場點名，請重新掃描目前的 QR。"}
            confirmed = await self.tron.submit_qr_payload(session, payload)
        except ValueError:
            return {"ok": False, "status": "invalid_payload", "reply": "QR 內容不完整或無法辨識，請重新掃描。"}
        except self.tron.UnauthorizedError:
            return {"ok": False, "status": "login_failed", "reply": "QR 提交失敗：登入狀態已失效，請重新登入。"}
        except (self.tron.TronHttpError, aiohttp.ClientError, self.tron.asyncio.TimeoutError):
            return {"ok": False, "status": "request_failed", "reply": "QR 未能完成提交，請確認網路、點名活動與 QR 是否仍有效。"}
        self.record_check_result(
            profile,
            "qrcode_submitted" if confirmed else "submitted_unconfirmed",
            rollcall_id=qr_data.rollcall_id,
            rollcall_type="qrcode",
        )
        return {
            "ok": bool(confirmed),
            "status": "ok" if confirmed else "submitted_unconfirmed",
            "reply": (
                "QR 點名已確認成功（帳號：{}）。" if confirmed
                else "QR 已送出，但尚未確認你的出席狀態（帳號：{}）。請先查看官方點名紀錄。"
            ).format(profile),
        }

    async def _submit_provided_number(self, session: Any, profile: str, code: str, *, expected_rollcall_id: str = "") -> Dict[str, Any]:
        """Submit only the code supplied in this command, then check attendance."""
        client = self.tron.create_tron_http_client(session, request_ssl=self.tron.get_ssl_request_setting())
        try:
            rollcalls_res = await client.fetch_rollcalls()
        except self.tron.UnauthorizedError:
            return {"ok": False, "status": "login_failed", "reply": "數字點名查詢失敗：登入狀態已失效。"}
        except (self.tron.TronHttpError, aiohttp.ClientError, self.tron.asyncio.TimeoutError):
            return {"ok": False, "status": "request_failed", "reply": "查詢進行中點名失敗，請稍後再試。"}

        rollcalls = rollcalls_res.payload.get("rollcalls") if isinstance(rollcalls_res.payload, dict) else None
        if not isinstance(rollcalls, list):
            return {"ok": False, "status": "request_failed", "reply": "點名清單格式無法辨識，請查看官方點名頁面。"}
        # A generic in_progress activity can be radar or QR; never send a number to it.
        candidates = {
            str(rc["rollcall_id"]): rc
            for rc in rollcalls
            if isinstance(rc, dict) and rc.get("is_number") and rc.get("rollcall_id")
            and rc.get("status") not in {"on_call_fine", "closed", "ended", "finished", "cancelled", "canceled"}
        }
        if not candidates:
            return {"ok": False, "status": "no_rollcall", "reply": "目前未偵測到待提交的數字點名活動，請確認官方點名頁面。"}
        if expected_rollcall_id and expected_rollcall_id not in candidates:
            return {"ok": False, "status": "rollcall_mismatch", "reply": "這場數字點名已結束或變更，請更新頁面後再輸入。"}
        if expected_rollcall_id:
            candidates = {expected_rollcall_id: candidates[expected_rollcall_id]}
        if len(candidates) > 1:
            return {"ok": False, "status": "ambiguous_rollcall", "reply": "目前有多個數字點名活動，請至官方頁面選擇正確課程後提交。"}
        target_rcid = next(iter(candidates))
        base_url = self.tron.get_active_http_endpoints().base_url.rstrip("/")
        request_url = "{}/api/rollcall/{}/answer_number_rollcall".format(base_url, target_rcid)
        put_payload = {
            "deviceId": getattr(session, "_tron_device_id", None) or self.tron.random_id(),
            "numberCode": code,
        }
        try:
            async with session.put(
                request_url, json=put_payload, ssl=self.tron.get_ssl_request_setting(), allow_redirects=False,
            ) as resp:
                status_code = resp.status
                classification = self.tron.classify_number_response(status_code, await resp.text())
        except (aiohttp.ClientError, self.tron.asyncio.TimeoutError):
            return {"ok": False, "status": "request_failed", "reply": "數字點名請求中斷，結果未明；請先查看官方點名紀錄。"}

        statuses = self.tron.NumberAttemptStatus
        if classification.status != statuses.SUCCESS:
            if status_code == 429:
                status, message = "rate_limited", "伺服器限制請求頻率，請稍後再試。"
            elif classification.status == statuses.UNAUTHORIZED:
                status, message = "login_failed", "登入狀態已失效，請重新登入。"
            elif classification.status == statuses.WRONG_CODE:
                status, message = "wrong_code", "點名碼被拒絕，請確認代碼及點名是否仍有效。"
            else:
                status, message = "request_failed", "伺服器未接受提交，請稍後再試。"
            return {"ok": False, "status": status, "reply": "數字點名未完成：" + message}

        try:
            verification = await self.tron.verify_rollcall_on_call_fine(session, target_rcid, rollcall_type="number")
            confirmed = bool(verification.get("ok") and verification.get("status") == "on_call_fine")
        except Exception:
            confirmed = False
        self.record_check_result(
            profile,
            "number_submitted" if confirmed else "submitted_unconfirmed",
            rollcall_id=target_rcid,
            rollcall_type="number",
        )
        return {
            "ok": confirmed,
            "status": "ok" if confirmed else "submitted_unconfirmed",
            "reply": (
                "數字點名已確認成功（帳號：{}）。" if confirmed
                else "數字點名碼已送出，但尚未確認你的出席狀態（帳號：{}）。請先查看官方點名紀錄。"
            ).format(profile),
        }

    async def audit(self, *, event: BotAuditEvent) -> None:
        self.tron.log(
            event="bot_command_audit",
            status="allowed" if event.allowed else "rejected",
            message="Bot command {}: {}".format(event.action, event.reason),
            payload_excerpt=event.to_dict(),
        )


def create_bot_runtime(
    config: Dict[str, Any],
    *,
    base_dir: Path,
    session_factory: Optional[SessionFactory] = None,
) -> BotRuntime:
    bridge = BotHandlerBridge(
        config,
        base_dir=base_dir,
        session_factory=session_factory,
    )
    return BotRuntime(
        config,
        BotRuntimeHandlers(
            status=bridge.status,
            accounts=bridge.accounts,
            force_check=bridge.force_check,
            reauth=bridge.reauth,
            qr_submit=bridge.qr_submit,
            audit=bridge.audit,
        ),
        runtime_base_dir=base_dir,
    )
