import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from troTHU import logging_runtime
from troTHU import runtime_context as ctx
from troTHU.account_runtime_store import load_runtime_state, mark_check_result


class DashboardRuntimeTest(unittest.TestCase):
    def test_unconfirmed_submission_survives_next_school_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            mark_check_result(base, 'test-student', 'submitted_unconfirmed', rollcall_id='88')
            mark_check_result(base, 'test-student', 'is_number', rollcall_id='88')
            profile = load_runtime_state(base).profiles['test-student']
        self.assertEqual(profile['last_check']['status'], 'is_number')
        self.assertEqual(profile['last_submission']['status'], 'submitted_unconfirmed')
        self.assertEqual(profile['last_submission']['rollcall_id'], '88')

    def test_crash_reason_and_restart_state_are_available_to_dashboard(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(ctx, 'BASE_DIR', Path(directory)),
            patch.object(ctx, 'get_active_profile', return_value=SimpleNamespace(name='test-student')),
            patch.object(ctx, 'log'),
            patch.object(ctx, 'log_print'),
            patch.object(ctx, 'LAST_FATAL_NOTIFICATION_AT', float('inf')),
        ):
            logging_runtime.report_fatal_exception(ValueError("reserved key 'SameSite'"), 1)
            profile = load_runtime_state(Path(directory)).profiles['test-student']
        self.assertEqual(profile['monitor_state'], 'restarting')
        self.assertEqual(profile['last_error']['status'], 'fatal_error')
        self.assertIn('SameSite', profile['last_error']['message'])
