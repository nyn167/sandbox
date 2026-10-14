"""Small Windows / macOS / Linux differences in one place.

- file_lock(): exclusive lock on a lock file (fcntl on macOS/Linux, msvcrt on Windows)
- replace(): os.replace with a short retry (Windows refuses while antivirus/indexer briefly holds a file)
- keep_awake(): stop the machine idle-sleeping while the app runs
    Windows: SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    macOS  : `caffeinate -i -w <our pid>` (ends automatically when we exit)
- utf8_console(): the terminal table uses │ ═ ₹; old Windows consoles default to cp1252
"""
from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import time

log = logging.getLogger(__name__)
WINDOWS = sys.platform == "win32"


@contextlib.contextmanager
def file_lock(path):
    with open(path, "a+") as f:
        if WINDOWS:
            import msvcrt
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)   # retries ~10 s internally, then raises
                    break
                except OSError:
                    time.sleep(0.2)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def replace(src, dst, attempts: int = 10):
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.1 * (i + 1))


def keep_awake() -> None:
    """Prevent idle sleep for as long as this process runs (lid-close / power-button still apply)."""
    try:
        if WINDOWS:
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            log.info("Keeping Windows awake while the app runs")
        elif sys.platform == "darwin":
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("Keeping macOS awake while the app runs (caffeinate)")
    except Exception as e:
        log.warning("Could not prevent sleep: %s", e)


def utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
