"""The apps on this server as redh-manager lists them: `redh list --json`, run as root through sudo."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .shutdown import SUDO, current_user, last_line, unsafe_path

ARGS = ("list", "--json")  # exactly what the sudo rule allows
CACHE_SECONDS = 10.0       # redh asks systemd and Podman each time; the app asks every 10 seconds
TIMEOUT = 90.0             # redh waits up to a minute for Podman


class AppsUnavailable(Exception):
    """The apps can't be listed; the message says why."""


class Apps:
    """Runs `redh list --json` as root and keeps its answer for a few seconds.

    RedhAPI doesn't look into the apps itself: they are the podman user's, under a directory only
    root and podman may enter, so only redh, as root, can tell.
    """

    def __init__(self, command: str, *, user: Optional[str] = None, sudo: Sequence[str] = SUDO,
                 owners: Sequence[int] = (0,), clock: Callable[[], float] = time.monotonic,
                 cache_seconds: float = CACHE_SECONDS, timeout: float = TIMEOUT) -> None:
        self.command = command
        self.user = user or current_user()
        self.sudo = list(sudo)
        self.owners = tuple(owners)
        self.clock = clock
        self.cache_seconds = cache_seconds
        self.timeout = timeout
        self._lock = threading.Lock()
        self._kept: Optional[Tuple[float, Dict[str, Any]]] = None

    @property
    def shown(self) -> str:
        return " ".join([self.command, *ARGS])

    def listing(self) -> Dict[str, Any]:
        """redh's document, at most cache_seconds old; AppsUnavailable says why there is none."""
        with self._lock:  # one redh at a time, and whoever asks meanwhile gets its answer
            if self._kept is not None and self.clock() - self._kept[0] < self.cache_seconds:
                return self._kept[1]
            document = self._run()
            self._kept = (self.clock(), document)
            return document

    def _run(self) -> Dict[str, Any]:
        if not os.path.lexists(self.command):
            raise AppsUnavailable(f"{self.command} does not exist; redh-manager isn't installed")
        unsafe = unsafe_path(self.command, self.owners)
        if unsafe:
            raise AppsUnavailable(unsafe)
        try:
            result = subprocess.run(self.sudo + [self.command, *ARGS], stdin=subprocess.DEVNULL,
                                    capture_output=True, encoding="utf-8", errors="replace", timeout=self.timeout)
        except FileNotFoundError:
            raise AppsUnavailable("sudo is not installed") from None
        except subprocess.TimeoutExpired:
            raise AppsUnavailable(f"{self.shown} took longer than {self.timeout:g} seconds") from None
        except OSError as e:
            raise AppsUnavailable(f"could not run sudo: {e.strerror}") from None
        if result.returncode != 0:
            reason = last_line(result.stderr) or last_line(result.stdout)
            if reason.startswith("sudo:"):
                raise AppsUnavailable(f"sudo does not let {self.user} run {self.shown} without a password ({reason})")
            raise AppsUnavailable(f"{self.shown} failed with exit status {result.returncode}"
                                  + (f": {reason}" if reason else ""))
        try:
            document = json.loads(result.stdout)
        except ValueError:
            document = None
        if not isinstance(document, dict):
            raise AppsUnavailable(f"{self.shown} didn't write a JSON object")
        return document
