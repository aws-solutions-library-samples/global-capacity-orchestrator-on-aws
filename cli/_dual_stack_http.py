"""HTTPS requests to the GCO APIs that do not stall on a client's broken IPv6.

The global API and the regional API bridges are dual-stack by default
(``api_gateway.ip_address_type``), so their host names resolve to IPv6 and
IPv4 addresses. urllib3 tries those addresses in the order ``getaddrinfo``
returns them and gives each one the whole connect timeout. On a host with a
global IPv6 address, that order puts every IPv6 address first, so on a network
that hands out IPv6 addresses but drops IPv6 traffic, a request would wait out
the connect timeout once per IPv6 address before it tried IPv4.

:func:`request` behaves like ``requests.request`` except for how it opens an
HTTPS connection. It alternates the address families, starting with the one
``getaddrinfo`` put first (RFC 8305, section 4), and gives every address but
the last at most :data:`ADDRESS_ATTEMPT_SECONDS` to accept the connection. A
broken IPv6 path then costs one short attempt before the first IPv4 address,
and a client without IPv4 still reaches the IPv6 addresses. The last address
keeps the caller's whole connect timeout, and so does the TLS handshake on
whichever address answered.
"""

from __future__ import annotations

import itertools
import socket
import sys
from collections.abc import Sequence
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    LocationParseError,
    NameResolutionError,
    NewConnectionError,
)
from urllib3.util.connection import allowed_gai_family

#: Longest one address may take to accept the TCP connection while another
#: address is left to try. A handshake that has not finished by then is most
#: likely on a path that drops packets, and the next address is of the other
#: family.
ADDRESS_ATTEMPT_SECONDS = 2.0

_AddressInfo = tuple[
    socket.AddressFamily,
    socket.SocketKind,
    int,
    str,
    tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes],
]


def _interleave_families(addresses: Sequence[_AddressInfo]) -> list[_AddressInfo]:
    """Alternate address families, keeping each family's own order (RFC 8305, section 4)."""
    if not addresses:
        return []
    first_family = addresses[0][0]
    preferred = [address for address in addresses if address[0] == first_family]
    others = [address for address in addresses if address[0] != first_family]
    return [
        address
        for pair in itertools.zip_longest(preferred, others)
        for address in pair
        if address is not None
    ]


def _connect(
    address: tuple[str, int],
    timeout: Any,
    source_address: tuple[str, int] | None,
    socket_options: Any,
) -> socket.socket:
    """``urllib3.util.connection.create_connection`` with interleaved, bounded attempts.

    ``timeout`` is the connection's connect timeout: seconds, or urllib3's
    "use the default" sentinel, which leaves the socket at
    ``socket.getdefaulttimeout()``.
    """
    host, port = address
    if host.startswith("["):
        host = host.strip("[]")
    try:
        host.encode("idna")
    except UnicodeError:
        raise LocationParseError(f"'{host}', label empty or too long") from None
    candidates = _interleave_families(
        socket.getaddrinfo(host, port, allowed_gai_family(), socket.SOCK_STREAM)
    )
    if not candidates:
        raise OSError("getaddrinfo returns an empty list")
    full_timeout = (
        float(timeout) if isinstance(timeout, (int, float)) else socket.getdefaulttimeout()
    )
    shortened = (
        ADDRESS_ATTEMPT_SECONDS
        if full_timeout is None
        else min(full_timeout, ADDRESS_ATTEMPT_SECONDS)
    )
    last_error: OSError | None = None
    for index, (family, kind, proto, _canonname, sockaddr) in enumerate(candidates):
        sock = None
        try:
            sock = socket.socket(family, kind, proto)
            for option in socket_options or ():
                sock.setsockopt(*option)
            sock.settimeout(full_timeout if index == len(candidates) - 1 else shortened)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            sock.settimeout(full_timeout)
            return sock
        except OSError as error:
            last_error = error
            if sock is not None:
                sock.close()
    assert last_error is not None  # every candidate either returned or failed
    raise last_error


class _InterleavingHTTPSConnection(HTTPSConnection):
    """An urllib3 HTTPS connection that opens its socket with :func:`_connect`."""

    def _new_conn(self) -> socket.socket:
        # The error mapping is urllib3's own HTTPConnection._new_conn, so
        # requests reports these failures exactly as before.
        try:
            sock = _connect(
                (self._dns_host, self.port),
                self.timeout,
                self.source_address,
                self.socket_options,
            )
        except socket.gaierror as error:
            raise NameResolutionError(self.host, self, error) from error
        except TimeoutError as error:
            raise ConnectTimeoutError(
                self,
                f"Connection to {self.host} timed out. (connect timeout={self.timeout})",
            ) from error
        except OSError as error:
            raise NewConnectionError(
                self, f"Failed to establish a new connection: {error}"
            ) from error
        sys.audit("http.client.connect", self, self.host, self.port)
        return sock


class _InterleavingHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _InterleavingHTTPSConnection


class _InterleavingAdapter(HTTPAdapter):
    """A requests adapter whose HTTPS pools use :class:`_InterleavingHTTPSConnection`."""

    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any
    ) -> None:
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            **self.poolmanager.pool_classes_by_scheme,
            "https": _InterleavingHTTPSConnectionPool,
        }


def request(method: str, url: str, **kwargs: Any) -> requests.Response:
    """Send one request like ``requests.request``, opening HTTPS connections with :func:`_connect`."""
    with requests.Session() as session:
        session.mount("https://", _InterleavingAdapter())
        return session.request(method=method, url=url, **kwargs)
