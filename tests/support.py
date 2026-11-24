"""For the tests: a scratch directory, stand-ins for sudo and redh-shut, a running server."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import shutil
import ssl
import tempfile
import textwrap
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from redh_api.server import App, Server

TOKEN = "lab-token-for-redh-control"
DIGEST = hashlib.sha256(TOKEN.encode()).hexdigest()

# Files in the scratch directory are the tests' own, so they pass for root's.
MINE = (0, os.getuid())

# The tests read the log with assertLogs; otherwise it stays quiet.
logging.getLogger("redh_api").addHandler(logging.NullHandler())
logging.getLogger("redh_api").propagate = False


def wait_for(condition: Callable[[], bool], seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


class Lab:
    """A scratch directory for a config, a redh-shut and a sudo that only pretends."""

    def __init__(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="redh-api-"))
        self.marker = self.path("redh-shut.ran")
        self.sudo_log = self.path("sudo.log")

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def write(self, name: str, text: str, mode: int = 0o644) -> str:
        path = self.path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, mode)
        return path

    def config(self, text: str) -> str:
        return self.write("redh-api.conf", textwrap.dedent(text))

    def command(self, status: int = 0, message: str = "") -> str:
        """A redh-shut that notes each run and exits with the given status."""
        complaint = f"echo '{message}' >&2\n" if message else ""
        return self.write("redh-shut", f"#!/bin/sh\necho ran >> '{self.marker}'\n{complaint}exit {status}\n", 0o755)

    def sudo(self, allow: bool = True) -> str:
        """A sudo that logs its arguments, answers -l and runs anything else as is."""
        lines = [
            "#!/bin/sh",
            f"echo \"$*\" >> '{self.sudo_log}'",
            '[ "$1" = "-n" ] || exit 2',
            "shift",
            'if [ "$1" = "-U" ]; then shift 2; fi',
            'if [ "$1" = "-l" ]; then',
            '    echo "$2"; exit 0' if allow else '    echo "Sorry, user lab may not run sudo on lab." >&2; exit 1',
            "fi",
            'exec "$@"',
        ]
        return self.write("sudo", "\n".join(lines) + "\n", 0o755)

    def runs(self) -> int:
        return len(self._lines(self.marker))

    def sudo_calls(self) -> List[str]:
        return self._lines(self.sudo_log)

    def _lines(self, path: str) -> List[str]:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []


class Running:
    """A RedhAPI server on a free port, in a thread."""

    def __init__(self, app: App, tls: Optional[ssl.SSLContext] = None,
                 client: Optional[ssl.SSLContext] = None) -> None:
        """`tls` serves HTTPS with RedhAPI's own certificate; `client` is how requests trust it."""
        self.server = Server(app, ("127.0.0.1", 0), tls=tls)
        self.port = self.server.server_address[1]
        self.client = client
        # a short poll interval, so that stop() doesn't wait half a second each time
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def request(self, method: str, path: str, token: Optional[str] = None, body: Optional[bytes] = None,
                headers: Optional[Dict[str, str]] = None) -> Tuple[int, http.client.HTTPMessage, Any]:
        connection = (http.client.HTTPSConnection("127.0.0.1", self.port, timeout=5, context=self.client)
                      if self.client else http.client.HTTPConnection("127.0.0.1", self.port, timeout=5))
        all_headers = dict(headers or {})
        if token is not None:
            all_headers["Authorization"] = f"Bearer {token}"
        try:
            connection.request(method, path, body=body, headers=all_headers)
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        return response.status, response.headers, json.loads(data) if data else None
