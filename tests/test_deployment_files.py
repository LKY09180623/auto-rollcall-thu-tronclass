import importlib.util
import re
import shlex
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
HELPERS = ('find_discord_user.py', 'test_discord.py', 'test_discord2.py')


class DeploymentFilesTest(unittest.TestCase):
    def test_compose_has_one_supervisor_and_no_embedded_credentials(self):
        compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        self.assertEqual(len(compose['services']), 1)
        service = compose['services']['auto-rollcall']
        self.assertNotIn('command', service)
        environment = service['environment']
        self.assertIn('TRON_ACCOUNTS_JSON', environment)
        self.assertIn('TRON_DISCORD_ADMIN_IDS', environment)
        self.assertIn('DISCORD_BOT_TOKEN', environment)
        self.assertTrue(all('=' not in entry for entry in environment))
        advanced = next(mount for mount in service['volumes']
                        if isinstance(mount, dict) and mount['target'] == '/app/config.advanced.yaml')
        self.assertTrue(advanced['read_only'])
        self.assertFalse(advanced['bind']['create_host_path'])

    def test_image_copies_only_runtime_source_and_credential_free_template(self):
        sources = set()
        for line in (ROOT / 'Dockerfile').read_text(encoding='utf-8').splitlines():
            tokens = shlex.split(line)
            if tokens and tokens[0] == 'COPY':
                sources.update(tokens[1:-1])
        self.assertEqual(sources, {'requirements.txt', 'troTHU', 'scanner_app', 'scripts',
                                  'probe_daemon.py', 'render_start.py', 'config.advanced.example.yaml'})
        rules = [line.strip() for line in (ROOT / '.dockerignore').read_text(encoding='utf-8').splitlines()
                 if line.strip() and not line.startswith('#')]
        self.assertEqual(rules[0], '**')
        self.assertNotIn('!config.advanced.yaml', rules)
        self.assertNotIn('!config.yaml', rules)
        self.assertNotIn('!.env', rules)
        config = yaml.safe_load((ROOT / 'config.advanced.example.yaml').read_text(encoding='utf-8'))
        self.assertEqual(config['integrations']['admins']['discord'], [])
        self.assertEqual(config['integrations']['discord']['token_env'], 'DISCORD_BOT_TOKEN')

    def test_token_literals_are_absent_from_deployment_and_legacy_helpers(self):
        pattern = re.compile(r'[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}')
        for name in (*HELPERS, 'docker-compose.yml', '.env.example', 'config.advanced.example.yaml'):
            with self.subTest(file=name):
                # Report only the filename, never matched credential values.
                self.assertFalse(bool(pattern.search((ROOT / name).read_text(encoding='utf-8'))))

    def test_importing_legacy_helpers_does_not_run_discord_requests(self):
        with patch('asyncio.run', side_effect=AssertionError('import must not execute network helpers')):
            for index, name in enumerate(HELPERS):
                spec = importlib.util.spec_from_file_location('deployment_helper_{}'.format(index), ROOT / name)
                spec.loader.exec_module(importlib.util.module_from_spec(spec))

