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
SERVICE_NAMES: tuple[str, ...] = (
    "manifest-processor",
    "health-monitor",
    "inference-proxy",
    "cost-monitor",
)

#: Routes FastAPI adds for its own interactive documentation. They are real
#: routes but they are not part of GCO's API contract, and no API Gateway
#: forwards them, so they are excluded to keep the documents about the service.
_DOC_ROUTE_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})


def load_apps() -> dict[str, Any]:
    """Return ``{service name: FastAPI app}`` for every GCO HTTP service.

    The imports are literal rather than resolved through
    ``importlib.import_module``: there is no reason for this mapping to be
    dynamic, and a static import set is both easier to follow and impossible to
    redirect at a module that was never intended to be loaded here.

    Importing does not start a server or call AWS. ``GCO_DEV_MODE`` is set first
    so the authentication middleware's constructor does not log a configuration
    error about the signing secret it does not need for schema generation.
    """
    os.environ.setdefault("GCO_DEV_MODE", "true")
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    from gco.services import cost_api, health_api, inference_api, manifest_api

    apps = {
        "manifest-processor": manifest_api.app,
        "health-monitor": health_api.app,
        "inference-proxy": inference_api.app,
        "cost-monitor": cost_api.app,
    }
    assert tuple(apps) == SERVICE_NAMES, "SERVICE_NAMES is out of step with load_apps()"
    return apps


def build_document(app: Any) -> dict[str, Any]:
    """Return one application's OpenAPI document, minus FastAPI's doc routes.

    Round-tripped through JSON so the returned document contains only plain
    types, matching exactly what is written to disk.
    """
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

    for service, app in sorted(load_apps().items()):
        rendered = render(build_document(app))
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
