"""Report which event loop and HTTP parser uvicorn's ``auto`` selection resolved.

The four uvicorn-served images ship ``uvloop`` and ``httptools`` (see the
``image-*`` groups in pyproject.toml) so uvicorn's default ``loop="auto"`` /
``http="auto"`` selection picks the libuv event loop and the llhttp parser.
The services deliberately keep those defaults -- a developer checkout without
the wheels still serves on the stdlib loop and h11 -- and instead log what
was resolved at startup, so a pod log states which implementations are in
effect. The integration:docker:* jobs assert the fast path inside every
built image; this module only makes the resolution observable.
"""

from __future__ import annotations

from collections.abc import Callable


def _qualified_name(obj: Callable[..., object]) -> str:
    """Dotted name of a class or factory function (both are callables with a qualname)."""
    return f"{obj.__module__}.{obj.__qualname__}"


def describe_uvicorn_runtime() -> tuple[str, str]:
    """Return ``(loop, http)``: the implementations uvicorn's ``auto`` mode resolves to.

    Both names come from the very objects uvicorn consults, not from a
    parallel ``import`` probe, so the log line cannot disagree with the server.
    """
    from uvicorn.loops.auto import auto_loop_factory
    from uvicorn.protocols.http.auto import AutoHTTPProtocol

    return _qualified_name(auto_loop_factory()), _qualified_name(AutoHTTPProtocol)
