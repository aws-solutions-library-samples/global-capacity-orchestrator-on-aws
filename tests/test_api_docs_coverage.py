"""Guard ``docs/API.md`` and ``docs/openapi/`` against the live HTTP surface.

``docs/API.md`` was titled "GCO Manifest Processor API" and documented only the
manifest processor's routes, while three other applications served real traffic:
the inference proxy (``/inference/*``), the health monitor (``/api/v1/health``,
``/api/v1/metrics``), and the cluster-internal cost monitor (``/internal/*``).
Nothing detected the omission, and nothing would have detected a newly added
route going undocumented either.

This enumerates every route the four applications actually serve and asserts the
documentation covers it, in both directions:

* **Forward** — every live ``(method, path)`` appears in ``docs/API.md``. Adding
  a route now fails here until it is documented.
* **Reverse** — every endpoint documented in a Markdown table exists on some
  application, so deleting a route surfaces the stale table row. Documented
  surfaces that are deliberately not FastAPI routes (the cross-region aggregator
  Lambda, the Mooncake proxy's ConfigMap-hosted app) are allowlisted below with
  the reason.

Paths are compared with parameter *names* erased, so the tables may keep the
readable ``/api/v1/jobs/{ns}/{name}`` while the code declares
``/api/v1/jobs/{namespace}/{name}``. Only the structure is pinned.

The committed OpenAPI documents are checked for staleness here too, so a route
change cannot land with schemas that disagree with the code.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
API_DOC = PROJECT_ROOT / "docs" / "API.md"
GENERATOR = PROJECT_ROOT / "scripts" / "generate_openapi.py"

_HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
_METHOD_ALTERNATION = "|".join(_HTTP_METHODS)
_METHOD_RE = re.compile(rf"\b({_METHOD_ALTERNATION})\b")
#: A whole cell (or backticked token) that is only a method list: ``GET``,
#: ``GET, HEAD, POST``, ``GET|HEAD|POST``.
_METHOD_LIST_RE = re.compile(
    rf"(?:{_METHOD_ALTERNATION})(?:\s*[,|]\s*(?:{_METHOD_ALTERNATION}))*",
)
#: A backticked token pairing a method list with its path, as used by the
#: health, observability, and cluster-internal tables: ``GET /api/v1/health``.
_METHOD_PATH_RE = re.compile(
    rf"^(?P<methods>(?:{_METHOD_ALTERNATION})(?:\s*[,|]\s*(?:{_METHOD_ALTERNATION}))*)"
    r"\s+(?P<path>/\S*)$"
)
_BACKTICKED_RE = re.compile(r"`([^`]+)`")
#: A bare path inside an ``http`` code fence, where backticks are not used.
_BARE_PATH_RE = re.compile(
    r"(?:^|\s)(/(?:api/v1|inference|internal|healthz|readyz|metrics)[^\s`,)]*)"
)

#: Documented endpoints that are intentionally not routes on any FastAPI app.
#: Each entry names what serves it instead; an unexplained entry here would be
#: indistinguishable from a stale table row, which is what this test exists to
#: catch.
_NON_FASTAPI_ENDPOINTS: dict[str, str] = {
    "/api/v1/global/jobs": "cross-region aggregator Lambda at the global API Gateway",
    "/api/v1/global/health": "cross-region aggregator Lambda at the global API Gateway",
    "/api/v1/global/status": "cross-region aggregator Lambda at the global API Gateway",
    "/health": "Mooncake prefill/decode proxy (gco/services/mooncake_pd_proxy.py)",
    "/instances/add": "Mooncake prefill/decode proxy admin route",
    "/{}": "Mooncake prefill/decode proxy catch-all dispatch",
}


def _load_generator() -> Any:
    """Import ``scripts/generate_openapi.py`` (not an importable package)."""
    spec = importlib.util.spec_from_file_location("gco_generate_openapi", GENERATOR)
    assert spec and spec.loader, f"could not load {GENERATOR}"
    module = importlib.util.module_from_spec(spec)
    # Register before executing so any postponed annotations the script uses
    # resolve through ``sys.modules`` rather than raising.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _normalize(path: str) -> str:
    """Erase path-parameter names so doc shorthand matches declared routes.

    ``/api/v1/jobs/{ns}/{name}`` and ``/api/v1/jobs/{namespace}/{name}`` both
    become ``/api/v1/jobs/{}/{}``. A trailing slash is dropped so ``/inference``
    and ``/inference/`` compare equal.
    """
    collapsed = re.sub(r"\{[^}]*\}", "{}", path)
    return collapsed.rstrip("/") or "/"


def _live_operations() -> dict[str, set[str]]:
    """Return ``{normalized_path: {METHOD, ...}}`` across every application."""
    os.environ.setdefault("GCO_DEV_MODE", "true")
    generator = _load_generator()

    operations: dict[str, set[str]] = {}
    for app in generator.load_apps().values():
        document = generator.build_document(app)
        for raw_path, methods in document.get("paths", {}).items():
            key = _normalize(raw_path)
            operations.setdefault(key, set()).update(m.upper() for m in methods)

        # ``/metrics`` is mounted as a plain Starlette route, so it never appears
        # in the OpenAPI document even though it serves live scrape traffic.
        for route in app.routes:
            path = getattr(route, "path", None)
            route_methods = getattr(route, "methods", None)
            if not path or not route_methods:
                continue
            if path in generator._DOC_ROUTE_PATHS or type(route).__name__ == "APIRoute":
                continue
            key = _normalize(path)
            operations.setdefault(key, set()).update(
                m.upper() for m in route_methods if m != "OPTIONS"
            )
    return operations


def _split_methods(raw: str) -> set[str]:
    """Turn ``"GET, HEAD, POST"`` or ``"GET|HEAD|POST"`` into a method set."""
    return {m.upper() for m in _METHOD_RE.findall(raw)}


def _table_row_endpoints(line: str) -> list[tuple[set[str], str]]:
    """Extract ``(methods, path)`` pairs a table row *declares* as endpoints.

    Two shapes appear in this document, and only these two are treated as
    declarations, so a path mentioned incidentally in a prose cell is ignored:

    * a cell whose backticked token pairs both, ``` `GET /api/v1/health` ```;
    * a cell that is only a method list, with the path in the next cell,
      ``| GET | `/api/v1/jobs` | ... |``.
    """
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    found: list[tuple[set[str], str]] = []
    for index, cell in enumerate(cells):
        for token in _BACKTICKED_RE.findall(cell):
            match = _METHOD_PATH_RE.match(token.strip())
            if match:
                found.append((_split_methods(match.group("methods")), match.group("path")))

        if _METHOD_LIST_RE.fullmatch(cell.replace("`", "").strip()) and index + 1 < len(cells):
            for token in _BACKTICKED_RE.findall(cells[index + 1]):
                candidate = token.strip()
                if candidate.startswith("/"):
                    found.append((_split_methods(cell), candidate))
                    break
    return [(methods, path) for methods, path in found if "*" not in path]


def _documented_methods_by_path(text: str) -> dict[str, set[str]]:
    """Collect ``{normalized_path: {METHOD, ...}}`` documented anywhere.

    Table rows are parsed cell-by-cell; ``http`` code fences pair the method and
    path on one bare line (``GET /api/v1/jobs``) and are read per line.
    """
    found: dict[str, set[str]] = {}

    def record(methods: set[str], raw_path: str) -> None:
        found.setdefault(_normalize(raw_path), set()).update(methods)

    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            for methods, path in _table_row_endpoints(line):
                record(methods, path)
            continue

        methods = _split_methods(line)
        for token in _BACKTICKED_RE.findall(line):
            match = _METHOD_PATH_RE.match(token.strip())
            if match:
                record(_split_methods(match.group("methods")), match.group("path"))
            elif token.startswith("/") and "*" not in token:
                record(methods, token)
        for raw_path in _BARE_PATH_RE.findall(line):
            if "*" not in raw_path:
                record(methods, raw_path)
    return found


def _documented_table_endpoints(text: str) -> dict[str, set[str]]:
    """Endpoints *declared* in Markdown tables, for the reverse check.

    Only rows that name an HTTP method contribute, which excludes the
    surface-map tables (their first column is a path *prefix*, with no method)
    while keeping every genuine endpoint table.
    """
    found: dict[str, set[str]] = {}
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        for methods, path in _table_row_endpoints(line):
            found.setdefault(_normalize(path), set()).update(methods)
    return found


@pytest.fixture(scope="module")
def doc_text() -> str:
    return API_DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def live() -> dict[str, set[str]]:
    return _live_operations()


def test_every_live_path_is_documented(live: dict[str, set[str]], doc_text: str) -> None:
    """Every path served by an application appears in docs/API.md."""
    documented = _documented_methods_by_path(doc_text)
    missing = sorted(path for path in live if path not in documented)
    assert not missing, (
        "these live paths are not documented in docs/API.md: "
        + ", ".join(missing)
        + " — add them to the endpoint tables"
    )


def test_every_live_method_is_documented(live: dict[str, set[str]], doc_text: str) -> None:
    """Each documented path lists every method the application serves on it."""
    documented = _documented_methods_by_path(doc_text)
    gaps: list[str] = []
    for path, methods in sorted(live.items()):
        # HEAD is implied wherever GET is documented; FastAPI registers it
        # explicitly on the inference proxy but the tables say "GET, HEAD, POST".
        undocumented = methods - documented.get(path, set()) - {"HEAD"}
        if undocumented:
            gaps.append(f"{path}: {', '.join(sorted(undocumented))}")
    assert not gaps, "docs/API.md omits these methods: " + "; ".join(gaps)


def test_documented_endpoints_exist(live: dict[str, set[str]], doc_text: str) -> None:
    """Every endpoint in a Markdown table is served by an app or allowlisted."""
    allowlisted = {_normalize(path) for path in _NON_FASTAPI_ENDPOINTS}
    stale = sorted(
        path
        for path in _documented_table_endpoints(doc_text)
        if path not in live and path not in allowlisted
    )
    assert not stale, (
        "docs/API.md documents endpoints that no application serves: "
        + ", ".join(stale)
        + " — remove the row, or add it to _NON_FASTAPI_ENDPOINTS with the "
        "service that does serve it"
    )


def test_committed_openapi_documents_are_current() -> None:
    """The checked-in OpenAPI documents match what the applications produce."""
    generator = _load_generator()
    assert generator.main(["--check"]) == 0, (
        "committed OpenAPI documents are stale — regenerate with "
        "`python scripts/generate_openapi.py`"
    )


def test_every_service_has_a_committed_openapi_document() -> None:
    """Adding a service to the generator requires committing its document."""
    generator = _load_generator()
    for service in generator.SERVICE_NAMES:
        target = PROJECT_ROOT / "docs" / "openapi" / f"{service}.json"
        assert target.is_file(), f"missing generated document: {target.relative_to(PROJECT_ROOT)}"
