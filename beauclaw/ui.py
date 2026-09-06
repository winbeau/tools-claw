"""Small terminal animations; logs and machine-readable output stay plain."""
from __future__ import annotations

import os
import sys
import threading

_enabled = True


def set_animation(enabled: bool) -> None:
    global _enabled
    _enabled = enabled


class activity:
    def __init__(self, message: str):
        self.message = message
        self.stream = sys.stderr
        self.stop = threading.Event()
        self.thread = None

    def update(self, message: str) -> None:
        self.message = message

    def __enter__(self):
        if (_enabled and self.stream.isatty() and os.environ.get("TERM") != "dumb"
                and os.environ.get("BEAUCLAW_NO_ANIMATION", "").lower() not in ("1", "true", "yes")):
            self.thread = threading.Thread(target=self._draw, daemon=True, name="terminal-activity")
            self.thread.start()
        return self

    def _draw(self):
        frames = "|/-\\"
        index = 0
        while not self.stop.is_set():
            color = "" if "NO_COLOR" in os.environ else "\033[38;5;208m"
            reset = "" if not color else "\033[0m"
            self.stream.write(f"\r\033[2K{color}{frames[index % len(frames)]}{reset} {self.message}")
            self.stream.flush()
            index += 1
            self.stop.wait(0.12)

    def __exit__(self, *_):
        self.stop.set()
        if self.thread:
            self.thread.join()
            self.stream.write("\r\033[2K")
            self.stream.flush()
