"""The redh-api command: serve the API, check an installation, make tokens."""

from __future__ import annotations

import argparse
import grp
import hashlib
import http.client
import json
import logging
import os
import pwd
import secrets
import signal
import socket
import ssl
import stat
import sys
from typing import Any, List, NoReturn, Optional, Sequence, Tuple

from . import __version__, system, tls
from .apps import ARGS as APPS_ARGS
from .config import DEFAULT_CERTIFICATE, DEFAULT_CONFIG, DEFAULT_KEY, TOKEN_NAME_RE, Config, ConfigError, load
from .server import App, Server
from .shutdown import SUDO, Shutdown, current_user, sudo_refusal, unsafe_path

log = logging.getLogger("redh_api")

SERVICE_USER = "redh-api"

# journald takes a line's syslog priority from a "<N>" at its start
PRIORITIES = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3, logging.CRITICAL: 2}

# a finding of 'check': ok (True), wrong (False) or not checked (None); a topic; the details
Finding = Tuple[Optional[bool], str, str]
MARKS = {True: "ok", False: "!!", None: "--"}


class JournalFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return f"<{PRIORITIES.get(record.levelno, 6)}>{super().format(record)}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="redh-api", description="Status and shutdown of this server, and wake-ups of others, for Redh Control.")
    parser.add_argument("--version", action="version", version=f"redh-api {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    serve_parser = commands.add_parser("serve", help="run the API; this is what systemd starts")
    serve_parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"default: {DEFAULT_CONFIG}")

    check_parser = commands.add_parser("check", help="check the configuration, redh-shut, the sudo rule and the service")
    check_parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"default: {DEFAULT_CONFIG}")
    check_parser.add_argument("--user", default=SERVICE_USER,
                              help=f"whose sudo rule to check, when run as root (default: {SERVICE_USER})")

    token_parser = commands.add_parser("token", help="make a token and print its line for [tokens], and its pairing "
                                                     "code where RedhAPI has a certificate of its own")
    token_parser.add_argument("name", help="a name for the journal: a-z, 0-9, - and _")
    token_parser.add_argument("--certificate", default=DEFAULT_CERTIFICATE,
                              help=f"RedhAPI's own certificate, for the pairing code (default: {DEFAULT_CERTIFICATE})")

    certificate_parser = commands.add_parser("certificate", help="make RedhAPI's own certificate, to serve HTTPS "
                                                                 "without Caddy")
    certificate_parser.add_argument("--certificate", default=DEFAULT_CERTIFICATE, help=f"default: {DEFAULT_CERTIFICATE}")
    certificate_parser.add_argument("--key", default=DEFAULT_KEY, help=f"default: {DEFAULT_KEY}")
    certificate_parser.add_argument("--name", default=socket.gethostname(),
                                    help="its common name; default: this host's name")

    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve(args.config)
    if args.command == "check":
        return check(args.config, args.user)
    if args.command == "certificate":
        return certificate(args.certificate, args.key, args.name)
    return token(args.name, args.certificate)


def serve(path: str) -> int:
    setup_logging()
    try:
        config = load(path)
    except ConfigError as e:
        log.error("%s", e)
        return 1
    shutdown = Shutdown(config.command, config.delay)
    problem = shutdown.problem()
    if problem:
        log.warning("shutdown is not available: %s", problem)
    context = None
    if config.tls_certificate and config.tls_key:
        try:
            context = tls.server_context(config.tls_certificate, config.tls_key)
        except (OSError, ssl.SSLError) as e:
            log.error("cannot use %s and its key %s: %s", config.tls_certificate, config.tls_key, e)
            return 1
    try:
        server = Server(App(config, shutdown), tls=context)
    except OSError as e:
        log.error("cannot listen on %s: %s", config.listen, e.strerror or e)
        return 1
    signal.signal(signal.SIGTERM, _stop)
    log.info("RedhAPI %s for %s on %s%s, %d token(s)", __version__, config.name, config.listen,
             ", HTTPS with its own certificate" if context else "", len(config.tokens))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    log.info("stopped")
    return 0


def _stop(signum: int, frame: Any) -> NoReturn:
    raise KeyboardInterrupt


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    if os.environ.get("JOURNAL_STREAM"):
        handler.setFormatter(JournalFormatter("%(message)s"))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def check(path: str, user: str) -> int:
    """One line per finding; the exit status is 1 if anything needs fixing."""
    try:
        config = load(path)
    except ConfigError as e:
        return report([(False, "config", str(e))])
    findings: List[Finding] = [(True, "config", f"{config.path}, tokens: {', '.join(sorted(config.tokens.values()))}")]
    if config.tls_certificate and config.tls_key:
        findings.append(tls_finding(config.tls_certificate, config.tls_key, user))
    unsafe = unsafe_path(config.command)
    findings.append((unsafe is None, "command", unsafe or f"{config.command} belongs to root, and only root can change it"))
    if unsafe is None:
        if os.geteuid() == 0 or current_user() == user:
            problem = Shutdown(config.command, config.delay, user=user).sudo_problem()
            findings.append((problem is None, "sudo", problem or f"{user} may run {config.command} as root"))
        else:
            findings.append((None, "sudo", f"not checked; 'sudo redh-api check' says whether {user} may run it"))
    findings.append(apps_finding(config.apps_command, user))
    findings.append(system_finding(user))
    problem = health(config)
    findings.append((problem is None, "service", problem or f"answers on {config.listen}"))
    return report(findings)


def apps_finding(command: str, user: str, sudo: Sequence[str] = SUDO, owners: Sequence[int] = (0,)) -> Finding:
    """GET /v1/apps is optional: without redh-manager, or without its sudo rule, it's merely not offered."""
    shown = " ".join([command, *APPS_ARGS])
    if not os.path.lexists(command):
        return None, "apps", f"not offered: {command} does not exist (redh-manager isn't installed)"
    unsafe = unsafe_path(command, owners)
    if unsafe:
        return False, "apps", unsafe
    if not (os.geteuid() == 0 or current_user() == user):
        return None, "apps", f"not checked; 'sudo redh-api check' says whether {user} may run {shown}"
    refusal = sudo_refusal(sudo, user, [command, *APPS_ARGS])
    if refusal:
        return None, "apps", f"not offered: {refusal}"
    return True, "apps", f"{user} may run {shown} as root"


def tls_finding(certificate_path: str, key_path: str, user: str) -> Finding:
    """RedhAPI's own certificate: readable, with a key the service's user may read."""
    try:
        fingerprint = tls.fingerprint(certificate_path)
    except (OSError, ValueError) as e:
        return False, "tls", f"{certificate_path} can't be read: {e}"
    if not os.path.exists(key_path):
        return False, "tls", f"{key_path} does not exist"
    if readable_by(key_path, user) is False:
        return False, "tls", f"{user} may not read {key_path}"
    return True, "tls", f"its own certificate, {fingerprint}"


def system_finding(user: str, sys_dir: str = "/sys") -> Finding:
    """GET /v1/system always answers; this says which sensors it finds, and whether the user may read
    the energy counters, which are root's alone unless redh-api.rules gives them to its group."""
    _, sensor = system.temperature(sys_dir)
    texts = [f"temperature from {sensor}" if sensor else "no temperature sensor"]
    zones = system.rapl_zones(sys_dir)
    counters = [os.path.join(zone, "energy_uj") for zone in zones]
    readable = [readable_by(counter, user) for counter in counters]
    if not zones:
        texts.append("no energy counter for the power")
    elif all(readable):
        names = ", ".join(system.read(os.path.join(zone, "name")) or os.path.basename(zone) for zone in zones)
        texts.append(f"power from {names}")
    elif any(r is None for r in readable):
        texts.append(f"power not checked: there is no user {user}")
    else:
        texts.append(f"no power: {user} may not read {counters[0]} (see the README)")
    found = bool(sensor) and bool(zones) and all(readable)
    return (True if found else None), "system", "; ".join(texts)


def readable_by(path: str, user: str) -> Optional[bool]:
    """Whether the user may read the file, by its owner, group and mode; None if that can't be told."""
    try:
        entry = pwd.getpwnam(user)
        stats = os.stat(path)
    except (KeyError, OSError):
        return None
    if stats.st_uid == entry.pw_uid:
        return bool(stats.st_mode & stat.S_IRUSR)
    groups = {entry.pw_gid} | {group.gr_gid for group in grp.getgrall() if user in group.gr_mem}
    if stats.st_gid in groups:
        return bool(stats.st_mode & stat.S_IRGRP)
    return bool(stats.st_mode & stat.S_IROTH)


def report(findings: List[Finding]) -> int:
    for ok, topic, text in findings:
        print(f"{MARKS[ok]}  {topic:<8} {text}")
    return 1 if any(ok is False for ok, _, _ in findings) else 0


def health(config: Config) -> Optional[str]:
    """Why nothing like RedhAPI answers on the configured address, or None if it does: over HTTPS
    with its own certificate, which it then has to present."""
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(config.host, config.host)
    connection: Optional[http.client.HTTPConnection] = None
    try:
        if config.tls_certificate:
            connection = http.client.HTTPSConnection(host, config.port, timeout=3,
                                                     context=tls.client_context(config.tls_certificate))
        else:
            connection = http.client.HTTPConnection(host, config.port, timeout=3)
        connection.request("GET", "/health")
        response = connection.getresponse()
        body = response.read()
    except (OSError, http.client.HTTPException) as e:
        return f"nothing answers on {config.listen} ({e}); is redh-api.service running?"
    finally:
        if connection is not None:
            connection.close()
    try:
        healthy = response.status == 200 and json.loads(body) == {"status": "ok"}
    except ValueError:
        healthy = False
    return None if healthy else f"{config.listen} answers, but not as RedhAPI does (HTTP {response.status})"


def token(name: str, certificate_path: str = DEFAULT_CERTIFICATE) -> int:
    if not TOKEN_NAME_RE.fullmatch(name):
        print(f"redh-api: '{name}' is not a token name: use a-z, 0-9, - and _, up to 32 characters", file=sys.stderr)
        return 2
    value = secrets.token_urlsafe(32)
    print(f"The token {name}, for Redh Control. Nothing keeps a copy; if it's lost, make a new one:")
    print(f"    {value}")
    print()
    print(f"Its line for [tokens] in {DEFAULT_CONFIG}; restart redh-api afterwards:")
    print(f"    {name} = {hashlib.sha256(value.encode()).hexdigest()}")
    if os.path.exists(certificate_path):
        try:
            code = tls.pairing_code(value, certificate_path)
        except (OSError, ValueError) as e:
            print(f"redh-api: no pairing code, as {certificate_path} can't be read: {e}", file=sys.stderr)
        else:
            print()
            print("Its pairing code, for Redh Control: the token and this server's certificate in one, to paste")
            print("instead of the token:")
            print(f"    {code}")
    return 0


def certificate(certificate_path: str, key_path: str, name: str, user: str = SERVICE_USER) -> int:
    """Makes RedhAPI's own certificate. Run as root, both files belong to root and the service's
    group, which may read the key."""
    group = -1
    if os.geteuid() == 0:
        try:
            group = grp.getgrnam(user).gr_gid
        except KeyError:
            print(f"redh-api: there is no group {user} yet; make the user first", file=sys.stderr)
            return 1
    try:
        tls.make(certificate_path, key_path, name, group=group)
    except (tls.CertificateError, OSError) as e:
        print(f"redh-api: {e}", file=sys.stderr)
        return 1
    print(f"Made {certificate_path} for {name}, valid for ten years, and its key {key_path}.")
    print("Its SHA-256 fingerprint, which the pairing codes carry:")
    print(f"    {tls.fingerprint(certificate_path)}")
    print()
    print(f"For {DEFAULT_CONFIG}, so that RedhAPI serves HTTPS itself on every address of this server,")
    print("without Caddy; restart redh-api afterwards:")
    print("    [api]")
    print("    listen = 0.0.0.0:8470")
    print("    [tls]")
    print(f"    certificate = {certificate_path}")
    print(f"    key = {key_path}")
    return 0
