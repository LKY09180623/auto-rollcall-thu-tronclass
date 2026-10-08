import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_discord_gateway import gateway_config, interaction
from tests.test_environment_config import ACCOUNTS
from troTHU import runtime_context as ctx
from troTHU.account_runtime_store import update_profile_runtime_state
from troTHU.bot_handlers import create_bot_runtime
from troTHU.discord_adapter import interaction_to_command
from troTHU.environment_config import build_environment_config, read_environment_accounts


class MultiAccountStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_worker_pending_lookup_does_not_select_another_profiles_records(self):
        env = {"TRON_ACCOUNTS_JSON": json.dumps(ACCOUNTS[:1])}
        config = ctx.normalize_config(build_environment_config(read_environment_accounts(env), {}, env))
        selected = []

        async def record_selection(profile, _payload):
            selected.append(profile)
            return 0

        with patch.object(ctx, "CONFIG", config), \
                patch.object(ctx, "build_qr_preview", return_value={"ok": True, "rollcall_id": "test"}), \
                patch.object(ctx, "match_pending_qr", return_value=[
                    SimpleNamespace(profile="secondary", provider="usc"),
                    SimpleNamespace(profile="primary", provider="usc"),
                ]):
            result = await ctx.qr_fanout_result("test-input", submit_profile=record_selection)
        self.assertEqual(selected, ["primary"])
        self.assertEqual(result["match_count"], 1)

    async def test_all_aggregates_local_monitors_and_preserves_authorization(self):
        env = {"TRON_ACCOUNTS_JSON": json.dumps(ACCOUNTS)}
        accounts = read_environment_accounts(env)
        config = ctx.normalize_config(build_environment_config(accounts, gateway_config(), env))
        config["integrations"]["bindings"] = {
            "discord:user-2": {"adapter": "discord", "external_user_id": "user-2", "profile": "primary", "channel_id": "chan-1"},
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(ctx, "CONFIG", config), patch.dict(os.environ, env):
            root = Path(directory)
            update_profile_runtime_state(root, "primary", monitor_state="running", heartbeat_at=time.time())
            update_profile_runtime_state(root, "secondary", monitor_state="running", heartbeat_at=time.time() - 600)
            original_provider = copy.deepcopy(config["provider"])
            runtime = create_bot_runtime(config, base_dir=root)
            command = interaction_to_command(interaction("all"))
            result = await runtime.handle_command(command, channel_id="chan-1")
            self.assertTrue(result.ok)
            self.assertEqual(result.data["visible_count"], 2)
            self.assertIn("primary", result.reply)
            self.assertIn("secondary", result.reply)
            self.assertIn("monitor running", result.reply)
            self.assertIn("heartbeat stale", result.reply)
            self.assertEqual(config["accounts"]["current"], "primary")
            self.assertEqual(config["provider"], original_provider)
            self.assertNotIn("second-password", json.dumps(result.to_dict()))

            user_payload = interaction("all")
            user_payload["member"]["user"]["id"] = "user-2"
            user_result = await runtime.handle_command(interaction_to_command(user_payload), channel_id="chan-1")
            self.assertTrue(user_result.ok)
            self.assertEqual(user_result.data["visible_count"], 1)
            self.assertNotIn("secondary", user_result.reply)
            user_payload["member"]["user"]["id"] = "unbound"
            denied = await runtime.handle_command(interaction_to_command(user_payload), channel_id="chan-1")
            self.assertFalse(denied.ok)
