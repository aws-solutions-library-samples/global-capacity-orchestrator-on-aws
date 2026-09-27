"""Trust configuration for GCO's in-cluster HTTPS clients.

Every in-cluster hop GCO owns terminates TLS with a leaf certificate that
cert-manager issues from the ``gco-internal-ca`` ClusterIssuer. A client of
one of those hops verifies the peer against that CA alone, never the public
trust store: a certificate minted by any public CA is rejected, and so is a
peer whose certificate does not name the Service host the client dialled.

The CA reaches a client as the ``ca.crt`` key of a cert-manager Secret in the
client's own namespace. cert-manager writes the issuing CA's certificate into
every leaf Secret it signs, so a pod that already mounts its own serving
keypair can project just that one key, on its own, at ``/var/run/gco/ca`` --
the client container never sees a private key, and no extra trust
distribution machinery is needed.

Contexts are cached per CA file identity (path, inode, modification time,
size). Kubernetes updates a projected Secret by swapping a symlink, which
changes the resolved inode and mtime, so a rotated CA is picked up by the
next context request without restarting the process; long-lived clients
that captured a context keep it until they are rebuilt.
"""

from __future__ import annotations

import os
import ssl
import threading
from pathlib import Path
from urllib.parse import urlsplit

INTERNAL_CA_FILE_ENV = "GCO_INTERNAL_CA_FILE"
DEFAULT_INTERNAL_CA_FILE = "/var/run/gco/ca/ca.crt"

_CacheKey = tuple[str, int, int, int]
_context_cache: dict[str, tuple[_CacheKey, ssl.SSLContext]] = {}
_cache_lock = threading.Lock()


class InternalTLSError(RuntimeError):
    """The GCO internal CA bundle is missing, unreadable, or not a usable CA."""


def internal_ca_file() -> Path:
    """Return the CA bundle path: ``GCO_INTERNAL_CA_FILE`` or the projected default."""
    configured = os.getenv(INTERNAL_CA_FILE_ENV, "").strip()
    return Path(configured or DEFAULT_INTERNAL_CA_FILE)


def _cache_key(path: Path) -> _CacheKey:
    try:
        stat = path.stat()
    except OSError as exc:
        raise InternalTLSError(
            f"GCO internal CA bundle is not readable at {path}: {exc.strerror or exc}"
        ) from exc
    return (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)


def _build_context(path: Path) -> ssl.SSLContext:
    """Client context that trusts exactly the CA bundle at ``path``."""
    try:
        # With an explicit cafile the default context loads no system anchors:
        # the internal CA is the only trust root.
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(path))
    except (OSError, ssl.SSLError) as exc:
        raise InternalTLSError(
            f"GCO internal CA bundle at {path} is not a usable CA: {exc}"
        ) from exc
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def internal_ssl_context(ca_file: str | os.PathLike[str] | None = None) -> ssl.SSLContext:
    """Return a verifying client context that trusts only the GCO internal CA.

    Hostname verification stays on (the default for ``Purpose.SERVER_AUTH``),
    so the peer must present a certificate for the exact Service name dialled.
    Raises :class:`InternalTLSError` when the bundle is missing or invalid, so
    callers fail closed instead of silently falling back to another trust store.
    """
    path = Path(ca_file) if ca_file is not None else internal_ca_file()
    key = _cache_key(path)
    with _cache_lock:
        cached = _context_cache.get(key[0])
        if cached is not None and cached[0] == key:
            return cached[1]
    context = _build_context(path)
    with _cache_lock:
        _context_cache[key[0]] = (key, context)
    return context


def verify_for_url(
    url: str, ca_file: str | os.PathLike[str] | None = None
) -> ssl.SSLContext | bool:
    """Return the httpx2 ``verify=`` value for an in-cluster URL.

    ``https`` URLs verify against the internal CA. Any other scheme -- a local
    or CI override such as ``http://127.0.0.1:8080`` -- opens no TLS session,
    so the library default is returned and no CA bundle is required.
    """
    if urlsplit(url).scheme.lower() == "https":
        return internal_ssl_context(ca_file)
    return True


def clear_cache() -> None:
    """Forget cached contexts (tests, and callers that rebuild after an error)."""
    with _cache_lock:
        _context_cache.clear()
