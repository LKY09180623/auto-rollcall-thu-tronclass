"""
probe_daemon.py
===============
常駐後台探針：持續輪詢 TronClass，
當偵測到「數字點名」正在進行時，自動對多個候選端點發起探測，
記錄哪些端點的回應包含 number_code 或其他敏感欄位。

本探針直接使用 troTHU 統一底層架構，自動適配學校（USC、THU 等）、
自動管理 Cookie 與 SSO 登入、自動重試與異常恢復。

輸出檔案：
- probe_status.json : 探針心跳與即時狀態
- probe_log.jsonl   : 歷史探測記錄（每點名一次，去重複）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from troTHU import runtime_context as ctx
except ImportError:
    import runtime_context as ctx  # type: ignore

STATUS_FILE = ROOT / "probe_status.json"
LOG_FILE = ROOT / "probe_log.jsonl"
DEFAULT_POLL_INTERVAL = 3.0


def _get_poll_interval() -> float:
    try:
        val = float(os.getenv("TRON_PROBE_INTERVAL", str(DEFAULT_POLL_INTERVAL)))
        return max(0.5, val)
    except Exception:
        return DEFAULT_POLL_INTERVAL


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _find_field_keys(obj: Any, targets: tuple[str, ...], _depth: int = 0) -> List[str]:
    found = []
    if _depth > 5:
        return found
    target_set = {t.lower() for t in targets}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in target_set:
                found.append(k)
            found.extend(_find_field_keys(v, targets, _depth + 1))
    elif isinstance(obj, list):
        for item in obj[:10]:
            found.extend(_find_field_keys(item, targets, _depth + 1))
    return sorted(set(found))


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


def _append_log(entry: Dict[str, Any]) -> None:
    try:
        existing: List[str] = []
        if LOG_FILE.exists():
            existing = [l for l in LOG_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        existing.append(json.dumps(entry, ensure_ascii=False))
        if len(existing) > MAX_LOG_ENTRIES:
            existing = existing[-MAX_LOG_ENTRIES:]
        LOG_FILE.write_text("\n".join(existing) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"[probe_daemon] log write error: {exc}", flush=True)


def _write_status(data: Dict[str, Any]) -> None:
    try:
        STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _build_candidate_endpoints(rollcall_id: str, course_id: str = "") -> List[Dict[str, str]]:
    rid = str(rollcall_id).strip()
    cid = str(course_id).strip()
    endpoints = [
        {"label": "student_rollcalls (學生名單)", "path": f"/api/rollcall/{rid}/student_rollcalls"},
        {"label": "student_rollcalls?action=number", "path": f"/api/rollcall/{rid}/student_rollcalls?action=number"},
        {"label": "answers (已簽到名單)", "path": f"/api/rollcall/{rid}/answers"},
        {"label": "answers (paged)", "path": f"/api/rollcall/{rid}/answers?page=1&page_size=200"},
        {"label": "lite (精簡資訊)", "path": f"/api/rollcall/{rid}/lite"},
        {"label": "GET rollcall root", "path": f"/api/rollcall/{rid}"},
        {"label": "GET rollcall?api_version=1.1.0", "path": f"/api/rollcall/{rid}?api_version=1.1.0"},
        {"label": "GET rollcall/detail", "path": f"/api/rollcall/{rid}/detail"},
        {"label": "GET rollcall/info", "path": f"/api/rollcall/{rid}/info"},
        {"label": "GET rollcall/status", "path": f"/api/rollcall/{rid}/status"},
        {"label": "GET rollcall/number_code", "path": f"/api/rollcall/{rid}/number_code"},
        {"label": "GET rollcall/code", "path": f"/api/rollcall/{rid}/code"},
        {"label": "GET teacher/rollcall", "path": f"/api/teacher/rollcall/{rid}"},
        {"label": "radar/rollcalls", "path": "/api/radar/rollcalls?api_version=1.1.0"},
    ]
    if cid:
        endpoints.extend([
            {"label": "course rollcall detail", "path": f"/api/course/{cid}/rollcall/{rid}"},
            {"label": "course rollcalls list", "path": f"/api/course/{cid}/rollcalls"},
        ])
    return endpoints


async def _probe_one(
    session: Any,
    base_url: str,
    ep: Dict[str, str],
    ssl_setting: Any = None,
) -> Dict[str, Any]:
    url = base_url.rstrip("/") + ep["path"]
    res: Dict[str, Any] = {
        "label": ep["label"],
        "path": ep["path"],
        "url": url,
        "http_status": 0,
        "has_number_code": False,
        "has_data_field": False,
        "matched_keys": [],
        "top_fields": [],
        "error": "",
    }
    kwargs: Dict[str, Any] = {"allow_redirects": False}
    if ssl_setting is not None:
        kwargs["ssl"] = ssl_setting
    try:
        async with session.get(url, timeout=12, **kwargs) as resp:
            res["http_status"] = resp.status
            if resp.status not in (200, 201):
                return res
            ct = resp.headers.get("Content-Type", "").lower()
            if "json" not in ct:
                res["top_fields"] = [f"(non-json: {ct})"]
                return res
            try:
                body = await resp.json(encoding="utf-8")
            except Exception:
                res["top_fields"] = ["(json-parse-failed)"]
                return res

            res["has_number_code"] = _has_field(body, "number_code") or _has_field(body, "numberCode")
            res["has_data_field"] = _has_field(body, "data")
            res["matched_keys"] = _find_field_keys(body, ("number_code", "numberCode", "code", "accessCode"))
            res["top_fields"] = sorted(set(_field_names_flat(body)))[:30]
    except asyncio.TimeoutError:
        res["error"] = "timeout"
    except Exception as exc:
        res["error"] = str(exc)[:80]
    return res


def check_operating_schedule() -> tuple[bool, str, Optional[datetime]]:
    try:
        now = ctx.current_datetime()
        next_switch = ctx.next_schedule_transition(now)
        weekday = now.weekday()
        schedule = ctx.get_schedule_for_day(weekday)
        if not schedule.get("enable", False):
            weekday_names = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
            day_str = weekday_names[weekday] if 0 <= weekday < 7 else f"Day {weekday}"
            return False, f"今日非上課日 ({day_str})", next_switch
        ranges = schedule.get("ranges", schedule.get("range"))
        current_time = now.time()
        if not ctx.is_within_any_schedule(ranges, current_time):
            return False, f"非上課時間 (目前: {now.strftime('%H:%M')})", next_switch
        return True, "上課時間中", next_switch
    except Exception as exc:
        return True, f"排程檢查略過 ({exc})", None


async def run_probe_loop() -> None:
    ctx.bootstrap_config()
    active_profile = ctx.get_active_profile(ctx.CONFIG)
    endpoints = ctx.get_active_http_endpoints()
    base_url = endpoints.base_url.rstrip("/")
    ssl_setting = ctx.get_ssl_request_setting()

    print(
        f"[probe_daemon] 啟動成功 | 帳號: {active_profile.name} | "
        f"Provider: {ctx.get_active_provider_key()} | BaseURL: {base_url}",
        flush=True,
    )

    headers = {"User-Agent": ctx.random_ua()}
    session_kwargs: Dict[str, Any] = {
        "connector": ctx.create_http_connector(),
        "headers": headers,
    }
    timeout = ctx.create_http_client_timeout()
    if timeout is not None:
        session_kwargs["timeout"] = timeout

    probed_rollcall_ids: set[str] = set()
    polls_count = 0
    probed_count = 0
    last_probed_id: Optional[str] = None

    async with ctx.aiohttp.ClientSession(**session_kwargs) as session:
        # 1. 嘗試載入 Cookie 快取
        cookie_loaded = False
        try:
            if ctx.cookie_cache_enabled(ctx.CONFIG):
                cookie_loaded = ctx.load_session_cookies(session, ctx.BASE_DIR, active_profile.name)
        except Exception:
            pass

        in_sched, sched_reason, _ = check_operating_schedule()
        # 2. 若在排程內且無 Cookie 則嘗試登入
        if in_sched and (not cookie_loaded or not ctx.has_session_cookie(session)):
            print(f"[probe_daemon] 正在為 {active_profile.name} 執行首次登入...", flush=True)
            login_res = await ctx.login(session)
            if login_res.ok and ctx.cookie_cache_enabled(ctx.CONFIG):
                ctx.save_session_cookies(session, ctx.BASE_DIR, active_profile.name)

        while True:
            now_iso = _utc_now_iso()
            in_sched, sched_reason, next_switch = check_operating_schedule()
            if not in_sched:
                next_switch_iso = next_switch.isoformat() if next_switch else None
                _write_status({
                    "status": "standby",
                    "reason": sched_reason,
                    "next_switch_at": next_switch_iso,
                    "profile": active_profile.name,
                    "school": ctx.get_active_provider_key(),
                    "base_url": base_url,
                    "polls_count": polls_count,
                    "last_poll_ts": now_iso,
                    "number_rollcalls_probed": probed_count,
                    "last_probed_id": last_probed_id,
                })
                await asyncio.sleep(60.0)
                continue

            # 進入上課時段，如尚未登入則執行登入
            if not ctx.has_session_cookie(session):
                print(f"[probe_daemon] 進入上課時段，正在為 {active_profile.name} 執行登入...", flush=True)
                login_res = await ctx.login(session)
                if login_res.ok and ctx.cookie_cache_enabled(ctx.CONFIG):
                    ctx.save_session_cookies(session, ctx.BASE_DIR, active_profile.name)
                if not ctx.has_session_cookie(session):
                    await asyncio.sleep(15.0)
                    continue

            polls_count += 1

            # 更新心跳狀態檔
            _write_status({
                "status": "running",
                "profile": active_profile.name,
                "school": ctx.get_active_provider_key(),
                "base_url": base_url,
                "polls_count": polls_count,
                "last_poll_ts": now_iso,
                "number_rollcalls_probed": probed_count,
                "last_probed_id": last_probed_id,
            })

            try:
                client = ctx.create_tron_http_client(session, request_ssl=ssl_setting)
                result = await client.fetch_rollcalls()
                rollcalls = result.payload.get("rollcalls") or []

                for rc in rollcalls:
                    if not isinstance(rc, dict):
                        continue

                    # 判斷是否為數字點名
                    cls_status, cls_type, _ = ctx.classify_rollcall(rc)
                    rtype = str(rc.get("attendance_type") or rc.get("type") or "").lower()
                    is_number = (cls_type == "number" or cls_status == "is_number" or "number" in rtype)

                    if not is_number:
                        continue

                    rid = str(rc.get("rollcall_id") or rc.get("id") or "").strip()
                    if not rid or rid in probed_rollcall_ids:
                        continue

                    # 發現新的數字點名！開始探測
                    probed_rollcall_ids.add(rid)
                    last_probed_id = rid
                    probed_count += 1
                    cid = str(rc.get("course_id") or rc.get("courseId") or "").strip()

                    print(f"\n[probe_daemon] [DETECT] 偵測到數字點名 # {rid} (course={cid})，開始探測各端點...", flush=True)

                    candidates = _build_candidate_endpoints(rid, cid)
                    probe_results = []
                    for ep in candidates:
                        pr = await _probe_one(session, base_url, ep, ssl_setting=ssl_setting)
                        icon = "[MATCH]" if pr["has_number_code"] else ("[OK]" if pr["http_status"] == 200 else "[FAIL]")
                        leak_tag = " [發現 number_code!]" if pr["has_number_code"] else ""
                        print(f"  {icon} [{pr['http_status']}] {pr['label']}{leak_tag}", flush=True)
                        probe_results.append(pr)
                        await asyncio.sleep(0.3)

                    leaking = [p["label"] for p in probe_results if p["has_number_code"]]
                    has_leak = bool(leaking)

                    entry = {
                        "ts": now_iso,
                        "rollcall_id": rid,
                        "course_id": cid,
                        "rollcall_type": "number",
                        "has_leak": has_leak,
                        "leaking_endpoints": leaking,
                        "raw_rollcall_fields": sorted(rc.keys()),
                        "probe_results": probe_results,
                    }
                    _append_log(entry)
                    print(f"[probe_daemon] 探測完成並寫入記錄檔！結果: {'找到洩漏' if has_leak else '各端點無直接洩漏'}\n", flush=True)

            except ctx.UnauthorizedError:
                print("[probe_daemon] Session 過期，重新登入...", flush=True)
                login_res = await ctx.login(session)
                if login_res.ok and ctx.cookie_cache_enabled(ctx.CONFIG):
                    ctx.save_session_cookies(session, ctx.BASE_DIR, active_profile.name)
            except Exception as exc:
                # 網絡波動或暫時錯誤，不崩潰
                pass

            await asyncio.sleep(_get_poll_interval())


def main() -> None:
    print("[probe_daemon] 啟動背景探針服務...", flush=True)
    while True:
        try:
            asyncio.run(run_probe_loop())
        except KeyboardInterrupt:
            print("[probe_daemon] 已手動終止", flush=True)
            break
        except Exception as exc:
            print(f"[probe_daemon] 主迴圈崩潰: {exc}，30 秒後重新啟動", flush=True)
            time.sleep(30)


if __name__ == "__main__":
    main()
