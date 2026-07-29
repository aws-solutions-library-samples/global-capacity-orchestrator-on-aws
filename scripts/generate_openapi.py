#!/usr/bin/env python3
"""Generate the committed OpenAPI documents for every GCO HTTP service.

Each FastAPI application already knows its own schema; this script asks each one
for ``app.openapi()`` and writes it to ``docs/openapi/<service>.json`` so the
API surface is reviewable in diffs and consumable by client generators without
running a cluster.

Usage::

    python scripts/generate_openapi.py           # write the documents
    python scripts/generate_openapi.py --check   # fail if anything is stale

``--check`` is what CI runs: it regenerates in memory and compares, so a route
added without regenerating is caught in review rather than shipping a schema
that disagrees with the code.

The applications are imported, not deployed. ``GCO_DEV_MODE`` is set so the
authentication middleware's constructor does not log a configuration error for a
missing signing secret, and no AWS call is made during import.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "docs" / "openapi"

#: Service name -> module exposing a module-level ``app``. The service name is
#: the filename stem of the generated document and matches the Kubernetes
#: Service name the application is served under.
SERVICES: dict[str, str] = {
    "manifest-processor": "gco.services.manifest_api",
    "health-monitor": "gco.services.health_api",
    "inference-proxy": "gco.services.inference_api",
    "cost-monitor": "gco.services.cost_api",
}

#: Routes FastAPI adds for its own interactive documentation. They are real
#: routes but they are not part of GCO's API contract, and no API Gateway
#: forwards them, so they are excluded to keep the documents about the service.
_DOC_ROUTE_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})


def _load_app(module_path: str) -> Any:
    """Import a service module and return its FastAPI application."""
    os.environ.setdefault("GCO_DEV_MODE", "true")
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    module = importlib.import_module(module_path)
    return module.app


def build_document(module_path: str) -> dict[str, Any]:
    """Return one service's OpenAPI document, minus FastAPI's own doc routes."""
    app = _load_app(module_path)
    document: dict[str, Any] = json.loads(json.dumps(app.openapi()))
    for path in _DOC_ROUTE_PATHS:
        document.get("paths", {}).pop(path, None)
    return document


def render(document: dict[str, Any]) -> str:
    """Serialize deterministically so regeneration produces a stable diff."""
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if any committed document is stale.",
    )
    args = parser.parse_args(argv)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []

    for service, module_path in sorted(SERVICES.items()):
        rendered = render(build_document(module_path))
        target = OUTPUT_DIR / f"{service}.json"
        current = target.read_text(encoding="utf-8") if target.is_file() else None

        if args.check:
            if current != rendered:
                stale.append(service)
                state = "missing" if current is None else "stale"
                print(f"{target.relative_to(REPO_ROOT)}: {state}")
            continue

        if current == rendered:
            print(f"{target.relative_to(REPO_ROOT)}: unchanged")
        else:
            target.write_text(rendered, encoding="utf-8")
            print(f"{target.relative_to(REPO_ROOT)}: written")

    if stale:
        print(
            "\nRegenerate with: python scripts/generate_openapi.py",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
