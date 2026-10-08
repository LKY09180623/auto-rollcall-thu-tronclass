"""Small cross-process transactions for local JSON state files."""

from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


@contextmanager
def file_transaction(path: Path, *, timeout: float = 10.0):
    """Lock a read/modify/write operation; OS locks are released after a crash."""
    path = Path(path).resolve()
    with _locks_guard:
        thread_lock = _locks.setdefault(str(path), threading.RLock())
    with thread_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(path.suffix + ".lock"), "a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()

                def acquire():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

                def release():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                def acquire():
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                def release():
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

            deadline = time.monotonic() + timeout
            while True:
                try:
                    acquire()
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timed out waiting for the local state transaction.") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                release()


def atomic_write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            handle.write("\n")
        # Windows readers/virus scanners can briefly deny replacement even
        # when all application writers hold the transaction lock.
        deadline = time.monotonic() + 1.0
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
