import copy
import json
import io
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from troTHU import config_runtime, runtime_context as ctx
from troTHU.account_store import get_active_profile, switch_profile
from troTHU.environment_config import (
    EnvironmentConfigError, build_environment_config, read_environment_accounts,
)
import render_start


ACCOUNTS = [
    {"name": "primary", "user": "S100", "passwd": ' leading:# \\"value" trailing ', "school": "USC"},
    {"name": "secondary", "user": "S200", "passwd": "second-password", "school": "THU"},
]


class EnvironmentConfigTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(ctx, "CONFIG", copy.deepcopy(ctx.DEFAULT_CONFIG)))
        self.stack.enter_context(patch.object(ctx, "CONFIG_PATH", self.root / "config.yaml"))
        self.stack.enter_context(patch.object(ctx, "CONFIG_ADVANCED_PATH", self.root / "config.advanced.yaml"))
        self.stack.enter_context(patch.object(ctx, "CONFIG_BOOTSTRAPPED", False))
        self.stack.enter_context(patch.object(ctx, "BOOTSTRAP_WARNINGS", []))
        self.stack.enter_context(patch.object(ctx, "CONFIG_WARNINGS", []))

    def tearDown(self):
        ctx.CONFIG.clear()
        ctx.CONFIG.update(copy.deepcopy(ctx.DEFAULT_CONFIG))

    def configure(self, records=ACCOUNTS):
        os.environ["TRON_ACCOUNTS_JSON"] = json.dumps(records)
        return ctx.bootstrap_config(force=True)

    def test_loads_accounts_without_creating_files_or_serializing_passwords(self):
        config = self.configure()
        self.assertEqual(set(config["accounts"]["profiles"]), {"primary", "secondary"})
        self.assertEqual(config["provider"]["current"], "usc")
        self.assertEqual(ctx.effective_config_now_value(config), "S100")
        self.assertEqual(list(self.root.iterdir()), [])
        for account in ACCOUNTS:
            self.assertNotIn(account["passwd"], json.dumps(config))
        self.assertFalse(ctx.save_config())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_switching_profiles_uses_matching_credentials_and_provider(self):
        config = self.configure()
        os.environ.update(TRON_USER="old-user", TRON_PASS="old-password")
        with patch.object(ctx, "get_keyring_password", side_effect=AssertionError("must not read keyring")):
            self.assertEqual(ctx.resolve_credentials(), ("S100", ACCOUNTS[0]["passwd"], "environment"))
            switch_profile(config, "secondary")
            self.assertEqual(ctx.resolve_credentials(), ("S200", "second-password", "environment"))
        self.assertEqual(config["provider"]["current"], "thu")
        self.assertEqual(config["accounts"]["profiles"]["primary"]["school"], "usc")

    def test_explicit_profile_and_advanced_settings_are_preserved(self):
        ctx.CONFIG_ADVANCED_PATH.write_text("time:\n  timezone: UTC\noperating:\n  0:\n    enable: false\n", encoding="utf-8")
        os.environ["TRON_PROFILE"] = "secondary"
        config = self.configure()
        self.assertEqual(get_active_profile(config).name, "secondary")
        self.assertEqual(config["provider"]["current"], "thu")
        self.assertEqual(config["time"]["timezone"], "UTC")
        self.assertFalse(config["operating"][0]["enable"])

    def test_environment_does_not_parse_or_overwrite_old_account_file(self):
        original = "now: [invalid custom configuration\n"
        ctx.CONFIG_PATH.write_text(original, encoding="utf-8")
        self.configure()
        self.assertEqual(ctx.CONFIG_PATH.read_text(encoding="utf-8"), original)
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_invalid_environment_does_not_trigger_local_config_recovery(self):
        original = "now: existing\n"
        ctx.CONFIG_PATH.write_text(original, encoding="utf-8")
        os.environ["TRON_ACCOUNTS_JSON"] = '[{"passwd":"DO-NOT-PRINT"}'
        with self.assertRaises(EnvironmentConfigError) as error:
            ctx.bootstrap_config(force=True)
        self.assertNotIn("DO-NOT-PRINT", str(error.exception))
        self.assertEqual(ctx.CONFIG_PATH.read_text(encoding="utf-8"), original)
        self.assertEqual(len(list(self.root.iterdir())), 1)
        self.assertFalse(ctx.CONFIG_BOOTSTRAPPED)

    def test_legacy_pair_still_works_without_writing_passwords(self):
        os.environ.update(TRON_USER="S300", TRON_PASS="legacy-pass", TRON_SCHOOL="TKU")
        config = ctx.bootstrap_config(force=True)
        self.assertEqual(ctx.resolve_credentials(), ("S300", "legacy-pass", "environment"))
        self.assertEqual(config["provider"]["current"], "tku")
        self.assertNotIn("legacy-pass", json.dumps(config))

    def test_missing_environment_account_fails_closed(self):
        self.configure()
        os.environ["TRON_ACCOUNTS_JSON"] = json.dumps(ACCOUNTS[1:])
        with self.assertRaises(EnvironmentConfigError):
            ctx.resolve_credentials()

    def test_json_takes_precedence_over_legacy_environment(self):
        os.environ.update(TRON_USER="old", TRON_PASS="old")
        self.configure()
        self.assertEqual(ctx.resolve_credentials()[0], "S100")

    def test_unknown_profile_fails_without_echoing_input(self):
        os.environ["TRON_PROFILE"] = "DO-NOT-PRINT"
        with self.assertRaises(EnvironmentConfigError) as error:
            self.configure()
        self.assertNotIn("DO-NOT-PRINT", str(error.exception))

    def test_invalid_account_shapes_and_collisions_are_rejected(self):
        invalid = [None, {}, [], [None], [True],
            [{"user": 123, "passwd": "pw", "school": "usc"}],
            [{"user": "x", "passwd": "", "school": "usc"}],
            [{"user": "x", "passwd": "pw", "school": "unknown"}],
            [{"user": "x", "passwd": "pw", "school": "usc", "name": "../x"}],
            [{"user": "x", "passwd": "pw\n", "school": "usc"}],
            [ACCOUNTS[0], ACCOUNTS[0]],
            [ACCOUNTS[0], dict(ACCOUNTS[1], name="PRIMARY")],
            [ACCOUNTS[0], dict(ACCOUNTS[0], name="other")],
        ]
        for records in invalid:
            with self.subTest(records=type(records).__name__):
                with self.assertRaises(EnvironmentConfigError):
                    read_environment_accounts({"TRON_ACCOUNTS_JSON": json.dumps(records)})
        for env in ({"TRON_USER": "u"}, {"TRON_PASS": "p"}):
            with self.assertRaises(EnvironmentConfigError):
                read_environment_accounts(env)

    def test_secret_repr_and_worker_configuration_are_isolated(self):
        accounts = read_environment_accounts({"TRON_ACCOUNTS_JSON": json.dumps(ACCOUNTS)})
        worker = read_environment_accounts({"TRON_ACCOUNTS_JSON": accounts[0].worker_json()})
        self.assertEqual([item.name for item in worker], ["primary"])
        self.assertNotIn(ACCOUNTS[0]["passwd"], repr(accounts))
        config = build_environment_config(accounts, {}, {"TRON_MQTT_CHANNEL": "test-channel"})
        self.assertEqual(config["mqtt_channel"], "test-channel")

    def test_environment_admin_ids_override_only_discord_admins(self):
        ctx.CONFIG_ADVANCED_PATH.write_text('integrations:\n  admins:\n    discord: ["111"]\n    telegram: ["telegram-admin"]\n', encoding='utf-8')
        os.environ['TRON_DISCORD_ADMIN_IDS'] = '222, 333,222'
        config = self.configure()
        self.assertEqual(config['integrations']['admins']['discord'], ['222', '333'])
        self.assertEqual(config['integrations']['admins']['telegram'], ['telegram-admin'])
        os.environ['TRON_DISCORD_ADMIN_IDS'] = ''
        self.assertEqual(self.configure()['integrations']['admins']['discord'], [])

    def test_invalid_environment_admin_ids_are_redacted(self):
        for value in ('DO-NOT-PRINT', '123,,456', '0', '-123', '９９９'):
            with self.subTest(value=value):
                os.environ['TRON_DISCORD_ADMIN_IDS'] = value
                with self.assertRaises(EnvironmentConfigError) as error:
                    self.configure()
                self.assertIn('TRON_DISCORD_ADMIN_IDS', str(error.exception))
                self.assertNotIn('DO-NOT-PRINT', str(error.exception))

    def test_invalid_advanced_config_is_not_silently_ignored_in_environment_mode(self):
        for value in ('token: [DO-NOT-PRINT', '[1, 2]', 'false', '0'):
            with self.subTest(kind=value[:1]):
                ctx.CONFIG_ADVANCED_PATH.write_text(value, encoding='utf-8')
                with self.assertRaises(EnvironmentConfigError) as error:
                    self.configure()
                self.assertNotIn('DO-NOT-PRINT', str(error.exception))
                self.assertEqual(ctx.CONFIG_ADVANCED_PATH.read_text(encoding='utf-8'), value)
                self.assertFalse(ctx.CONFIG_PATH.exists())

    def test_check_without_environment_does_not_create_or_recover_files(self):
        for value in (None, 'token: [DO-NOT-PRINT', 'now:\naccount:\n'):
            with self.subTest(existing=value is not None):
                if value is not None:
                    ctx.CONFIG_PATH.write_text(value, encoding='utf-8')
                output = io.StringIO()
                with redirect_stderr(output), patch.object(render_start.subprocess, 'Popen') as popen:
                    result = render_start.main(['--check'])
                self.assertEqual(result, 2)
                popen.assert_not_called()
                self.assertNotIn('DO-NOT-PRINT', output.getvalue())
                if value is None:
                    self.assertEqual(list(self.root.iterdir()), [])
                else:
                    self.assertEqual(ctx.CONFIG_PATH.read_text(encoding='utf-8'), value)
                    self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_check_can_read_a_valid_local_config_without_writing_advanced_defaults(self):
        text = 'now:S100\naccount:\n  user:S100\n  passwd:example-pass\n  school:USC\n'
        ctx.CONFIG_PATH.write_text(text, encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(render_start.main(['--check']), 0)
        self.assertEqual(json.loads(output.getvalue())['processes'], ['monitor'])
        self.assertEqual(ctx.CONFIG_PATH.read_text(encoding='utf-8'), text)
        self.assertFalse(ctx.CONFIG_ADVANCED_PATH.exists())

    def test_environment_cli_mutations_do_not_modify_profiles_files_or_cookies(self):
        config = self.configure()
        original = copy.deepcopy(config)
        for command in (
            ['account', 'add', 'third', '--user', 'S300', '--password', 'do-not-save'],
            ['account', 'switch', 'secondary'],
            ['account', 'remove', 'secondary'],
            ['account', 'bind', 'discord', '123', 'primary'],
            ['account', 'unbind', 'discord', '123'],
            ['init', '--yes', '--user', 'S300', '--password', 'do-not-save'],
            ['config', 'compact', '--write', '--json'],
        ):
            with self.subTest(command=command[:2]), redirect_stdout(io.StringIO()), \
                    patch.object(ctx, 'clear_session_cookies') as remove_cookies, \
                    patch.object(ctx, 'set_keyring_password') as set_keyring:
                self.assertEqual(ctx.main(command), 1)
                remove_cookies.assert_not_called()
                set_keyring.assert_not_called()
            self.assertEqual(config, original)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_environment_credential_report_uses_requested_profile_without_switching(self):
        config = self.configure()
        os.environ['TRON_ACCOUNTS_JSON'] = json.dumps(ACCOUNTS[:1])
        with patch.object(ctx, 'get_keyring_password', side_effect=AssertionError('environment diagnostics do not read keyring')):
            primary = ctx.credential_report('primary')
            secondary = ctx.credential_report('secondary')
        self.assertEqual(primary['effective_source'], 'environment')
        self.assertEqual(secondary['effective_source'], 'missing')
        self.assertFalse(secondary['sources']['environment'])
        self.assertEqual(config['accounts']['current'], 'primary')
        self.assertNotIn('second-password', json.dumps([primary, secondary]))

    def test_legacy_credential_report_does_not_reuse_active_profile_password(self):
        ctx.CONFIG.clear()
        ctx.CONFIG.update(ctx.normalize_config({'accounts': {'current': 'first', 'profiles': {
            'first': {'user': 'S100', 'passwd': 'first-pass'},
            'second': {'user': 'S200', 'passwd': ''},
        }}}))
        with patch.object(ctx, 'get_keyring_password', return_value=''), \
                patch.object(ctx, 'get_runtime_credentials', return_value=('', '')):
            self.assertEqual(ctx.credential_report('first')['effective_source'], 'config')
            self.assertEqual(ctx.credential_report('second')['effective_source'], 'missing')

    def test_environment_account_whitespace_and_custom_name_normalization(self):
        records = [
            {"user": "  A113280040  ", "passwd": "pw1", "school": "usc", "name": "A113280040 "},
            {"user": "A113280042", "passwd": "pw2", "school": "usc", "name": "王大明"},
        ]
        accounts = read_environment_accounts({"TRON_ACCOUNTS_JSON": json.dumps(records)})
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0].name, "A113280040")
        self.assertEqual(accounts[0].user, "A113280040")
        self.assertEqual(accounts[1].name, "A113280042")
        self.assertEqual(accounts[1].label, "王大明")

    def test_environment_account_json_with_trailing_comma(self):
        raw = """[
            {"user": "A113280040", "passwd": "pw1", "school": "usc"},
            {"user": "A113280042", "passwd": "pw2", "school": "usc"},
        ]"""
        accounts = read_environment_accounts({"TRON_ACCOUNTS_JSON": raw})
        self.assertEqual(len(accounts), 2)

