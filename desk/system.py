"""Small operating-system helpers for running the bot unattended.

InstanceLock: makes sure only ONE copy of the bot uses a database at a time.
  Two bots on the same paper/live account would double-trade.
keep_awake: asks Windows not to go to sleep while the bot runs (like a video
  player does). The screen can still switch off. It's automatically undone
  when the bot stops. Does nothing on Linux/macOS (servers don't sleep).
"""

from __future__ import annotations

import os
from pathlib import Path

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class InstanceLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


def keep_awake(on: bool) -> bool:
    """Windows only. True if the request was accepted."""
    if os.name != "nt":
        return False
    import ctypes
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
