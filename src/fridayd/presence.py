"""Is Aman at the Mac, and can Friday talk right now?"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from friday_core.paths import FRIDAY_HOME

MICBUSY = FRIDAY_HOME / "bin" / "micbusy"


def _ioreg(args: list[str]) -> str:
    try:
        return subprocess.run(["ioreg", *args], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def idle_seconds() -> float:
    m = re.search(r'"HIDIdleTime" = (\d+)', _ioreg(["-c", "IOHIDSystem", "-d", "4"]))
    return int(m.group(1)) / 1e9 if m else 0.0


def screen_locked() -> bool:
    return '"IOConsoleLocked" = Yes' in _ioreg(["-n", "Root", "-d1"])


def mic_busy() -> bool:
    """True while any app holds an input device (a call, a recording, Wispr Flow dictating)."""
    if not MICBUSY.exists():
        return False
    try:
        return subprocess.run([str(MICBUSY)], timeout=3).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@dataclass
class Snapshot:
    idle_s: float
    locked: bool
    mic_busy: bool
    away_after_s: float
    face: bool = False       # FridayEars' camera saw a face recently (only checked while keyboard/mouse are idle)

    @property
    def at_desk(self) -> bool:
        return self.face or (not self.locked and self.idle_s < self.away_after_s)

    def as_dict(self) -> dict:
        return {"idle_s": round(self.idle_s, 1), "locked": self.locked, "mic_busy": self.mic_busy,
                "face": self.face, "at_desk": self.at_desk}


_face_seen_at = 0.0
_typing = (0.0, 0)          # (when FridayEars reported, keys pressed in the minute before)


def saw_typing(keys_per_min: int) -> None:
    global _typing
    _typing = (time.time(), int(keys_per_min))


def typing_rate() -> int:
    """Keys per minute right now (0 if FridayEars hasn't reported in the last 30 s)."""
    at, n = _typing
    return n if time.time() - at < 30 else 0


def saw_face() -> None:
    global _face_seen_at
    _face_seen_at = time.time()


_last: tuple[float, Snapshot] | None = None


def snapshot(away_after_s: float, face_grace_s: float = 45, max_age: float = 0) -> Snapshot:
    """Each reading runs ioreg twice and micbusy once. ``max_age`` lets a frequent reader (FridayEars
    polls /status every 0.3 s) reuse a recent one instead of launching ~10 processes a second, which
    kept coreaudiod near 50% CPU."""
    global _last
    now = time.time()
    if max_age and _last and now - _last[0] < max_age and _last[1].away_after_s == away_after_s:
        snap = _last[1]
        snap.face = now - _face_seen_at < face_grace_s
        return snap
    snap = Snapshot(idle_seconds(), screen_locked(), mic_busy(), away_after_s,
                    face=now - _face_seen_at < face_grace_s)
    _last = (now, snap)
    return snap
