"""Lower other audio while Friday talks: Spotify and Apple Music (their own volume, over AppleScript)
and media playing in Aman's Brave tabs (through the Friday extension). macOS has no public per-app
volume for anything else.

Ducks when an utterance starts and stays down until it's over, including the quiet stretches while
Friday works between remarks, so the music doesn't pump up and down between sentences. Restores a
moment after she stops. A volume Aman changed himself while ducked is left as he set it. The saved
volumes are kept on disk, so a crash or quit while ducked is undone on the next start or the quit."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

from friday_core import mac
from friday_core.paths import STATE_DIR

log = logging.getLogger("fridayd.ducking")
SAVED = STATE_DIR / "ducked.json"
APPS = {"spotify": "Spotify", "music": "Music"}


def _player(app: str) -> Optional[tuple[str, int]]:
    """(state, volume) of a running player, or None when it isn't running."""
    out = mac.osascript(f'''if application "{app}" is running then
  tell application "{app}" to return (player state as string) & "," & (sound volume as string)
end if
return ""''', timeout=4)
    if not out or "," not in out:
        return None
    state, vol = out.split(",", 1)
    return state.strip(), int(float(vol))


def _set_volume(app: str, volume: int) -> None:
    mac.osascript(f'tell application "{app}" to set sound volume to {max(0, min(100, int(volume)))}', timeout=4)


class Ducker(threading.Thread):
    def __init__(self, speaking: Callable[[], bool], brave: Optional[Callable[[str, dict], dict]] = None,
                 level: int = 10, restore_after_s: float = 1.2, apps: tuple[str, ...] = ("spotify", "music", "brave")):
        super().__init__(name="ducking", daemon=True)
        self.speaking, self.brave = speaking, brave
        self.level, self.restore_after_s, self.apps = level, restore_after_s, apps
        self.saved: dict[str, int] = {}
        self.brave_ducked = False
        self.ducking = False                 # this utterance has been handled (even if nothing was playing)
        self._lock = threading.Lock()

    def run(self) -> None:
        self.restore()                      # a crash or quit while ducked last time
        quiet_since: Optional[float] = None
        while True:
            time.sleep(0.15)
            try:
                if self.speaking():
                    quiet_since = None
                    if not self.ducking:
                        self.ducking = True
                        self.duck()
                elif self.ducking:
                    quiet_since = quiet_since or time.time()
                    if time.time() - quiet_since >= self.restore_after_s:
                        self.restore()
                        self.ducking = False
                        quiet_since = None
            except Exception:
                log.exception("ducking failed")
                time.sleep(2)

    def duck(self) -> None:
        with self._lock:
            for key, app in APPS.items():
                if key not in self.apps:
                    continue
                try:
                    p = _player(app)
                except mac.MacError as e:   # e.g. macOS hasn't allowed FridayEars to control it yet
                    log.info("can't duck %s: %s", app, e)
                    continue
                if not p or p[0] != "playing" or p[1] <= self.level:
                    continue
                self.saved[key] = p[1]
                _set_volume(app, (p[1] + self.level) // 2)   # a short fade, not a cut
                _set_volume(app, self.level)
                log.info("ducked %s %d -> %d", app, p[1], self.level)
            self._persist()
            if "brave" in self.apps and self.brave is not None:
                try:
                    r = self.brave("duck", {"factor": self.level / 100})
                    self.brave_ducked = bool(r.get("ducked"))
                except Exception as e:           # Brave closed or the extension isn't connected
                    log.debug("brave duck: %s", e)

    def restore(self) -> None:
        with self._lock:
            saved = self.saved or self._load()
            for key, volume in saved.items():
                app = APPS.get(key)
                if not app:
                    continue
                try:
                    p = _player(app)
                    if p and abs(p[1] - self.level) <= 1:     # still where Friday put it: give it back
                        _set_volume(app, (p[1] + volume) // 2)
                        _set_volume(app, volume)
                        after = _player(app)        # Spotify keeps N-1 when told N: nudge it back up
                        if after and after[1] < volume:
                            _set_volume(app, volume + (volume - after[1]))
                        log.info("restored %s -> %d", app, volume)
                except mac.MacError as e:
                    log.debug("%s: %s", app, e)
            self.saved = {}
            SAVED.unlink(missing_ok=True)
            if self.brave_ducked and self.brave is not None:
                try:
                    self.brave("unduck", {})
                except Exception as e:
                    log.debug("brave unduck: %s", e)
            self.brave_ducked = False

    def _persist(self) -> None:
        if self.saved:
            SAVED.parent.mkdir(parents=True, exist_ok=True)
            SAVED.write_text(json.dumps(self.saved))

    @staticmethod
    def _load() -> dict[str, int]:
        try:
            return {k: int(v) for k, v in json.loads(SAVED.read_text()).items()}
        except (OSError, ValueError):
            return {}
