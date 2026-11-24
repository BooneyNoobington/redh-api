"""The configuration, /etc/redh-api/redh-api.conf, read strictly: a typo stops the start."""

from __future__ import annotations

import configparser
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

DEFAULT_CONFIG = "/etc/redh-api/redh-api.conf"
DEFAULT_LISTEN = "127.0.0.1:8470"
DEFAULT_COMMAND = "/usr/local/bin/redh-shut"
DEFAULT_APPS_COMMAND = "/usr/local/bin/redh"
DEFAULT_DELAY = 2.0
MAX_DELAY = 60.0
DEFAULT_BROADCAST = "255.255.255.255"
DEFAULT_WAKE_PORT = 9
DEFAULT_DISKS = ("/",)
DEFAULT_CERTIFICATE = "/etc/redh-api/tls.crt"  # where 'redh-api certificate' puts them
DEFAULT_KEY = "/etc/redh-api/tls.key"

TOKEN_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PORT_RE = re.compile(r"[0-9]{1,5}")

# The keys each section may have; [tokens] takes any token name.
SECTIONS: Dict[str, Optional[Set[str]]] = {
    "api": {"listen", "name"},
    "shutdown": {"command", "delay_seconds"},
    "apps": {"command"},
    "wake": {"broadcast", "port"},
    "system": {"disks"},
    "tls": {"certificate", "key"},
    "tokens": None,
}


class ConfigError(Exception):
    """The configuration can't be used; the message says where and why."""


@dataclass(frozen=True)
class Config:
    path: str
    host: str
    port: int
    name: str
    command: str
    delay: float
    tokens: Dict[str, str]  # the SHA-256 of a token, in hex -> the token's name
    apps_command: str = DEFAULT_APPS_COMMAND  # redh-manager's redh, for GET /v1/apps
    wake_broadcast: str = DEFAULT_BROADCAST  # where POST /v1/wake sends the magic packet
    wake_port: int = DEFAULT_WAKE_PORT
    disks: Tuple[str, ...] = DEFAULT_DISKS  # whose space GET /v1/system reports
    tls_certificate: Optional[str] = None  # RedhAPI's own certificate, to serve HTTPS without Caddy
    tls_key: Optional[str] = None

    @property
    def listen(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"


def load(path: str = DEFAULT_CONFIG) -> Config:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as f:
            parser.read_file(f)
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}") from None
    except configparser.Error as e:
        raise ConfigError(f"{path}: {' '.join(str(e).split())}") from None

    if parser.defaults():
        raise ConfigError(f"{path}: [DEFAULT] is not used; put each key in its own section")
    for section in parser.sections():
        if section not in SECTIONS:
            raise ConfigError(f"{path}: unknown section [{section}]; "
                              "there are [api], [shutdown], [apps], [wake], [system], [tls] and [tokens]")
        allowed = SECTIONS[section]
        if allowed is None:
            continue
        for key in parser[section]:
            if key not in allowed:
                raise ConfigError(f"{path}: unknown key '{key}' in [{section}]")

    certificate, key = parse_tls(parser, path)
    host, port = parse_listen(parser.get("api", "listen", fallback=DEFAULT_LISTEN).strip(), path,
                              tls=certificate is not None)
    name = parser.get("api", "name", fallback="").strip() or socket.gethostname()
    command = parse_command(parser.get("shutdown", "command", fallback=DEFAULT_COMMAND).strip(), "shutdown",
                            "sudo allows none", path)
    delay = parse_delay(parser.get("shutdown", "delay_seconds", fallback=str(DEFAULT_DELAY)).strip(), path)
    apps_command = parse_command(parser.get("apps", "command", fallback=DEFAULT_APPS_COMMAND).strip(), "apps",
                                 "RedhAPI adds 'list --json'", path)
    broadcast = parse_broadcast(parser.get("wake", "broadcast", fallback=DEFAULT_BROADCAST).strip(), path)
    wake_port = parse_wake_port(parser.get("wake", "port", fallback=str(DEFAULT_WAKE_PORT)).strip(), path)
    disks = parse_disks(parser.get("system", "disks", fallback="\n".join(DEFAULT_DISKS)), path)
    return Config(path, host, port, name, command, delay, parse_tokens(parser, path), apps_command,
                  broadcast, wake_port, disks, certificate, key)


def parse_command(command: str, section: str, why: str, path: str) -> str:
    if not os.path.isabs(command) or any(c.isspace() for c in command):
        raise ConfigError(f"{path}: command in [{section}] must be an absolute path, without arguments "
                          f"({why}), not '{command}'")
    return command


def parse_listen(value: str, path: str, tls: bool = False) -> Tuple[str, int]:
    """Without [tls], a loopback address only, behind Caddy; with it, any address of the server."""
    host, colon, port = value.rpartition(":")
    if not colon or not host or not PORT_RE.fullmatch(port):
        raise ConfigError(f"{path}: listen in [api] must be HOST:PORT, such as {DEFAULT_LISTEN}, not '{value}'")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    number = int(port)
    if not 1 <= number <= 65535:
        raise ConfigError(f"{path}: the port in listen in [api] is out of range: {number}")
    if host != "localhost":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            if tls:
                raise ConfigError(f"{path}: listen in [api] must be an address of this server, such as 0.0.0.0 "
                                  f"for all of them, not {host}") from None
            loopback = False
        if not loopback and not tls:
            raise ConfigError(f"{path}: listen in [api] must be a loopback address such as 127.0.0.1, not {host}: "
                              "RedhAPI speaks plain HTTP, and Caddy serves it with TLS. With a certificate of "
                              "its own in [tls], it may listen on the network.")
    return host, number


def parse_tls(parser: configparser.ConfigParser, path: str) -> Tuple[Optional[str], Optional[str]]:
    """RedhAPI's own certificate and key, both or neither: absolute paths."""
    if not parser.has_section("tls"):
        return None, None
    files = []
    for key in ("certificate", "key"):
        value = parser.get("tls", key, fallback="").strip()
        if not os.path.isabs(value):
            raise ConfigError(f"{path}: {key} in [tls] must be an absolute path, such as "
                              f"{DEFAULT_CERTIFICATE if key == 'certificate' else DEFAULT_KEY}, not '{value}'")
        files.append(value)
    return files[0], files[1]


def parse_delay(value: str, path: str) -> float:
    try:
        delay = float(value)
    except ValueError:
        delay = -1.0
    if not 0 <= delay <= MAX_DELAY:
        raise ConfigError(f"{path}: delay_seconds in [shutdown] must be a number from 0 to {MAX_DELAY:g}, "
                          f"not '{value}'")
    return delay


def parse_broadcast(value: str, path: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise ConfigError(f"{path}: broadcast in [wake] must be an IPv4 address, such as {DEFAULT_BROADCAST} "
                          f"or the network's own, such as 192.168.1.255, not '{value}'") from None


def parse_wake_port(value: str, path: str) -> int:
    if not PORT_RE.fullmatch(value) or not 1 <= int(value) <= 65535:
        raise ConfigError(f"{path}: port in [wake] must be a number from 1 to 65535, not '{value}'")
    return int(value)


def parse_disks(value: str, path: str) -> Tuple[str, ...]:
    """One absolute path per line; none at all is fine too."""
    disks = tuple(line.strip() for line in value.splitlines() if line.strip())
    for disk in disks:
        if not os.path.isabs(disk):
            raise ConfigError(f"{path}: disks in [system] are absolute paths, one per line, not '{disk}'")
    return disks


def parse_tokens(parser: configparser.ConfigParser, path: str) -> Dict[str, str]:
    tokens: Dict[str, str] = {}
    if parser.has_section("tokens"):
        for name, value in parser.items("tokens"):
            digest = value.strip().lower()
            if not TOKEN_NAME_RE.fullmatch(name):
                raise ConfigError(f"{path}: '{name}' in [tokens] is not a token name: "
                                  "a-z, 0-9, - and _, up to 32 characters")
            if not SHA256_RE.fullmatch(digest):
                raise ConfigError(f"{path}: {name} in [tokens] is not a SHA-256 in hex; "
                                  f"'redh-api token {name}' prints a line to use")
            if digest in tokens:
                raise ConfigError(f"{path}: {tokens[digest]} and {name} in [tokens] have the same hash")
            tokens[digest] = name
    if not tokens:
        raise ConfigError(f"{path}: no tokens in [tokens]; 'redh-api token NAME' makes one")
    return tokens
