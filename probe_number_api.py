"""
probe_number_api.py
====================
在「數字點名」進行中時，使用目前登入帳號探測多個 API 端點，
找出哪一個端點會在回應裡洩漏 number_code。

支援手動執行與即時除錯：
    python probe_number_api.py [rollcall_id]

若未提供 rollcall_id，腳本會自動查詢目前進行中的點名列表。
使用 troTHU 統一底層架構，自動適配學校 SSO 與 Cookie。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from troTHU import runtime_context as ctx
except ImportError:
    import runtime_context as ctx  # type: ignore

PROBE_ENDPOINTS: List[Dict[str, str]] = [
    {"label": "student_rollcalls (學生名單)", "path": "/api/rollcall/{rid}/student_rollcalls"},
    {"label": "student_rollcalls?action=number", "path": "/api/rollcall/{rid}/student_rollcalls?action=number"},
    {"label": "answers (已簽到名單)", "path": "/api/rollcall/{rid}/answers"},
    {"label": "answers (paged)", "path": "/api/rollcall/{rid}/answers?page=1&page_size=200"},
    {"label": "lite (精簡資訊)", "path": f"/api/rollcall/{rid}/lite"},
    {"label": "GET rollcall root", "path": "/api/rollcall/{rid}"},
    {"label": "GET rollcall?api_version=1.1.0", "path": "/api/rollcall/{rid}?api_version=1.1.0"},
    {"label": "GET rollcall/detail", "path": "/api/rollcall/{rid}/detail"},
    {"label": "GET rollcall/info", "path": "/api/rollcall/{rid}/info"},
    {"label": "GET rollcall/status", "path": "/api/rollcall/{rid}/status"},
    {"label": "GET rollcall/number_code", "path": "/api/rollcall/{rid}/number_code"},
    {"label": "GET rollcall/code", "path": "/api/rollcall/{rid}/code"},
    {"label": "GET teacher/rollcall", "path": "/api/teacher/rollcall/{rid}"},
    {"label": "radar/rollcalls (main poll)", "path": "/api/radar/rollcalls?api_version=1.1.0"},
]


def _has_field(obj: Any, name: str, _depth: int = 0) -> bool:
    if _depth > 5:
        return False
    target = name.lower()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() == target:
                return True
            if _has_field(v, name, _depth + 1):
                return True
    elif isinstance(obj, list):
        return any(_has_field(item, name, _depth + 1) for item in obj[:15])
    return False


def _field_names_flat(obj: Any, prefix: str = "", depth: int = 0) -> List[str]:
    names = []
    if depth > 3:
        return names
    if isinstance(obj, dict):
        for k, v in obj.items():
            full = f"{prefix}.{k}" if prefix else k
            names.append(full)
            names.extend(_field_names_flat(v, full, depth + 1))
    elif isinstance(obj, list) and obj:
        names.extend(_field_names_flat(obj[0], f"{prefix}[]", depth + 1))
    return names


def _redacted_preview(obj: Any, depth: int = 0) -> Any:
    sensitive = {"data", "number_code", "numbercode", "token", "cookie", "session", "password", "passwd", "secret", "answer", "code"}
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k.lower() in sensitive:
                result[k] = f"[REDACTED — 長度={len(str(v))}]"
            elif depth < 2:
                result[k] = _redacted_preview(v, depth + 1)
            else:
                result[k] = type(v).__name__
        return result
    if isinstance(obj, list):
        if not obj:
            return []
        return [_redacted_preview(obj[0], depth), f"... ({len(obj)} items)"]
    return obj


async def probe_endpoint(session: Any, base_url: str, label: str, path: str, rollcall_id: str, ssl_setting: Any) -> Dict[str, Any]:
    url = base_url.rstrip("/") + path.format(rid=rollcall_id)
    res = {
        "label": label,
        "url": url,
        "http_status": 0,
        "has_number_code": False,
        "has_data": False,
        "field_names": [],
        "preview": None,
        "error": "",
    }
    kwargs: Dict[str, Any] = {"allow_redirects": False}
    if ssl_setting is not None:
        kwargs["ssl"] = ssl_setting
    try:
        async with session.get(url, timeout=12, **kwargs) as resp:
            res["http_status"] = resp.status
            if resp.status in (401, 403):
                res["error"] = "unauthorized / forbidden"
                return res
            if resp.status == 404:
                res["error"] = "not found (404)"
                return res
            if resp.status == 429:
                res["error"] = "rate limited (429)"
                return res
            if resp.status >= 500:
                res["error"] = f"server error ({resp.status})"
                return res

            ct = resp.headers.get("Content-Type", "").lower()
            if "json" not in ct:
                text = await resp.text()
                res["field_names"] = [f"(non-json: {ct})"]
                res["preview"] = text[:150]
                return res

            data = await resp.json(encoding="utf-8")
            res["has_number_code"] = _has_field(data, "number_code") or _has_field(data, "numberCode")
            res["has_data"] = _has_field(data, "data")
            res["field_names"] = sorted(set(_field_names_flat(data)))[:25]
            res["preview"] = _redacted_preview(data)
    except asyncio.TimeoutError:
        res["error"] = "timeout"
    except Exception as exc:
        res["error"] = str(exc)[:80]
    return res


async def main() -> None:
    rollcall_id_arg: Optional[str] = sys.argv[1] if len(sys.argv) > 1 else None

    print("=" * 60)
    print("  TronClass 數字點名 API 探測工具 (troTHU 驅動)")
    print("=" * 60)

    ctx.bootstrap_config()
    profile = ctx.get_active_profile(ctx.CONFIG)
    endpoints = ctx.get_active_http_endpoints()
    base_url = endpoints.base_url.rstrip("/")
    ssl_setting = ctx.get_ssl_request_setting()

    print(f"[*] 帳號: {profile.name} | 學校: {ctx.get_active_provider_key()} | Base URL: {base_url}")

    session_kwargs: Dict[str, Any] = {
        "connector": ctx.create_http_connector(),
        "headers": {"User-Agent": ctx.random_ua()},
    }
    timeout = ctx.create_http_client_timeout()
    if timeout is not None:
        session_kwargs["timeout"] = timeout

    async with ctx.aiohttp.ClientSession(**session_kwargs) as session:
        # 1. 登入檢查
        cookie_loaded = False
        if ctx.cookie_cache_enabled(ctx.CONFIG):
            cookie_loaded = ctx.load_session_cookies(session, ctx.BASE_DIR, profile.name)

        if not cookie_loaded or not ctx.has_session_cookie(session):
            print(f"[*] 正在為 {profile.name} 登入...")
            login_res = await ctx.login(session)
            if not login_res.ok:
                print(f"[!] 登入失敗: {login_res.status} ({login_res.error})")
                return
            if ctx.cookie_cache_enabled(ctx.CONFIG):
                ctx.save_session_cookies(session, ctx.BASE_DIR, profile.name)
            print("[+] 登入成功！")
        else:
            print("[+] 已載入有效的 Cookie 快取。")

        # 2. 獲取 rollcall_id
        rollcall_id = rollcall_id_arg
        course_id = ""
        if not rollcall_id:
            print("\n[*] 正在向伺服器查詢目前進行中的點名...")
            client = ctx.create_tron_http_client(session, request_ssl=ssl_setting)
            try:
                res = await client.fetch_rollcalls()
                rollcalls = res.payload.get("rollcalls") or []
                print(f"[*] 目前有 {len(rollcalls)} 個點名活動進行中")
                for rc in rollcalls:
                    if not isinstance(rc, dict):
                        continue
                    rtype = str(rc.get("attendance_type") or rc.get("type") or "")
                    rid = str(rc.get("rollcall_id") or rc.get("id") or "")
                    cid = str(rc.get("course_id") or rc.get("courseId") or "")
                    print(f"  → rollcall_id={rid} | type={rtype} | course={cid}")
                    if "number" in rtype.lower():
                        rollcall_id = rid
                        course_id = cid
                        break
                if not rollcall_id and rollcalls:
                    rollcall_id = str(rollcalls[0].get("rollcall_id") or rollcalls[0].get("id") or "")
                    course_id = str(rollcalls[0].get("course_id") or rollcalls[0].get("courseId") or "")
            except Exception as exc:
                print(f"[!] 查詢點名失敗: {exc}")

        if not rollcall_id:
            print("\n[!] 目前沒有正在進行的點名，請在數字點名開始時執行，或直接指定 ID：")
            print("    python probe_number_api.py <rollcall_id>")
            return

        print(f"\n[*] 開始探測目標 rollcall_id = {rollcall_id} (course = {course_id or 'unknown'})\n")

        endpoints_to_probe = list(PROBE_ENDPOINTS)
        if course_id:
            endpoints_to_probe.extend([
                {"label": "course rollcall detail", "path": f"/api/course/{course_id}/rollcall/{rollcall_id}"},
                {"label": "course rollcalls list", "path": f"/api/course/{course_id}/rollcalls"},
            ])

        interesting: List[Dict[str, Any]] = []
        for ep in endpoints_to_probe:
            r = await probe_endpoint(session, base_url, ep["label"], ep["path"], rollcall_id, ssl_setting)
            icon = "🎯" if r["has_number_code"] else ("✅" if r["http_status"] == 200 else "❌")
            print(f"{icon} [{r['http_status']}] {r['label']}")
            if r["error"]:
                print(f"   └─ {r['error']}")
            else:
                if r["has_number_code"]:
                    print("   🎯 *** 發現 number_code 欄位！***")
                    interesting.append(r)
                if r["has_data"]:
                    print("   📦 發現 data 欄位")
                if r["field_names"]:
                    print(f"   └─ 欄位: {', '.join(r['field_names'][:12])}")
            print()
            await asyncio.sleep(0.3)

        print("=" * 60)
        print("  探測結果摘要")
        print("=" * 60)
        if interesting:
            print(f"🎯 成功找到 {len(interesting)} 個含 number_code 的端點：")
            for r in interesting:
                print(f"  • {r['label']} -> {r['url']}")
        else:
            print("❌ 目前探測的端點均未直接返回 number_code。")


if __name__ == "__main__":
    asyncio.run(main())
