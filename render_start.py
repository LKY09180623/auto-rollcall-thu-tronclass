"""Render entry point: isolated account processes and one optional Gateway."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

from troTHU.environment_config import EnvironmentConfigError, read_environment_accounts


ROOT = Path(__file__).resolve().parent
PAUSED_ACCOUNTS_FILE = ROOT / "paused_accounts.json"


def get_paused_accounts(environ: Mapping[str, str] | None = None) -> set[str]:
    if environ is not None and "TRON_PAUSED_ACCOUNTS" in environ:
        try:
            return set(json.loads(environ["TRON_PAUSED_ACCOUNTS"]))
        except Exception:
            return set()
    if PAUSED_ACCOUNTS_FILE.exists():
        try:
            data = json.loads(PAUSED_ACCOUNTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(data)
        except Exception:
            pass
    return set()


def save_paused_accounts(paused: set[str]) -> None:
    try:
        data = sorted(list(paused))
        PAUSED_ACCOUNTS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.environ["TRON_PAUSED_ACCOUNTS"] = json.dumps(data, ensure_ascii=False)
    except Exception:
        pass


@dataclass(frozen=True)
class ProcessSpec:
    name: str
    command: tuple[str, ...]
    environment: dict[str, str] = field(repr=False)


def build_process_specs(config: dict, environ: Mapping[str, str] | None = None) -> list[ProcessSpec]:
    env = dict(os.environ if environ is None else environ)
    if environ is None:
        dynamic_file = ROOT / "dynamic_accounts.json"
        if dynamic_file.exists():
            try:
                dyn_list = json.loads(dynamic_file.read_text(encoding="utf-8"))
                if isinstance(dyn_list, list) and dyn_list:
                    base_list = json.loads(env.get("TRON_ACCOUNTS_JSON", "[]"))
                    existing_users = {a.get("user") for a in base_list}
                    for da in dyn_list:
                        if da.get("user") not in existing_users:
                            base_list.append(da)
                    merged_str = json.dumps(base_list, ensure_ascii=False)
                    env["TRON_ACCOUNTS_JSON"] = merged_str
                    os.environ["TRON_ACCOUNTS_JSON"] = merged_str
            except Exception:
                pass
    accounts = read_environment_accounts(env)
    paused_set = get_paused_accounts(environ) if environ is not None else get_paused_accounts()
    specs = []
    for account in accounts:
        if account.name in paused_set:
            continue
        worker_env = dict(env)
        # Each monitor can see only its own profile, including relay callbacks.
        worker_env["TRON_ACCOUNTS_JSON"] = account.worker_json()
        worker_env["TRON_PROFILE"] = account.name
        worker_env.pop("TRON_USER", None)
        worker_env.pop("TRON_PASS", None)
        specs.append(ProcessSpec("monitor:" + account.name,
            (sys.executable, "-m", "troTHU.tron", "run", "--no-input"), worker_env))
    if not accounts:
        specs.append(ProcessSpec("monitor", (sys.executable, "-m", "troTHU.tron", "run", "--no-input"), env))

    discord = config.get("integrations", {}).get("discord", {})
    token_present = bool(env.get(discord.get("token_env", "DISCORD_BOT_TOKEN"), "").strip())
    enabled = env.get("TRON_DISCORD_GATEWAY_ENABLED", "auto").strip().lower()
    if enabled not in {"auto", "true", "false", "1", "0"}:
        raise EnvironmentConfigError("TRON_DISCORD_GATEWAY_ENABLED must be auto, true, or false.")
    gateway_enabled = token_present if enabled == "auto" else enabled in {"true", "1"}
    if gateway_enabled and not token_present:
        raise EnvironmentConfigError("Discord Gateway is enabled but its bot token is missing.")
    if gateway_enabled:
        specs.append(ProcessSpec("discord-gateway", (sys.executable, "-m", "troTHU.tron", "bot", "discord-gateway"), env))

    probe_raw = env.get("TRON_PROBE_DAEMON_ENABLED", "auto").strip().lower()
    if probe_raw not in {"auto", "true", "false", "1", "0"}:
        raise EnvironmentConfigError("TRON_PROBE_DAEMON_ENABLED must be auto, true, or false.")
    probe_enabled = (bool(env.get("RENDER") or env.get("RENDER_EXTERNAL_URL")) if probe_raw == "auto" else probe_raw in {"true", "1"})
    probe_file = ROOT / "probe_daemon.py"
    if probe_enabled and probe_file.exists():
        specs.append(ProcessSpec("probe-daemon", (sys.executable, str(probe_file)), env))
    return specs


class ProcessSupervisor:
    def __init__(self, specs: list[ProcessSpec]) -> None:
        self.specs = specs
        self.children: dict[str, subprocess.Popen] = {}
        self.failures: dict[str, int] = {}
        self.started_at: dict[str, float] = {}
        self.retry_at: dict[str, float] = {}
        self.lock = threading.Lock()

    def health(self) -> dict:
        with self.lock:
            running = sum(child.poll() is None for child in self.children.values())
            return {"status": "ok" if running == len(self.specs) else "degraded",
                    "expected_processes": len(self.specs), "running_processes": running}

    def add_spec(self, spec: ProcessSpec) -> bool:
        with self.lock:
            if any(s.name == spec.name for s in self.specs):
                return False
            self.specs.append(spec)
            return True

    def remove_spec(self, name: str) -> bool:
        with self.lock:
            spec = next((s for s in self.specs if s.name == name or s.name == f"monitor:{name}"), None)
            if not spec:
                return False
            self.specs.remove(spec)
            child = self.children.pop(spec.name, None)
            if child and child.poll() is None:
                try:
                    child.terminate()
                except Exception:
                    pass
            return True

    def tick(self, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        with self.lock:
            for spec in self.specs:
                child = self.children.get(spec.name)
                if child is not None:
                    code = child.poll()
                    if code is None:
                        continue
                    del self.children[spec.name]
                    failures = 0 if moment - self.started_at[spec.name] >= 60 else self.failures.get(spec.name, 0)
                    self.failures[spec.name] = failures + 1
                    delay = min(60, 3 * 2 ** min(failures, 5))
                    self.retry_at[spec.name] = moment + delay
                    print("{} exited ({}); retry in {}s.".format(spec.name, code, delay), flush=True)
                if moment < self.retry_at.get(spec.name, 0):
                    continue
                try:
                    child = subprocess.Popen(list(spec.command), cwd=ROOT, env=spec.environment)
                except OSError:
                    self.retry_at[spec.name] = moment + 60
                    print("Could not start {}; retry in 60s.".format(spec.name), flush=True)
                    continue
                self.children[spec.name] = child
                self.started_at[spec.name] = moment
                print("Started {}.".format(spec.name), flush=True)

    def stop(self) -> None:
        with self.lock:
            children = list(self.children.values())
        for child in children:
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 10
        for child in children:
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def make_handler(supervisor: ProcessSupervisor):
    class DashboardHandler(BaseHTTPRequestHandler):
        def _write_response(
            self,
            status: int,
            content_type: str,
            body: bytes,
            cache_control: str = "no-store",
            extra_headers: dict[str, str] | None = None,
        ):
            accept_enc = self.headers.get("Accept-Encoding", "") if hasattr(self, "headers") and self.headers else ""
            use_gzip = "gzip" in accept_enc and len(body) > 400
            if use_gzip:
                body = gzip.compress(body)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            if use_gzip:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Cache-Control", cache_control)
            self.send_header("Content-Length", str(len(body)))
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, data: dict, status: int = 200):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._write_response(
                status,
                "application/json; charset=utf-8",
                body,
                cache_control="no-store",
                extra_headers={
                    "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
                },
            )

        def _send_js(self, file_path: Path):
            if file_path.is_file():
                body = file_path.read_bytes()
                host = self.headers.get("Host") or "tronclass-bot.onrender.com"
                proto = self.headers.get("X-Forwarded-Proto", "https" if "onrender.com" in host else "http")
                base_url = f"{proto}://{host}"
                body = body.replace(b"__BACKEND_URL__", base_url.encode("utf-8"))
                self._write_response(200, "application/javascript; charset=utf-8", body, cache_control="no-cache")
            else:
                self.send_error(404)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_PUT(self):
            self.do_POST()

        def _send_html(self, file_path: Path):
            if file_path.is_file():
                body = file_path.read_bytes()
                if file_path.name == "index.html" and file_path.parent.name == "scanner_app":
                    channel = os.environ.get("TRON_MQTT_CHANNEL", "").strip() or "A113280040_secret"
                    replacement = json.dumps(channel).replace("<", "\\u003c").replace(">", "\\u003e")
                    body = body.replace(b'"A113280040_secret"', replacement.encode("utf-8"))
                self._write_response(200, "text/html; charset=utf-8", body, cache_control="no-cache")
            else:
                self.send_error(404)

        def _parse_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except Exception:
                return {}

        def do_GET(self):
            if self.path == "/test_qr.png":
                img_file = ROOT / "scanner_app" / "test_qr.png"
                if img_file.is_file():
                    body = img_file.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                return self.send_error(404)

            if self.path in ("/proxy.js", "/api/proxy.js", "/scripts/proxy_rollcall_relay.js"):
                return self._send_js(ROOT / "scripts" / "proxy_rollcall_relay.js")

            if self.path in ("/scanner", "/scanner/"):
                return self._send_html(ROOT / "scanner_app" / "index.html")

            if self.path == "/":
                accept = self.headers.get("Accept", "")
                if "application/json" in accept and "text/html" not in accept:
                    health = supervisor.health()
                    return self._send_json(health, 200 if health["status"] == "ok" else 503)
                return self._send_html(ROOT / "scanner_app" / "index.html")

            if self.path in ("/health", "/healthz"):
                health = supervisor.health()
                return self._send_json(health, 200 if health["status"] == "ok" else 503)

            if self.path == "/api/status":
                try:
                    from troTHU import runtime_context as ctx
                    from troTHU.account_runtime_store import load_runtime_state, runtime_profile_summary
                    from troTHU.pending_qr import list_pending_qr
                    config = ctx.load_config(read_only=True)
                    runtime_state = load_runtime_state(ROOT)
                    profiles = config.get("accounts", {}).get("profiles", {})
                    paused_set = get_paused_accounts()
                    accounts_data = []
                    for name in profiles:
                        p_summary = runtime_profile_summary(runtime_state, name)
                        cookie = ctx.cookie_report(name)
                        pending = [item.to_dict() for item in list_pending_qr(ROOT) if item.profile == name]
                        is_paused = name in paused_set
                        accounts_data.append({
                            "name": name,
                            "paused": is_paused,
                            "monitor_state": "paused" if is_paused else (p_summary.get("monitor_state") or "running"),
                            "cookie_valid": bool(cookie.get("exists") and cookie.get("valid")),
                            "last_check": p_summary.get("last_check") or {},
                            "last_login": p_summary.get("last_login") or {},
                            "last_error": p_summary.get("last_error") or {},
                            "pending_count": len(pending),
                            "heartbeat_stale": bool(p_summary.get("heartbeat_stale")),
                        })
                    return self._send_json({"status": "ok", "supervisor": supervisor.health(), "accounts": accounts_data})
                except Exception as exc:
                    return self._send_json({"status": "error", "error": str(exc), "accounts": [], "supervisor": supervisor.health()}, 200)

            if self.path in ("/api/probe_log", "/api/probe_log/"):
                status_path = ROOT / "probe_status.json"
                log_path = ROOT / "probe_log.jsonl"
                daemon_status = {}
                if status_path.exists():
                    try:
                        daemon_status = json.loads(status_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                entries = []
                if log_path.exists():
                    try:
                        entries = [
                            json.loads(line)
                            for line in log_path.read_text(encoding="utf-8").splitlines()
                            if line.strip()
                        ]
                    except Exception:
                        pass
                total_count = len(entries)
                entries = entries[-15:]
                return self._send_json({
                    "status": "ok",
                    "daemon_status": daemon_status,
                    "entries": entries,
                    "count": total_count,
                })

            self.send_error(404)

        def do_POST(self):
            payload_data = self._parse_body()
            profile_target = payload_data.get("profile", "all")

            rollcall_match = re.search(r"/api/rollcall/(\d+)", self.path)
            if rollcall_match:
                payload_data.setdefault("rollcallId", rollcall_match.group(1))

            original_ctx_config = None
            try:
                from troTHU import runtime_context as ctx
                from troTHU.bot_handlers import BotHandlerBridge
                config = ctx.load_config(read_only=True)
                original_ctx_config = ctx.CONFIG
                ctx.CONFIG = config
                bridge = BotHandlerBridge(config, base_dir=ROOT)
                profiles = list(config.get("accounts", {}).get("profiles", {}).keys())
                targets = [profile_target] if profile_target and profile_target != "all" else profiles
                if not targets and profiles:
                    targets = profiles

                cmd = SimpleNamespace(payload={})

                if self.path == "/api/submit" or rollcall_match:
                    user_payload = str(payload_data.get("payload", "")).strip()
                    if not user_payload:
                        if ("rollcallId" in payload_data or "rollcall_id" in payload_data) and "data" in payload_data:
                            user_payload = json.dumps(payload_data)
                        else:
                            user_payload = str(payload_data.get("data") or payload_data.get("token") or payload_data.get("qr") or payload_data.get("code") or "").strip()
                    if not user_payload:
                        return self._send_json({"ok": False, "message": "內容不能為空"}, 400)
                    expected_type = str(payload_data.get("expectedType") or "").strip()
                    expected_rollcall_id = str(payload_data.get("expectedRollcallId") or "").strip()
                    if expected_type or expected_rollcall_id:
                        if (expected_type not in {"number", "qrcode"}
                                or not expected_rollcall_id.isascii() or not expected_rollcall_id.isdecimal()
                                or not isinstance(profile_target, str) or profile_target not in profiles):
                            return self._send_json({"ok": False, "message": "請選擇目前點名的帳號與活動後再送出。"}, 400)
                        cmd.payload = {"expected_type": expected_type, "expected_rollcall_id": expected_rollcall_id}
                    results = []
                    any_ok = False
                    msgs = []
                    paused_set = get_paused_accounts()

                    async def _run_all_submits():
                        out = []
                        for p in targets:
                            if p in paused_set:
                                out.append({"profile": p, "result": {"ok": False, "status": "paused", "reply": "帳號已暫停，略過簽到"}})
                                continue
                            try:
                                res = await bridge.qr_submit(profile=p, payload=user_payload, command=cmd)
                                out.append({"profile": p, "result": res})
                            except Exception as exc:
                                out.append({"profile": p, "result": {"ok": False, "status": "error", "reply": str(exc)}})
                        return out

                    sub_results = asyncio.run(_run_all_submits())
                    for item in sub_results:
                        p = item["profile"]
                        res = item["result"]
                        results.append(item)
                        if res.get("ok"):
                            any_ok = True
                        msgs.append("{}: {}".format(p, res.get("reply") or res.get("status")))
                    return self._send_json({"ok": any_ok, "message": "; ".join(msgs), "details": results})

                elif self.path == "/api/force_check":
                    results = []
                    msgs = []
                    for p in targets:
                        res = asyncio.run(bridge.force_check(profile=p, command=cmd, admin=True))
                        results.append({"profile": p, "result": res})
                        msgs.append("{}: {}".format(p, res.get("reply") or res.get("status")))
                    return self._send_json({"ok": True, "message": "; ".join(msgs), "details": results})

                elif self.path == "/api/reauth":
                    results = []
                    any_ok = False
                    msgs = []
                    for p in targets:
                        res = asyncio.run(bridge.reauth(profile=p, command=cmd, admin=True))
                        results.append({"profile": p, "result": res})
                        if res.get("ok"):
                            any_ok = True
                        msgs.append("{}: {}".format(p, res.get("reply") or res.get("status")))
                    return self._send_json({"ok": any_ok, "message": "; ".join(msgs), "details": results})

                elif self.path == "/api/account/add":
                    user = str(payload_data.get("user") or "").strip()
                    passwd = str(payload_data.get("passwd") or "").strip()
                    school = str(payload_data.get("school") or "usc").strip().lower()
                    name = str(payload_data.get("name") or user).strip()
                    if not user or not passwd:
                        return self._send_json({"ok": False, "message": "帳號與密碼為必填"}, 400)

                    existing_raw = os.environ.get("TRON_ACCOUNTS_JSON", "[]")
                    try:
                        acc_list = json.loads(existing_raw)
                        if not isinstance(acc_list, list):
                            acc_list = []
                    except Exception:
                        acc_list = []

                    if any(a.get("name") == name or a.get("user") == user for a in acc_list):
                        return self._send_json({"ok": False, "message": f"帳號 {user} 已存在清單中"}, 400)

                    new_record = {"name": name, "user": user, "passwd": passwd, "school": school}
                    acc_list.append(new_record)
                    new_json_str = json.dumps(acc_list, ensure_ascii=False)
                    os.environ["TRON_ACCOUNTS_JSON"] = new_json_str

                    try:
                        (ROOT / "dynamic_accounts.json").write_text(new_json_str, encoding="utf-8")
                    except Exception:
                        pass

                    worker_env = dict(os.environ)
                    worker_env["TRON_ACCOUNTS_JSON"] = json.dumps([new_record], ensure_ascii=False)
                    worker_env["TRON_PROFILE"] = name
                    worker_env.pop("TRON_USER", None)
                    worker_env.pop("TRON_PASS", None)
                    spec = ProcessSpec("monitor:" + name, (sys.executable, "-m", "troTHU.tron", "run", "--no-input"), worker_env)
                    supervisor.add_spec(spec)

                    return self._send_json({
                        "ok": True,
                        "message": f"成功加入帳號 {user} 並啟動即時監控！",
                        "accounts_json": new_json_str,
                        "account": new_record,
                        "total_accounts": len(acc_list),
                    })

                elif self.path == "/api/account/toggle_pause":
                    target = str(payload_data.get("name") or payload_data.get("user") or "").strip()
                    if not target:
                        return self._send_json({"ok": False, "message": "缺少目標帳號名稱"}, 400)

                    paused_set = get_paused_accounts()
                    explicit_pause = payload_data.get("paused")
                    if isinstance(explicit_pause, bool):
                        now_paused = explicit_pause
                    else:
                        now_paused = target not in paused_set

                    if now_paused:
                        paused_set.add(target)
                        save_paused_accounts(paused_set)
                        supervisor.remove_spec(target)
                        msg = f"帳號 {target} 已暫停監控與代簽"
                    else:
                        paused_set.discard(target)
                        save_paused_accounts(paused_set)
                        env_accounts = read_environment_accounts(os.environ)
                        target_acc = next((a for a in env_accounts if a.name == target), None)
                        if target_acc:
                            worker_env = dict(os.environ)
                            worker_env["TRON_ACCOUNTS_JSON"] = target_acc.worker_json()
                            worker_env["TRON_PROFILE"] = target_acc.name
                            worker_env.pop("TRON_USER", None)
                            worker_env.pop("TRON_PASS", None)
                            spec = ProcessSpec("monitor:" + target_acc.name,
                                (sys.executable, "-m", "troTHU.tron", "run", "--no-input"), worker_env)
                            supervisor.add_spec(spec)
                        msg = f"帳號 {target} 已恢復啟用"

                    return self._send_json({
                        "ok": True,
                        "name": target,
                        "paused": now_paused,
                        "message": msg,
                        "paused_accounts": sorted(list(paused_set)),
                    })

                elif self.path == "/api/account/remove":
                    target = str(payload_data.get("name") or payload_data.get("user") or "").strip()
                    if not target:
                        return self._send_json({"ok": False, "message": "缺少目標帳號名稱"}, 400)

                    existing_raw = os.environ.get("TRON_ACCOUNTS_JSON", "[]")
                    try:
                        acc_list = json.loads(existing_raw)
                        if not isinstance(acc_list, list):
                            acc_list = []
                    except Exception:
                        acc_list = []

                    new_list = [a for a in acc_list if a.get("name") != target and a.get("user") != target]
                    if len(new_list) == len(acc_list):
                        return self._send_json({"ok": False, "message": f"找不到帳號 {target}"}, 404)

                    new_json_str = json.dumps(new_list, ensure_ascii=False)
                    os.environ["TRON_ACCOUNTS_JSON"] = new_json_str
                    try:
                        (ROOT / "dynamic_accounts.json").write_text(new_json_str, encoding="utf-8")
                    except Exception:
                        pass

                    supervisor.remove_spec(target)
                    return self._send_json({
                        "ok": True,
                        "message": f"帳號 {target} 已移除並停止監控",
                        "accounts_json": new_json_str,
                        "total_accounts": len(new_list),
                    })

                self.send_error(404)
            except Exception as exc:
                return self._send_json({"ok": False, "message": str(exc)}, 500)
            finally:
                if original_ctx_config is not None:
                    ctx.CONFIG = original_ctx_config

        def log_message(self, *_args):
            pass

    return DashboardHandler


def _self_ping_loop(interval: int = 180) -> None:
    """Hit own external URL every *interval* seconds to prevent Render free-tier spin-down."""
    import urllib.request
    import urllib.error

    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/") or "https://tronclass-bot.onrender.com"
    target = url + "/healthz"
    # Give the HTTP server a moment to bind.
    time.sleep(20)
    print("Self-ping enabled: {} every {}s".format(target, interval), flush=True)

    while True:
        try:
            req = urllib.request.Request(
                target,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KeepAlive/1.0"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except Exception as exc:
            print("Self-ping failed: {}".format(exc), flush=True)
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate startup without starting processes or connecting.")
    args = parser.parse_args(argv)

    specs = []
    try:
        from troTHU import runtime_context as ctx
        config = ctx.load_config(read_only=True)
        if not ctx.effective_config_now_value(config):
            raise EnvironmentConfigError('Select an account in config.yaml or configure environment accounts before startup.')
        specs = build_process_specs(config)
        port = int(os.environ.get("PORT", "10000"))
        if not 1 <= port <= 65535:
            raise ValueError
    except EnvironmentConfigError as exc:
        print("Configuration error: {}".format(exc), file=sys.stderr)
        if args.check:
            return 2
        print("Starting web service in standalone recovery mode...", flush=True)
        specs = []
        port = int(os.environ.get("PORT", "10000")) if os.environ.get("PORT", "").isdigit() else 10000
    except ValueError:
        print("PORT must be an integer between 1 and 65535.", file=sys.stderr)
        if args.check:
            return 2
        port = 10000
    if args.check:
        print(json.dumps({"status": "ok", "processes": [spec.name for spec in specs],
                          "gateway_count": sum(spec.name == "discord-gateway" for spec in specs),
                          "port": port, "connects": False}, ensure_ascii=False))
        return 0

    supervisor = ProcessSupervisor(specs)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(supervisor))
    stop = threading.Event()
    previous_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, lambda *_args: stop.set())
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    ping_thread = threading.Thread(target=_self_ping_loop, daemon=True)
    ping_thread.start()
    try:
        while not stop.is_set():
            supervisor.tick()
            stop.wait(0.5)
    finally:
        supervisor.stop()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
