"""Checks for user-supplied model base URLs (SSRF guard).

A user may point a provider at a self-hosted gateway or proxy. Such a URL must be http(s), carry no
credentials, query or fragment, and -- unless the user is an admin or the server sets
``NEWSD_LLM_ALLOW_PRIVATE_URLS=1`` -- every address its host resolves to must be public (no
loopback, private, link-local, CGNAT, multicast or reserved ranges). The check runs when the URL is
saved/tested and again before every request (DNS may change between the two). Redirects are never
followed and proxies from the environment are ignored (see ``LLMClient``).
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

from .client import LLMError

MAX_URL = 512


def _system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos})


# Tests replace this with a fake: (host, port) -> list of IP strings.
RESOLVER = _system_resolver


def private_allowed() -> bool:
    return os.environ.get("NEWSD_LLM_ALLOW_PRIVATE_URLS", "").strip().lower() in ("1", "true", "yes", "on")


def normalize(url: str) -> str:
    """Syntax check only. Returns the URL without a trailing slash; raises LLMError(config)."""
    u = (url or "").strip()
    if not u:
        raise LLMError("config", "base URL is empty")
    if len(u) > MAX_URL or any(c.isspace() for c in u):
        raise LLMError("config", "base URL is invalid")
    try:
        p = urlsplit(u)
        port = p.port
    except ValueError as exc:
        raise LLMError("config", "base URL is invalid") from exc
    if p.scheme not in ("http", "https") or not p.hostname:
        raise LLMError("config", "base URL must start with http:// or https:// and name a host")
    if p.username is not None or p.password is not None or "@" in p.netloc:
        raise LLMError("config", "base URL must not contain credentials")
    if p.query or p.fragment:
        raise LLMError("config", "base URL must not contain a query string or fragment")
    if port is not None and not (0 < port < 65536):
        raise LLMError("config", "base URL is invalid")
    return u.rstrip("/")


def _public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def check(url: str, *, allow_private: bool = False) -> str:
    """normalize() plus the address check. Returns the normalized URL; raises LLMError(config)."""
    u = normalize(url)
    if allow_private or private_allowed():
        return u
    p = urlsplit(u)
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        ips = RESOLVER(p.hostname, port)
    except (OSError, UnicodeError) as exc:
        raise LLMError("config", f"base URL host {p.hostname!r} does not resolve") from exc
    if not ips:
        raise LLMError("config", f"base URL host {p.hostname!r} does not resolve")
    if not all(_public(ip) for ip in ips):
        raise LLMError("config", "base URL points to a private or internal address; only an admin may use one")
    return u
