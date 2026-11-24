"""HTTP: the routes, the bearer tokens and the JSON answers."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import socket
import ssl
import string
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

from . import __version__, system
from .apps import Apps, AppsUnavailable
from .config import Config
from .shutdown import Shutdown
from .wake import Wake, parse_mac, spelled

log = logging.getLogger("redh_api")

MAX_BODY = 1024

# path: (method, whether it needs a token)
ROUTES = {
    "/health": ("GET", False),
    "/v1/status": ("GET", True),
    "/v1/shutdown": ("POST", True),
    "/v1/apps": ("GET", True),
    "/v1/wake": ("POST", True),
    "/v1/system": ("GET", True),
}

PRINTABLE = set(string.printable) - set("\t\n\r\x0b\x0c")

# status, body, and what to do once the answer is out
Answer = Tuple[int, Dict[str, Any], Optional[Callable[[], None]]]


class ApiError(Exception):
    """An answer other than success: status, error code, message, extra headers."""

    def __init__(self, status: int, code: str, message: str, headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def error_body(code: str, message: str) -> Dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def printable(text: str, limit: int = 100) -> str:
    """Text from a request, safe for the journal: printable ASCII, cut short."""
    cleaned = "".join(c if c in PRINTABLE else "?" for c in text[:limit])
    return cleaned + ("..." if len(text) > limit else "")


class App:
    """What the requests work with: the configuration, the shutdown, the apps, the wake-ups, and the
    machine through /proc and /sys."""

    def __init__(self, config: Config, shutdown: Shutdown, proc: str = "/proc", apps: Optional[Apps] = None,
                 wake: Optional[Wake] = None, sampler: Optional[system.Sampler] = None) -> None:
        self.config = config
        self.shutdown = shutdown
        self.proc = proc
        self.apps = apps or Apps(config.apps_command)
        self.wake = wake or Wake(config.wake_broadcast, config.wake_port)
        self.sampler = sampler or system.Sampler(proc)

    def token_name(self, authorization: Optional[str]) -> Optional[str]:
        """The name of the bearer token in an Authorization header, or None if it isn't one of ours."""
        scheme, _, token = (authorization or "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        found = None
        for known, name in self.config.tokens.items():
            if hmac.compare_digest(digest, known):
                found = name
        return found

    def status(self) -> Dict[str, Any]:
        return {
            "name": self.config.name,
            "version": __version__,
            "boot_id": system.boot_id(self.proc),
            "uptime_seconds": system.uptime_seconds(self.proc),
            "shutdown": self.shutdown.status(),
            "wake": self.wake.status(),
        }


class Handler(BaseHTTPRequestHandler):
    server: Server
    timeout = 10  # seconds for each read or write: Caddy's, or the apps' with RedhAPI's own certificate

    def setup(self) -> None:
        """With RedhAPI's own certificate, the TLS handshake, in this request's thread: a slow client
        holds up nobody else."""
        if self.server.tls is not None:
            self.request.settimeout(self.timeout)
            self.request = self.server.tls.wrap_socket(self.request, server_side=True)
        super().setup()

    def version_string(self) -> str:
        return "RedhAPI"

    def do_GET(self) -> None:
        self.answer()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

    def answer(self) -> None:
        started = time.monotonic()
        self.token = "-"
        status, after = 500, None
        try:
            try:
                status, body, after = self.route()
                headers: Dict[str, str] = {}
            except ApiError as e:
                status, body, headers = e.status, error_body(e.code, e.message), e.headers
            except Exception:
                log.exception("%s %s failed", self.command, printable(self.path))
                status, body, headers = 500, error_body("internal", "RedhAPI failed; its journal says why."), {}
            self.send_json(status, body, headers)
        finally:
            log.info("%s %s %s %d %dms token=%s", self.client(), self.command, printable(self.path.split("?", 1)[0]),
                     status, (time.monotonic() - started) * 1000, self.token)
            if after is not None:
                after()

    def route(self) -> Answer:
        try:
            url = urlsplit(self.path)
        except ValueError:
            raise ApiError(400, "bad_request", "The request's path can't be read.") from None
        if url.path not in ROUTES:
            raise ApiError(404, "not_found", f"There is no {printable(url.path)} here.")
        method, needs_token = ROUTES[url.path]
        if self.command != method:
            raise ApiError(405, "method_not_allowed", f"{url.path} takes {method} only.", {"Allow": method})
        if url.query:
            raise ApiError(400, "unexpected_parameters", f"{url.path} takes no parameters.")
        body = self.read_body()
        app = self.server.app
        if needs_token:
            name = app.token_name(self.headers.get("Authorization"))
            if name is None:
                raise ApiError(401, "unauthorized", "This needs a valid bearer token.",
                               {"WWW-Authenticate": 'Bearer realm="redh-api"'})
            self.token = name
        if url.path == "/health":
            return 200, {"status": "ok"}, None
        if url.path == "/v1/status":
            return 200, app.status(), None
        if url.path == "/v1/apps":
            try:
                return 200, app.apps.listing(), None
            except AppsUnavailable as e:
                raise ApiError(409, "apps_unavailable", str(e)) from None
        if url.path == "/v1/wake":
            return self.request_wake(body)
        if url.path == "/v1/system":
            return 200, system.report(app.sampler, app.config.disks), None
        return self.request_shutdown(body)

    def request_shutdown(self, body: bytes) -> Answer:
        if body.strip() not in (b"", b"{}"):
            raise ApiError(400, "unexpected_parameters", "/v1/shutdown takes no parameters.")
        shutdown = self.server.app.shutdown
        problem = shutdown.accept()
        if problem is not None:
            raise ApiError(409, "shutdown_unavailable", problem)
        log.warning("shutdown requested with token %s; %s runs in %gs", self.token, shutdown.command, shutdown.delay)
        return 202, {"accepted": True, "delay_seconds": shutdown.delay}, shutdown.start

    def request_wake(self, body: bytes) -> Answer:
        try:
            request = json.loads(body) if body.strip() else None
        except ValueError:
            request = None
        if not isinstance(request, dict) or "mac" not in request:
            raise ApiError(400, "bad_request", 'The body must be {"mac": "AA:BB:CC:DD:EE:FF"}.')
        if len(request) > 1:
            raise ApiError(400, "unexpected_parameters", "/v1/wake takes the mac only.")
        mac = parse_mac(request["mac"]) if isinstance(request["mac"], str) else None
        if mac is None:
            raise ApiError(400, "bad_request",
                           f"'{printable(str(request['mac']), 40)}' is not a MAC address such as AA:BB:CC:DD:EE:FF.")
        wake = self.server.app.wake
        try:
            wake.send(mac)
        except OSError as e:
            raise ApiError(409, "wake_failed", f"The magic packet for {spelled(mac)} couldn't be sent to "
                                               f"{wake.broadcast}:{wake.port}: {e.strerror or e}") from None
        log.info("wake-up of %s requested with token %s, sent to %s:%d", spelled(mac), self.token,
                 wake.broadcast, wake.port)
        return 200, {"sent": True, "mac": spelled(mac)}, None

    def read_body(self) -> bytes:
        if "Transfer-Encoding" in self.headers:
            raise ApiError(411, "length_required", "Send a Content-Length, not a chunked body.")
        raw = self.headers.get("Content-Length", "0").strip()
        if not raw.isdigit():
            raise ApiError(400, "bad_request", "Content-Length is not a number.")
        length = int(raw)
        if length > MAX_BODY:
            raise ApiError(413, "too_large", f"A request body may have {MAX_BODY} bytes at most.")
        return self.rfile.read(length) if length else b""

    def send_json(self, status: int, body: Dict[str, Any], headers: Dict[str, str]) -> None:
        data = (json.dumps(body, ensure_ascii=False) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        """http.server's own errors, such as an unsupported method, in the API's format."""
        self.log_error("%d %s", code, message or "")
        reason = self.responses.get(code, ("Error",))[0]
        data = (json.dumps(error_body(f"http_{code}", message or reason)) + "\n").encode()
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD" and code >= 200 and code not in (204, 304):
            self.wfile.write(data)

    def client(self) -> str:
        """Who asked: Caddy passes the app's address in X-Forwarded-For. Without Caddy, the header
        would be the client's own claim."""
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return printable(forwarded, 64) if forwarded and self.server.tls is None else self.client_address[0]

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        pass  # answer() logs each request once, with the token's name

    def log_message(self, format: str, *args: Any) -> None:
        log.warning("%s %s", self.client_address[0], printable(format % args, 200))


class Server(ThreadingHTTPServer):
    """A thread per request, on the address from the configuration: plain HTTP on loopback behind
    Caddy, or HTTPS with RedhAPI's own certificate."""

    daemon_threads = True

    def __init__(self, app: App, address: Optional[Tuple[str, int]] = None,
                 tls: Optional[ssl.SSLContext] = None) -> None:
        self.app = app
        self.tls = tls
        host, port = address or (app.config.host, app.config.port)
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, port), Handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind looks up the host's FQDN, which can hang on DNS.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), int(self.server_address[1])

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.warning("connection from %s: %s", client_address[0] if client_address else "?", sys.exc_info()[1])
