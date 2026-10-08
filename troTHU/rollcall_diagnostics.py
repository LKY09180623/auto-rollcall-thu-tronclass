"""Read current attendance and explain the monitor's gate without submitting."""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone
from typing import Any, Dict
from urllib.parse import quote

import aiohttp

try:
    from troTHU import runtime_context as ctx
    from troTHU.environment_config import EnvironmentConfigError
    from troTHU.monitor_runtime import ATTENDANCE_RATE_GATE_PERCENT
    from troTHU.rollcall_engine import decide_rollcall
    from troTHU.rollcall_progress import summarize_rollcall_progress
except ImportError:  # pragma: no cover - direct script fallback
    import runtime_context as ctx
    from environment_config import EnvironmentConfigError
    from monitor_runtime import ATTENDANCE_RATE_GATE_PERCENT
    from rollcall_engine import decide_rollcall
    from rollcall_progress import summarize_rollcall_progress


MESSAGES = {
    "detected": "已偵測到點名活動；這次查詢沒有提交答案。",
    "no_active_rollcall": "目前帳號的點名清單沒有待處理活動；不代表之後的活動無法偵測。",
    "cookie_missing": "沒有可用的登入快取，尚未向學校查詢；請先完成登入。",
    "login_expired": "學校端未接受目前登入狀態，請重新登入後再查。",
    "forbidden": "學校端拒絕讀取此資料，無法確認目前點名。",
    "rate_limited": "查詢受到頻率限制；本次沒有重試。",
    "service_unavailable": "學校端暫時無法回應，無法確認目前點名。",
    "http_error": "學校端未接受查詢，無法確認目前點名。",
    "invalid_response": "回應格式無法辨識，不能當成沒有點名。",
    "network_error": "查詢連線失敗或逾時，無法確認目前點名。",
    "config_error": "設定無法讀取，請檢查本機或環境帳號設定。",
    "profile_not_found": "指定帳號不在目前設定中。",
    "provider_blocked": "目前學校設定尚未允許日常查詢，請檢查 provider 設定。",
}
GATE_MESSAGES = {
    "waiting_for_rate": "已偵測到活動；依設定，監控會等待簽到率達 15%。",
    "rate_unavailable": "已偵測到活動，但簽到率無法讀取；依設定，監控會停在等待條件。",
    "ready": "已偵測到活動，簽到率已達門檻；這不代表已完成簽到。",
    "disabled": "目前不需等待簽到率；這不代表已完成簽到。",
    "already_confirmed": "目前帳號已有出席確認。",
    "unrecognized": "清單有活動，但目前監控未辨識其類型。",
    "not_applicable": "目前沒有可評估等待條件的活動。",
}


def _result(status: str, **data: Any) -> Dict[str, Any]:
    return {
        "ok": status in {"detected", "no_active_rollcall"},
        "status": status,
        "read_only": True,
        "submission_attempted": False,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "message": MESSAGES[status],
        **data,
    }


async def _read_json(session: Any, url: str, request_ssl: Any) -> Dict[str, Any]:
    try:
        async with session.get(url, ssl=request_ssl, allow_redirects=False) as response:
            code = response.status
            if code == 401 or 300 <= code < 400:
                status = "login_expired"
            elif code == 403:
                status = "forbidden"
            elif code == 429:
                status = "rate_limited"
            elif code >= 500:
                status = "service_unavailable"
            elif code != 200:
                status = "http_error"
            else:
                try:
                    payload = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    return {"ok": False, "status": "invalid_response", "http_status": code}
                return {"ok": True, "payload": payload, "http_status": code}
            return {"ok": False, "status": status, "http_status": code}
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return {"ok": False, "status": "network_error", "http_status": None}


def _activity(item: Dict[str, Any]) -> Dict[str, Any]:
    decision = decide_rollcall([item])
    rollcall_id = str(item.get("rollcall_id") or item.get("id") or "")
    if not (rollcall_id.isascii() and rollcall_id.isdecimal()):
        rollcall_id = ""
    completed = item.get("status") == "on_call_fine" or item.get("rollcall_status") == "on_call_fine"
    closed = item.get("status") in ("closed", "ended", "finished", "cancelled", "canceled")
    return {
        "rollcall_id": rollcall_id,
        "type": decision.attendance_type.value,
        "monitor_status": decision.status,
        "state": "confirmed" if completed else "closed" if closed else "pending",
    }


async def inspect_rollcalls(
    session: Any, *, endpoints: Any, user: str, request_ssl: Any = None, ignore_gate: bool = False,
) -> Dict[str, Any]:
    response = await _read_json(session, endpoints.rollcalls_url, request_ssl)
    if not response["ok"]:
        return _result(response["status"], http_status=response["http_status"])
    payload = response["payload"]
    items = payload.get("rollcalls") if isinstance(payload, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return _result("invalid_response", http_status=response["http_status"])
    # Emit only allowlisted summary fields; never return source payloads, codes, or rosters.
    activities = [_activity(item) for item in items[:50]]
    pending = [item for item in activities if item["state"] == "pending"]
    decision = decide_rollcall(items)
    selected = _activity(decision.rollcall) if decision.rollcall is not None else None
    gate_exempt = selected is not None and selected["type"] in {"qrcode", "number"}
    gate = {"enabled": not (ignore_gate or gate_exempt), "threshold_percent": ATTENDANCE_RATE_GATE_PERCENT, "state": "not_applicable"}
    if selected is not None and selected["state"] == "confirmed":
        gate["state"] = "already_confirmed"
    elif selected is not None and selected["state"] == "pending":
        if selected["type"] not in {"radar", "number", "qrcode"} or not selected["rollcall_id"]:
            gate["state"] = "unrecognized"
        else:
            url = "{}/api/rollcall/{}/student_rollcalls".format(
                endpoints.base_url.rstrip("/"), quote(selected["rollcall_id"], safe=""),
            )
            progress_response = await _read_json(session, url, request_ssl)
            roster = progress_response.get("payload")
            if progress_response["ok"] and not (isinstance(roster, dict) and isinstance(roster.get("student_rollcalls"), list)):
                progress_response = {"ok": False, "status": "invalid_response", "http_status": progress_response["http_status"]}
            gate["progress_status"] = "ok" if progress_response["ok"] else progress_response["status"]
            gate["progress_http_status"] = progress_response["http_status"]
            progress = summarize_rollcall_progress(progress_response.get("payload"), None, user)
            gate["present_rate_percent"] = progress["present_rate_percent"]
            gate["personal_confirmed"] = progress["my_present"]
            if progress["my_present"]:
                gate["state"] = "already_confirmed"
            elif ignore_gate or gate_exempt:
                gate["state"] = "disabled"
            elif not progress["present_rate_known"]:
                gate["state"] = "rate_unavailable"
            elif progress["present_rate_percent"] < ATTENDANCE_RATE_GATE_PERCENT:
                gate["state"] = "waiting_for_rate"
            else:
                gate["state"] = "ready"
    gate["message"] = GATE_MESSAGES[gate["state"]]
    return _result(
        "detected" if pending else "no_active_rollcall", http_status=response["http_status"],
        activity_count=len(items), activities=activities, truncated=len(items) > len(activities),
        selected=selected, gate=gate,
    )


async def _inspect_cached_session() -> Dict[str, Any]:
    active = ctx.get_active_profile(ctx.CONFIG)
    metadata = {"profile": active.name, "provider": ctx.get_active_provider_key()}
    if not ctx.provider_is_daily_allowed():
        return _result("provider_blocked", **metadata)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        if not ctx.load_session_cookies(session, ctx.BASE_DIR, active.name) or not ctx.has_session_cookie(session):
            return _result("cookie_missing", **metadata)
        result = await inspect_rollcalls(
            session, endpoints=ctx.get_active_http_endpoints(), user=active.user,
            request_ssl=ctx.get_ssl_request_setting(), ignore_gate=ctx.get_ignore_attendance_rate_gate(),
        )
        return {**result, **metadata}


def rollcall_status_command(*, profile: str = "", json_output: bool = False) -> int:
    original = copy.deepcopy(ctx.CONFIG)
    try:
        config = ctx.load_config(read_only=True)
        profiles = {item.name for item in ctx.list_profiles(config)}
        if profile and profile not in profiles:
            report = _result("profile_not_found")
        else:
            ctx.CONFIG.clear()
            ctx.CONFIG.update(config)
            if profile:
                ctx.switch_profile(ctx.CONFIG, profile)
            report = asyncio.run(_inspect_cached_session())
    except (EnvironmentConfigError, OSError, ValueError):
        report = _result("config_error")
    finally:
        ctx.CONFIG.clear()
        ctx.CONFIG.update(original)
    if json_output:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(report["message"])
        for item in report.get("activities", []):
            print(" - #{} {}：{}".format(item["rollcall_id"] or "?", item["type"], item["state"]))
        if report.get("gate"):
            print(report["gate"]["message"])
    return 0 if report["ok"] else 1
