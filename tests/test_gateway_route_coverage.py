"""CI guard: the ALB HTTPRoute must send every live path to a Service that serves it.

The shared Gateway API ``HTTPRoute`` (``gco-system/gco-routes`` in
``lambda/kubectl-applier-simple/manifests/post-helm-gateway.yaml``) fronts three
applications with five path prefixes, the last being a ``/`` catch-all to the
manifest processor. Nothing verified that the prefixes actually corresponded to
the routes those Services implement, and one did not: ``/api/v1/metrics`` is
served only by the health monitor, so the catch-all sent it to the manifest
processor, which has no such route. The endpoint answered ``404`` through both
API Gateways while looking correctly configured in every manifest.

That is the failure mode this closes. It is invisible from either side on its
own — the manifest is valid YAML and passes kubeconform, and the health monitor
genuinely serves the route — so only comparing the two catches it.

Live paths come from the committed documents in ``docs/openapi/``, whose
freshness against the running applications is already enforced by
``tests/test_api_docs_coverage.py::test_committed_openapi_documents_are_current``.
Reading them here instead of importing the FastAPI apps keeps this test free of
the module-level app state that makes import-order bugs possible.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_MANIFEST = (
    PROJECT_ROOT / "lambda" / "kubectl-applier-simple" / "manifests" / "post-helm-gateway.yaml"
)
OPENAPI_DIR = PROJECT_ROOT / "docs" / "openapi"

#: Applications that are deliberately not reachable through the shared ALB.
#: The cost monitor is confined to manifest-processor traffic by a
#: NetworkPolicy; the manifest processor's ``/api/v1/cost/*`` routes are its
#: authenticated front. Its ``/internal/*`` paths must therefore NOT resolve to
#: it, which is asserted below rather than skipped.
_NOT_ALB_EXPOSED = {"cost-monitor"}

#: Paths more than one application implements, with the Service the ALB is
#: intended to pick and why. A PathPrefix cannot be split across Services, so
#: each of these is a decision, not an accident.
_EXPECTED_WINNER: dict[str, tuple[str, str]] = {
    "/api/v1/health": (
        "health-monitor",
        "cluster health is the Global Accelerator health-check target; the "
        "manifest processor's own /api/v1/health only reports its process",
    ),
    "/api/v1/status": (
        "manifest-processor",
        "the cross-region aggregator's /api/v1/global/status fans out to this "
        "path and reads templates_count, webhooks_count, resource_limits, and "
        "allowed_namespaces, which only the manifest processor returns",
    ),
    "/healthz": (
        "health-monitor",
        "the TargetGroupConfiguration health check probes /healthz, so it must "
        "report the health monitor rather than any single other pod",
    ),
    "/readyz": (
        "manifest-processor",
        "all four applications serve /readyz, so the catch-all's answer is a "
        "real readiness signal; no explicit rule is required",
    ),
    "/": (
        "manifest-processor",
        "the root descriptor of whichever Service owns the bulk of /api/v1",
    ),
}


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _load_httproute() -> dict[str, Any]:
    documents = [
        document
        for document in yaml.safe_load_all(GATEWAY_MANIFEST.read_text(encoding="utf-8"))
        if document and document.get("kind") == "HTTPRoute"
    ]
    assert len(documents) == 1, (
        f"expected exactly one HTTPRoute in {GATEWAY_MANIFEST.name}, found {len(documents)}"
    )
    route: dict[str, Any] = documents[0]
    return route


def _rules() -> list[tuple[str, str]]:
    """``[(path_prefix, service_name)]`` in document order.

    Only ``PathPrefix`` matches are understood. An ``Exact`` or
    ``RegularExpression`` match would change the precedence model this test
    implements, so encountering one fails rather than being ignored.
    """
    collected: list[tuple[str, str]] = []
    for rule in _load_httproute()["spec"]["rules"]:
        backends = rule["backendRefs"]
        assert len(backends) == 1, f"expected a single backendRef per rule, got {backends}"
        for match in rule["matches"]:
            path = match["path"]
            assert path["type"] == "PathPrefix", (
                f"unsupported path match type {path['type']!r} for {path.get('value')!r} — "
                "this guard models Gateway API PathPrefix precedence only"
            )
            collected.append((path["value"], backends[0]["name"]))
    return collected


def _resolve(path: str, rules: list[tuple[str, str]]) -> tuple[str, str] | None:
    """The winning ``(prefix, service)`` for ``path``.

    Implements Gateway API precedence: among matching ``PathPrefix`` rules the
    longest match wins, independent of document order. Prefixes match whole
    path segments, so ``/api/v1/metrics`` does not match ``/api/v1/metricsfoo``.
    """
    best: tuple[str, str] | None = None
    target = _segments(path)
    for prefix, service in rules:
        candidate = _segments(prefix)
        if target[: len(candidate)] != candidate:
            continue
        if best is None or len(candidate) > len(_segments(best[0])):
            best = (prefix, service)
    return best


def _live_paths() -> dict[str, set[str]]:
    """``{path: {app, ...}}`` from the committed OpenAPI documents."""
    documents = sorted(OPENAPI_DIR.glob("*.json"))
    assert documents, f"no OpenAPI documents found in {OPENAPI_DIR}"
    by_path: dict[str, set[str]] = {}
    for document in documents:
        schema = json.loads(document.read_text(encoding="utf-8"))
        for path in schema.get("paths", {}):
            by_path.setdefault(path, set()).add(document.stem)
    return by_path


@pytest.fixture(scope="module")
def rules() -> list[tuple[str, str]]:
    return _rules()


@pytest.fixture(scope="module")
def live() -> dict[str, set[str]]:
    return _live_paths()


def test_every_alb_exposed_path_reaches_a_service_that_serves_it(
    rules: list[tuple[str, str]], live: dict[str, set[str]]
) -> None:
    """No live path may resolve to a Service without that route.

    This is the assertion that /api/v1/metrics failed: served by the health
    monitor, delivered to the manifest processor, answered 404.
    """
    misrouted: list[str] = []
    for path, apps in sorted(live.items()):
        exposed = apps - _NOT_ALB_EXPOSED
        if not exposed:
            continue
        resolved = _resolve(path, rules)
        if resolved is None:
            misrouted.append(f"{path}: no rule matches (served by {', '.join(sorted(exposed))})")
            continue
        prefix, service = resolved
        if service not in exposed:
            misrouted.append(
                f"{path}: served by {', '.join(sorted(exposed))} but prefix "
                f"{prefix!r} sends it to {service}"
            )
    assert not misrouted, (
        "the shared HTTPRoute delivers paths to Services that do not serve them, "
        "so they answer 404 through the ALB — add an explicit rule in "
        "post-helm-gateway.yaml:\n  " + "\n  ".join(misrouted)
    )


def test_collision_paths_resolve_to_their_intended_service(
    rules: list[tuple[str, str]], live: dict[str, set[str]]
) -> None:
    """Paths several applications implement resolve to the documented winner."""
    wrong: list[str] = []
    for path, (expected, reason) in sorted(_EXPECTED_WINNER.items()):
        resolved = _resolve(path, rules)
        assert resolved is not None, f"{path} matches no rule"
        if resolved[1] != expected:
            wrong.append(f"{path}: expected {expected} ({reason}), got {resolved[1]}")
    assert not wrong, "shared-path routing changed:\n  " + "\n  ".join(wrong)


def test_every_collision_path_is_accounted_for(live: dict[str, set[str]]) -> None:
    """A newly shared path must be a deliberate choice, not an inherited default."""
    collisions = {
        path
        for path, apps in live.items()
        if len(apps - _NOT_ALB_EXPOSED) > 1  # noqa: PLR2004 — >1 app means a collision
    }
    undecided = sorted(collisions - set(_EXPECTED_WINNER))
    assert not undecided, (
        "these paths are now served by more than one ALB-exposed application, so "
        "the HTTPRoute silently picks one — record the intended Service and the "
        f"reason in _EXPECTED_WINNER: {undecided}"
    )


def test_cost_monitor_is_never_an_alb_backend(rules: list[tuple[str, str]]) -> None:
    """The cost monitor stays behind the manifest processor, not on the ALB."""
    backends = {service for _, service in rules}
    assert "cost-monitor" not in backends, (
        "cost-monitor must not be an HTTPRoute backend — it is confined to "
        "manifest-processor traffic by a NetworkPolicy and runs no auth "
        "middleware of its own; expose it through /api/v1/cost/* instead"
    )


def test_internal_paths_do_not_reach_the_cost_monitor(
    rules: list[tuple[str, str]], live: dict[str, set[str]]
) -> None:
    """``/internal/*`` must not become externally routable."""
    leaked = sorted(
        path
        for path, apps in live.items()
        if apps <= _NOT_ALB_EXPOSED
        and (resolved := _resolve(path, rules)) is not None
        and resolved[1] in _NOT_ALB_EXPOSED
    )
    assert not leaked, f"unauthenticated cluster-internal paths became ALB-routable: {leaked}"


def test_catch_all_is_last_and_unique(rules: list[tuple[str, str]]) -> None:
    """Exactly one ``/`` rule, in last position.

    Precedence is by specificity, not order, so this pins readability rather
    than behavior: the file should read the way traffic resolves. A second
    catch-all, or one placed earlier, is a sign the rule list was edited without
    that in mind.
    """
    catch_alls = [index for index, (prefix, _) in enumerate(rules) if prefix == "/"]
    assert len(catch_alls) == 1, f"expected exactly one '/' rule, found {len(catch_alls)}"
    assert catch_alls[0] == len(rules) - 1, (
        f"the '/' catch-all is rule {catch_alls[0] + 1} of {len(rules)}; move it last"
    )


def test_rules_are_ordered_most_specific_first(rules: list[tuple[str, str]]) -> None:
    """Prefix depth never increases down the list.

    Keeps the document order consistent with resolution order, so the routing
    table stays readable and would still be correct under a controller that
    consulted order instead of specificity.
    """
    depths = [len(_segments(prefix)) for prefix, _ in rules]
    out_of_order = [
        f"{rules[index][0]!r} (depth {depths[index]}) precedes "
        f"{rules[index + 1][0]!r} (depth {depths[index + 1]})"
        for index in range(len(depths) - 1)
        if depths[index] < depths[index + 1]
    ]
    assert not out_of_order, "HTTPRoute rules should read most-specific first:\n  " + "\n  ".join(
        out_of_order
    )
