import multiprocessing
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from troTHU.account_runtime_store import load_runtime_state, update_profile_runtime_state
from troTHU.pending_qr import add_pending_qr, list_pending_qr, remove_pending_qr
from troTHU import json_storage


def write_shared_state(directory, worker, start):
    root = Path(directory)
    start.wait(10)
    for index in range(10):
        update_profile_runtime_state(root, "shared", **{"worker_{}_{}".format(worker, index): index})
        update_profile_runtime_state(root, "profile-{}".format(worker), counter=index)
        add_pending_qr(root, profile="profile-{}".format(worker), rollcall_id=str(index), ttl_seconds=120)
    for index in range(5):
        remove_pending_qr(root, profile="profile-{}".format(worker), rollcall_id=str(index))


class MultiProcessStateTest(unittest.TestCase):
    @unittest.skipUnless(json_storage.os.name == "nt", "Windows sharing semantics")
    def test_atomic_replace_retries_transient_windows_sharing_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            replace = json_storage.os.replace
            denied = PermissionError("temporary sharing violation")
            denied.winerror = 32
            with patch.object(json_storage.os, "replace", side_effect=[denied, None]) as attempt:
                # Use the real replacement for the successful second attempt.
                calls = 0
                def flaky_replace(source, destination):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise denied
                    return replace(source, destination)
                attempt.side_effect = flaky_replace
                json_storage.atomic_write_json(path, {"ready": True})
                self.assertEqual(attempt.call_count, 2)
            self.assertIn('"ready": true', path.read_text(encoding="utf-8"))

    def test_concurrent_writers_preserve_profiles_fields_and_pending_records(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            start = context.Event()
            processes = [context.Process(target=write_shared_state, args=(directory, index, start)) for index in range(4)]
            try:
                for process in processes:
                    process.start()
                start.set()
                for process in processes:
                    process.join(timeout=20)
                    self.assertEqual(process.exitcode, 0)
                snapshot = load_runtime_state(Path(directory))
                self.assertEqual(len(snapshot.profiles), 5)
                for worker in range(4):
                    self.assertEqual(snapshot.profile("profile-{}".format(worker))["counter"], 9)
                    for index in range(10):
                        self.assertEqual(snapshot.profile("shared")["worker_{}_{}".format(worker, index)], index)
                records = list_pending_qr(Path(directory))
                self.assertEqual(len(records), 20)
                self.assertEqual({item.rollcall_id for item in records}, {str(index) for index in range(5, 10)})
                self.assertEqual(list((Path(directory) / "state").glob("*.tmp")), [])
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
