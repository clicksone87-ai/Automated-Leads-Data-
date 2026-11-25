"""HTTP client for visiting unknown websites: public addresses only, bounded downloads."""

from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Iterable
from typing import Any

import anyio
import httpcore
import httpx

MAX_BYTES = 2_000_000


class BlockedAddressError(httpcore.ConnectError):
    """The host resolves to a private or otherwise non-public address."""


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        # Addresses that carry an IPv4 one inside (mapped, 6to4, NAT64) reach that host.
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip in ipaddress.ip_network("64:ff9b::/96"):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            return is_public(str(embedded))
    return ip.is_global and not ip.is_multicast


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Resolves a name once, refuses non-public results and connects to that exact address.

    Checking here covers redirects too, and a name cannot change its answer between the
    check and the connection.
    """

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if port not in (80, 443):
            raise BlockedAddressError(f"blocked port {port}")
        try:
            with anyio.fail_after(timeout):
                found = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(str(exc)) from exc
        except (OSError, UnicodeError) as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        addresses = [str(item[4][0]) for item in found]
        if not addresses or not all(is_public(a) for a in addresses):
            raise BlockedAddressError("blocked address")
        # IPv4 first: many home connections have no working IPv6.
        addresses.sort(key=lambda a: ":" in a)
        return await self._inner.connect_tcp(
            addresses[0], port, timeout, local_address, socket_options
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.AsyncNetworkStream:
        raise BlockedAddressError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PublicOnlyTransport(httpx.AsyncHTTPTransport):
    def __init__(self) -> None:
        super().__init__()
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(cafile=_ca_file()),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=PublicOnlyBackend(),
        )


def _ca_file() -> str:
    import certifi

    return certifi.where()


async def read_limited(response: httpx.Response, limit: int = MAX_BYTES) -> str:
    """Decoded body, cut at ``limit`` bytes."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk[: limit - size])
        size += len(chunks[-1])
        if size >= limit:
            break
    data = b"".join(chunks)
    try:
        return data.decode(response.encoding or "utf-8", errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")
