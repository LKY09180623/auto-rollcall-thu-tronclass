import json
import os
import re
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from troTHU.json_storage import atomic_write_json, file_transaction
except ImportError:  # pragma: no cover - direct script fallback
    from json_storage import atomic_write_json, file_transaction


KEYRING_SERVICE = "troTHU"
PROFILE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


try:
    import keyring as _keyring  # type: ignore
except Exception:  # pragma: no cover - environment dependent
    _keyring = None


@dataclass(frozen=True)
class AccountProfile:
    name: str
    user: str = ""
    passwd: str = ""
    label: str = ""


def keyring_available() -> bool:
    return _keyring is not None


def normalize_profile_name(value: Any) -> str:
    name = PROFILE_NAME_PATTERN.sub("-", str(value or "").strip()).strip("-._")
    return name or "default"


def normalize_accounts_config(config: Dict[str, Any]) -> Dict[str, Any]:
    legacy = config.setdefault("account", {})
    if not isinstance(legacy, dict):
        legacy = {}
        config["account"] = legacy

    accounts = config.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        accounts = {}
        config["accounts"] = accounts

    raw_profiles = accounts.setdefault("profiles", {})
    if not isinstance(raw_profiles, dict):
        raw_profiles = {}

    profiles: Dict[str, Dict[str, str]] = {}
    for raw_name, raw_profile in raw_profiles.items():
        profile_name = normalize_profile_name(raw_name)
        profile = raw_profile if isinstance(raw_profile, dict) else {}
        profiles[profile_name] = {
            "user": str(profile.get("user", "") or ""),
            "passwd": str(profile.get("passwd", "") or ""),
            "label": str(profile.get("label", "") or ""),
        }
        if profile.get("school"):
            profiles[profile_name]["school"] = str(profile["school"]).strip().lower()

    legacy_user = str(legacy.get("user", "") or "")
    legacy_passwd = str(legacy.get("passwd", "") or "")
    if not profiles:
        profiles["default"] = {
            "user": legacy_user,
            "passwd": legacy_passwd,
            "label": "legacy config",
        }

    current = normalize_profile_name(accounts.get("current") or next(iter(profiles)))
    if current not in profiles:
        current = next(iter(profiles))

    accounts["current"] = current
    accounts["profiles"] = profiles
    return accounts


def get_active_profile(config: Dict[str, Any]) -> AccountProfile:
    accounts = normalize_accounts_config(config)
    current = accounts["current"]
    profile = accounts["profiles"][current]
    return AccountProfile(
        name=current,
        user=profile.get("user", ""),
        passwd=profile.get("passwd", ""),
        label=profile.get("label", ""),
    )


def list_profiles(config: Dict[str, Any]) -> List[AccountProfile]:
    accounts = normalize_accounts_config(config)
    return [
        AccountProfile(
            name=name,
            user=profile.get("user", ""),
            passwd=profile.get("passwd", ""),
            label=profile.get("label", ""),
        )
        for name, profile in accounts["profiles"].items()
    ]


def set_profile(
    config: Dict[str, Any],
    name: str,
    user: str,
    passwd: str = "",
    label: str = "",
    make_current: bool = True,
) -> AccountProfile:
    accounts = normalize_accounts_config(config)
    profile_name = normalize_profile_name(name)
    accounts["profiles"][profile_name] = {
        "user": str(user or ""),
        "passwd": str(passwd or ""),
        "label": str(label or ""),
    }
    if make_current:
        accounts["current"] = profile_name
        config.setdefault("account", {})["user"] = str(user or "")
        config.setdefault("account", {})["passwd"] = str(passwd or "")
    return get_active_profile(config) if make_current else AccountProfile(profile_name, user, passwd, label)


def remove_profile(config: Dict[str, Any], name: str) -> bool:
    accounts = normalize_accounts_config(config)
    profile_name = normalize_profile_name(name)
    if profile_name not in accounts["profiles"]:
        return False
    if len(accounts["profiles"]) == 1:
        return False
    del accounts["profiles"][profile_name]
    if accounts["current"] == profile_name:
        accounts["current"] = next(iter(accounts["profiles"]))
    active = get_active_profile(config)
    config.setdefault("account", {})["user"] = active.user
    config.setdefault("account", {})["passwd"] = active.passwd
    return True


def switch_profile(config: Dict[str, Any], name: str) -> AccountProfile:
    accounts = normalize_accounts_config(config)
    profile_name = normalize_profile_name(name)
    if profile_name not in accounts["profiles"]:
        raise KeyError(profile_name)
    accounts["current"] = profile_name
    active = get_active_profile(config)
    config.setdefault("account", {})["user"] = active.user
    config.setdefault("account", {})["passwd"] = active.passwd
    school = accounts["profiles"][profile_name].get("school")
    if school:
        config.setdefault("provider", {})["current"] = school
    return active


def get_keyring_password(profile_name: str, user: str) -> str:
    if _keyring is None or not user:
        return ""
    try:
        return _keyring.get_password(KEYRING_SERVICE, f"{profile_name}:{user}") or ""
    except Exception:
        return ""


def set_keyring_password(profile_name: str, user: str, password: str) -> bool:
    if _keyring is None or not user:
        return False
    try:
        _keyring.set_password(KEYRING_SERVICE, f"{profile_name}:{user}", password)
        return True
    except Exception:
        return False


def state_dir(base_dir: Path) -> Path:
    return base_dir / "state"


def cookie_cache_enabled(config: Dict[str, Any]) -> bool:
    session_config = config.setdefault("session", {})
    if not isinstance(session_config, dict):
        return True
    return bool(session_config.get("cache_cookies", True))


def cookie_path(base_dir: Path, profile_name: str) -> Path:
    return state_dir(base_dir) / "cookies" / f"{normalize_profile_name(profile_name)}.json"


def save_session_cookies(session: Any, base_dir: Path, profile_name: str) -> bool:
    path = cookie_path(base_dir, profile_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for cookie in getattr(session, "cookie_jar", []):
        records.append(
            {
                "key": getattr(cookie, "key", ""),
                "value": getattr(cookie, "value", ""),
                "domain": _cookie_attribute(cookie, "domain"),
                "path": _cookie_attribute(cookie, "path") or "/",
                "secure": bool(_cookie_attribute(cookie, "secure")),
                "httponly": bool(_cookie_attribute(cookie, "httponly")),
                "expires": _cookie_attribute(cookie, "expires"),
            }
        )
    with file_transaction(path):
        atomic_write_json(path, records)
    return True


def _cookie_attribute(cookie: Any, name: str) -> Any:
    try:
        return cookie[name]
    except (KeyError, TypeError):
        return ""


def load_session_cookies(session: Any, base_dir: Path, profile_name: str) -> bool:
    from yarl import URL

    path = cookie_path(base_dir, profile_name)
    if not path.exists():
        return False
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(records, list):
        return False
    restored = False
    for record in records:
        if not isinstance(record, dict):
            continue
        key = str(record.get("key", "") or "")
        value = str(record.get("value", "") or "")
        if not key or not value:
            continue
        try:
            cookies = SimpleCookie()
            cookies[key] = value
            for attribute in ("domain", "path", "expires"):
                if record.get(attribute):
                    cookies[key][attribute] = str(record[attribute])
            for attribute in ("secure", "httponly"):
                if record.get(attribute) is True:
                    cookies[key][attribute] = True
            domain = str(record.get("domain") or "").lstrip(".")
            if domain:
                response_url = URL.build(
                    scheme="https" if record.get("secure") is True else "http",
                    host=domain,
                    path=str(record.get("path") or "/"),
                )
                session.cookie_jar.update_cookies(cookies, response_url=response_url)
            else:
                # Older manual-cookie exports did not always include a domain.
                session.cookie_jar.update_cookies(cookies)
            restored = True
        except (CookieError, ValueError, TypeError):
            continue
    return restored


def clear_session_cookies(base_dir: Path, profile_name: str) -> bool:
    path = cookie_path(base_dir, profile_name)
    if not path.exists():
        return False
    with file_transaction(path):
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True
