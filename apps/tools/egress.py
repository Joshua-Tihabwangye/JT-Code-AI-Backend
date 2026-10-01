"""SSRF-safe outbound HTTP for tools.

Every tool request goes through :func:`safe_request`:

* HTTPS only, default port, no embedded credentials;
* the hostname is resolved once and **every** address must be public (no
  private, loopback, link-local/metadata, reserved, multicast or unspecified);
* the connection is pinned to the validated IP (TLS still verifies the
  hostname via SNI), defeating DNS-rebinding between check and connect;
* redirects are followed manually and each hop is re-validated;
* optional host allowlist, global denylist, response size cap and timeout.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from django.conf import settings


class EgressDenied(PermissionError):
    """The destination is not an allowed public HTTPS endpoint."""


class EgressError(RuntimeError):
    """The request failed (timeout, transport error, oversize response)."""


@dataclass
class EgressResponse:
    status_code: int
    headers: httpx.Headers
    content: bytes
    url: str

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json

        return json.loads(self.content or b"null")


def _host_matches(host: str, patterns: list[str] | tuple[str, ...]) -> bool:
    for pattern in patterns:
        pattern = pattern.lower().rstrip(".")
        if pattern.startswith("*.") and host.endswith(pattern[1:]) and host != pattern[2:]:
            return True
        if host == pattern:
            return True
    return False


def resolve_public(host: str) -> list[str]:
    """Resolve ``host`` and require every address to be publicly routable."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EgressDenied(f"Cannot resolve {host!r}.") from exc
    addresses = sorted({str(info[4][0]) for info in infos})
    if not addresses:
        raise EgressDenied(f"{host!r} has no addresses.")
    for address in addresses:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(address.split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped  # judge IPv4-mapped IPv6 by its embedded IPv4 address
        if not ip.is_global or ip.is_multicast:
            raise EgressDenied(f"{host!r} resolves to a non-public address.")
    return addresses


def validate_url(url: str, *, allowed_hosts: list[str] | tuple[str, ...] | None = None) -> tuple[str, str]:
    """Return ``(host, pinned_ip)`` for an allowed URL or raise :class:`EgressDenied`."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise EgressDenied("Only https:// URLs without credentials are allowed.")
    if parts.port not in (None, 443):
        raise EgressDenied("Only the default HTTPS port is allowed.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise EgressDenied("IP-literal destinations are not allowed; use a DNS name.")
    if _host_matches(host, tuple(settings.TOOL_EGRESS_DENYLIST)):
        raise EgressDenied(f"{host!r} is on the egress denylist.")
    if allowed_hosts is not None and not _host_matches(host, allowed_hosts):
        raise EgressDenied(f"{host!r} is not in this tool's allowed hosts.")
    return host, resolve_public(host)[0]


def _pinned_url(url: str, ip: str) -> str:
    parts = urlsplit(url)
    literal = f"[{ip}]" if ":" in ip else ip
    return urlunsplit((parts.scheme, literal, parts.path or "/", parts.query, ""))


def http_client(timeout: float) -> httpx.Client:
    """Outbound client (tests replace this with a mock transport)."""
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)), follow_redirects=False)


def safe_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json: Any = None,
    content: bytes | None = None,
    params: dict[str, Any] | None = None,
    allowed_hosts: list[str] | tuple[str, ...] | None = None,
    timeout: float | None = None,
    max_bytes: int | None = None,
    max_redirects: int = 3,
) -> EgressResponse:
    timeout = float(timeout or settings.EXTERNAL_API_TIMEOUT_SECONDS)
    limit = int(max_bytes or settings.TOOL_MAX_RESPONSE_BYTES)
    current = url
    if params:
        current = str(httpx.URL(url, params=params))
    for _hop in range(max_redirects + 1):
        host, ip = validate_url(current, allowed_hosts=allowed_hosts)
        request_headers = {**(headers or {}), "Host": host}
        try:
            with (
                http_client(timeout) as client,
                client.stream(
                    method,
                    _pinned_url(current, ip),
                    headers=request_headers,
                    json=json,
                    content=content,
                    extensions={"sni_hostname": host},
                ) as response,
            ):
                if response.status_code in (301, 302, 303, 307, 308) and "location" in response.headers:
                    current = urljoin(current, response.headers["location"])
                    if response.status_code == 303:
                        method, json, content = "GET", None, None
                    continue
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise EgressError(f"Response exceeded the {limit}-byte limit.")
                return EgressResponse(response.status_code, response.headers, bytes(body), current)
        except httpx.TimeoutException as exc:
            raise EgressError(f"Request to {host} timed out after {timeout}s.") from exc
        except httpx.TransportError as exc:
            raise EgressError(f"Request to {host} failed: {type(exc).__name__}.") from exc
    raise EgressDenied("Too many redirects.")
