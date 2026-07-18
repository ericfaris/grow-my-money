"""Sentinel-file kill switch.

A killed bot stays killed across restarts because the sentinel lives in the
bind-mounted ``state/`` dir. This is intentional fail-closed behavior: only an
explicit ``resume`` (or removing the file) re-arms trading.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_KILL_PATH = Path("state") / "KILL"


class KillSwitch:
    def __init__(self, path: str | Path = DEFAULT_KILL_PATH):
        self.path = Path(path)

    def engage(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("engaged\n", encoding="utf-8")
        log.warning("KILL SWITCH ENGAGED at %s — no new orders will be placed", self.path)

    def clear(self) -> None:
        try:
            os.remove(self.path)
            log.warning("Kill switch cleared at %s", self.path)
        except FileNotFoundError:
            pass

    def is_engaged(self) -> bool:
        return self.path.exists()
