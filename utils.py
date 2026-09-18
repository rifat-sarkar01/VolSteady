"""
VolSteady — Utilities
Logging setup, single-instance lock, and helper functions.
"""

import logging
import os
import sys
import tempfile
from pathlib import Path


# ─── Logging setup ───────────────────────────────────────────

def setup_logging(debug: bool = False):
    """Configure logging to both file and console."""
    appdata = os.environ.get("APPDATA", str(Path.home()))
    log_dir = Path(appdata) / "VolSteady"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "volsteady.log"

    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%H:%M:%S"

    handlers = [
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    if not getattr(sys, "frozen", False):  # Not packaged — show console output
        handlers.append(logging.StreamHandler())

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)
    return log_file


# ─── Single-instance lock ────────────────────────────────────

_LOCK_FILE = None


def acquire_single_instance_lock() -> bool:
    """
    Prevent running two copies of VolSteady simultaneously.
    Returns True if lock acquired (this is the only instance),
    False if another instance is already running.
    """
    global _LOCK_FILE
    lock_path = Path(tempfile.gettempdir()) / "volsteady.lock"

    try:
        # Try to create the lock file exclusively
        import msvcrt
        _LOCK_FILE = open(lock_path, "w")
        msvcrt.locking(_LOCK_FILE.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except (IOError, OSError):
        return False


def release_single_instance_lock():
    """Release the single-instance lock on exit."""
    global _LOCK_FILE
    if _LOCK_FILE:
        try:
            import msvcrt
            msvcrt.locking(_LOCK_FILE.fileno(), msvcrt.LK_UNLCK, 1)
            _LOCK_FILE.close()
        except Exception:
            pass
        _LOCK_FILE = None
