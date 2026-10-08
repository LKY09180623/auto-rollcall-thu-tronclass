"""Validated account configuration from deployment environment variables.

Passwords stay in the environment, outside the serializable configuration.
This module performs no I/O and never includes input values in error messages.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

try:
    from troTHU.account_store import normalize_profile_name
    from troTHU.providers import PROVIDERS, normalize_provider_name
except ImportError:  # pragma: no cover - direct script fallback
    from account_store import normalize_profile_name
    from providers import PROVIDERS, normalize_provider_name


class EnvironmentConfigError(RuntimeError):
    """A deployment setting is invalid; local config recovery must not run."""


@dataclass(frozen=True)
class EnvironmentAccount:
    name: str
    user: str
    school: str
    passwd: str = field(repr=False)
    label: str = ""

    def public_profile(self) -> dict[str, str]:
        return {"user": self.user, "passwd": "", "school": self.school, "label": self.label}

    def worker_json(self) -> str:
        return json.dumps([{
            "name": self.name, "user": self.user, "passwd": self.passwd,
            "school": self.school, "label": self.label,
        }], ensure_ascii=False)


def read_environment_accounts(environ: Mapping[str, str] | None = None) -> list[EnvironmentAccount]:
    env = os.environ if environ is None else environ
    if "TRON_ACCOUNTS_JSON" in env:
        raw_val = env["TRON_ACCOUNTS_JSON"].strip()
        try:
            records = json.loads(raw_val)
        except (ValueError, TypeError, RecursionError):
            cleaned = re.sub(r",\s*([\]}])", r"\1", raw_val)
            try:
                records = json.loads(cleaned)
            except Exception:
                raise EnvironmentConfigError("TRON_ACCOUNTS_JSON must contain valid JSON.") from None
        if not isinstance(records, list) or not records:
            raise EnvironmentConfigError("TRON_ACCOUNTS_JSON must be a non-empty array of accounts.")
    elif env.get("TRON_USER") or env.get("TRON_PASS"):
        if not env.get("TRON_USER") or not env.get("TRON_PASS"):
            raise EnvironmentConfigError("TRON_USER and TRON_PASS must both be set.")
        records = [{"user": env["TRON_USER"], "passwd": env["TRON_PASS"],
                    "school": env.get("TRON_SCHOOL", "usc")}]
    else:
        return []

    accounts = []
    names: set[str] = set()
    identities: set[tuple[str, str]] = set()
    for index, record in enumerate(records, 1):
        prefix = "Environment account entry {}".format(index)
        if not isinstance(record, dict):
            raise EnvironmentConfigError(prefix + " must be an object.")
        for key in ("user", "passwd", "school"):
            value = record.get(key)
            if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
                raise EnvironmentConfigError(prefix + ": " + key + " must be a non-empty string without control characters.")
        user = record["user"].strip()
        raw_name = record.get("name")
        if raw_name is not None and not isinstance(raw_name, str):
            raise EnvironmentConfigError(prefix + ": name must be a valid profile name (up to 80 characters).")
        name_str = (raw_name if raw_name is not None else user).strip()
        if not name_str or len(name_str) > 80 or any(c in name_str for c in ("\0", "\r", "\n", "..", "/", "\\")):
            raise EnvironmentConfigError(prefix + ": name must be a valid profile name (up to 80 characters).")
        norm_name = normalize_profile_name(name_str)
        if norm_name and norm_name != "default":
            name = norm_name
        else:
            norm_user = normalize_profile_name(user)
            name = norm_user if norm_user and norm_user != "default" else "account_{}".format(index)
        school = normalize_provider_name(record["school"])
        if school not in PROVIDERS:
            raise EnvironmentConfigError(prefix + ": school is not a supported provider.")
        if name.casefold() in names or (school, user.casefold()) in identities:
            raise EnvironmentConfigError(prefix + ": duplicate profile name or school/account pair.")
        raw_label = record.get("label")
        if raw_label is None:
            label = name_str if name_str != name else school.upper()
        else:
            if not isinstance(raw_label, str) or len(raw_label) > 120 or any(ord(c) < 32 for c in raw_label):
                raise EnvironmentConfigError(prefix + ": label must be a string of up to 120 characters without control characters.")
            label = raw_label.strip() or school.upper()
        names.add(name.casefold())
        identities.add((school, user.casefold()))
        accounts.append(EnvironmentAccount(name, user, school, record["passwd"], label))
    return accounts


def build_environment_config(
    accounts: list[EnvironmentAccount],
    advanced: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    selected = env.get("TRON_PROFILE", "").strip() or accounts[0].name
    active = next((account for account in accounts if account.name == selected), None)
    if active is None:
        raise EnvironmentConfigError("TRON_PROFILE does not match a configured environment account.")
    config = copy.deepcopy(dict(advanced))
    config["accounts"] = {"current": active.name, "profiles": {
        account.name: account.public_profile() for account in accounts
    }}
    config["account"] = {"user": active.user, "passwd": ""}
    provider = config.get("provider", {})
    config["provider"] = dict(provider) if isinstance(provider, dict) else {}
    config["provider"]["current"] = active.school
    # The console uses _simple.now even when there is no config.yaml on disk.
    config["_simple"] = {"now": active.user, "accounts": [
        account.public_profile() for account in accounts
    ], "groups": []}
    config["_environment_accounts"] = [account.name for account in accounts]
    if "TRON_DISCORD_ADMIN_IDS" in env:
        text = env["TRON_DISCORD_ADMIN_IDS"].strip()
        admins = [item.strip() for item in text.split(",")] if text else []
        if any(not re.fullmatch(r"[0-9]{1,20}", item) or int(item) == 0 for item in admins):
            raise EnvironmentConfigError("TRON_DISCORD_ADMIN_IDS must contain comma-separated Discord user IDs.")
        integrations = config.setdefault("integrations", {})
        if not isinstance(integrations, dict):
            raise EnvironmentConfigError("integrations must be a mapping in config.advanced.yaml.")
        admin_config = integrations.setdefault("admins", {})
        if not isinstance(admin_config, dict):
            raise EnvironmentConfigError("integrations.admins must be a mapping in config.advanced.yaml.")
        admin_config["discord"] = list(dict.fromkeys(admins))
    if "TRON_MQTT_CHANNEL" in env:
        config["mqtt_channel"] = env["TRON_MQTT_CHANNEL"].strip()
    if "operating" not in config:
        config["operating"] = {
            0: {"enable": True, "range": ["07:00", "18:00"]},
            1: {"enable": True, "range": ["07:00", "18:00"]},
            2: {"enable": True, "range": ["07:00", "18:00"]},
            3: {"enable": True, "range": ["07:00", "18:00"]},
            4: {"enable": True, "range": ["07:00", "18:00"]},
            5: {"enable": False, "range": ["00:00", "00:00"]},
            6: {"enable": False, "range": ["00:00", "00:00"]},
        }
    return config


def environment_credentials(config: Mapping[str, Any], profile: Any) -> tuple[str, str]:
    if profile.name not in config.get("_environment_accounts", []):
        raise EnvironmentConfigError("The active profile is not an environment account.")
    for account in read_environment_accounts():
        if account.name == profile.name and account.user == profile.user:
            return account.user, account.passwd
    raise EnvironmentConfigError("The active account is missing from the environment; reload the configuration.")
