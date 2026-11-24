"""RedhAPI's own certificate: made once with openssl, served without Caddy, pinned by Redh Control.

A server without the homelab's CA gets a self-signed certificate of its own. Redh Control trusts
exactly that certificate, by its SHA-256, which the pairing code carries along with a token."""

from __future__ import annotations

import hashlib
import os
import ssl
import subprocess
from typing import Sequence

DAYS = 3650  # ten years: a new certificate breaks every pin
PAIRING_PREFIX = "redh1"


class CertificateError(Exception):
    """The certificate can't be made; the message says why."""


def make(certificate: str, key: str, name: str, days: int = DAYS, openssl: str = "openssl",
         group: int = -1) -> None:
    """An EC key (P-256), readable by its owner and the group only, and a self-signed certificate
    for it, valid for `days`, with the name as its common name."""
    for path in (certificate, key):
        if os.path.lexists(path):
            raise CertificateError(f"{path} exists already; a new certificate would break every pairing with "
                                   "it, so remove it first if that's what you want")
    descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        run([openssl, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", key])
        run([openssl, "req", "-new", "-x509", "-key", key, "-days", str(days), "-subj", f"/CN={name}",
             "-out", certificate])
    except CertificateError:
        for path in (certificate, key):
            if os.path.exists(path):
                os.remove(path)
        raise
    os.chmod(key, 0o640)
    os.chmod(certificate, 0o644)
    if group >= 0:
        os.chown(key, 0, group)
        os.chown(certificate, 0, group)


def run(command: Sequence[str]) -> None:
    try:
        result = subprocess.run(list(command), capture_output=True, text=True)
    except OSError as e:
        raise CertificateError(f"{command[0]} can't be run: {e.strerror or e}") from None
    if result.returncode != 0:
        raise CertificateError(f"{' '.join(command[:2])} failed: {result.stderr.strip() or result.returncode}")


def der(certificate: str) -> bytes:
    with open(certificate, encoding="ascii") as f:
        return ssl.PEM_cert_to_DER_cert(f.read())


def fingerprint(certificate: str) -> str:
    """The SHA-256 of the certificate, as openssl and Redh Control write it: AB:CD:…, 32 pairs."""
    digest = hashlib.sha256(der(certificate)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def pairing_code(token: str, certificate: str) -> str:
    """What Redh Control takes in one paste: the certificate's SHA-256 and the token,
    redh1:<64 hex digits>:<token>."""
    return f"{PAIRING_PREFIX}:{hashlib.sha256(der(certificate)).hexdigest()}:{token}"


def server_context(certificate: str, key: str) -> ssl.SSLContext:
    """TLS 1.2 or newer, with the certificate and its key."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    return context


def client_context(certificate: str) -> ssl.SSLContext:
    """For RedhAPI's own check: trusts exactly its certificate, whatever its name, as the app does."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cafile=certificate)
    return context
