#!/usr/bin/env python3
"""Generate the committed OpenAPI document for the in-cluster Gateway (the internal ALB).

Inside a region every request from the API Gateway proxies lands on one
internal ALB: the Kubernetes Gateway ``gco-system/gco-gateway``, whose single
``HTTPRoute`` (``gco-routes`` in
``lambda/kubectl-applier-simple/manifests/post-helm-gateway.yaml``) picks a
Service by longest path prefix. The Gateway has no schema of its own — it is a
routing table over the FastAPI services — so this script composes one: for
every path in the committed service documents (``docs/openapi/<service>.json``)
it resolves the HTTPRoute rule that wins and keeps the operation if, and only
if, the winning Service is the one that serves it. The result,
``docs/openapi/cluster-gateway.json``, is the HTTP surface the ALB actually
exposes: each operation is tagged with its backend Service and carries the rule
that delivers it (``x-gco-route``); paths a Service implements but never
receives are listed under ``x-gco-unreachable``; Services that are not ALB
backends at all (the cost monitor) under ``x-gco-not-exposed``.

Usage::

    python scripts/generate_cluster_gateway_openapi.py           # write the document
    python scripts/generate_cluster_gateway_openapi.py --check   # fail if it is stale

The same resolution is enforced as a test by
``tests/test_gateway_route_coverage.py``; this document is its reviewable form,
and the interaction diagram in ``diagrams/api_specs/`` is drawn from it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.generate_openapi import SERVICE_NAMES  # noqa: E402

OUTPUT_DIR = REPO_ROOT / "docs" / "openapi"
DOCUMENT_NAME = "cluster-gateway"
GATEWAY_MANIFEST = Path("lambda/kubectl-applier-simple/manifests/post-helm-gateway.yaml")
GENERATOR = "scripts/generate_cluster_gateway_openapi.py"
API_VERSION = "1.0.0"

#: Header carrying the request-bound HMAC the proxies mint; the companion
#: headers complete the envelope the auth middleware verifies.
HMAC_SIGNATURE_HEADER = "x-gco-signature"
HMAC_ENVELOPE_HEADERS: tuple[str, ...] = (
    "x-gco-signature-version",
    "x-gco-timestamp",
    "x-gco-nonce",
    HMAC_SIGNATURE_HEADER,
)

#: Services reachable only from another Service, never from the ALB. The value
#: names the front door and the paths that reach it, and generation fails if
#: the Service turns up as an HTTPRoute backend or the front door lacks the
#: paths, so this table cannot drift from the manifests.
INTERNAL_CONSUMERS: dict[str, dict[str, str]] = {
    "cost-monitor": {
        "via": "manifest-processor",
        "pathPrefix": "/api/v1/cost",
        "description": (
            "Confined to manifest-processor traffic by a NetworkPolicy and runs no "
            "authentication middleware of its own; the manifest processor's "
            "`/api/v1/cost/*` routes are its authenticated front."
        ),
    },
}

#: Where a backend Service sends requests next, when it is itself a proxy.
#: ``hop`` is the id the API Gateway documents use for the same destination in
#: their ``x-gco-backend`` chains, so the diagram draws one box for both.
DOWNSTREAM: dict[str, dict[str, str]] = {
    "inference-proxy": {
        "hop": "inference-endpoint",
        "target": "gco-inference/<endpoint>, <endpoint>-canary or <endpoint>-proxy Services",
        "description": (
            "Resolves `/inference/<endpoint>/...` by endpoint name from the inference "
            "endpoint store, requires the endpoint to be running in this region, allows "
            "only its serving and health paths, and streams "
            "`http://<service>.gco-inference.svc.cluster.local/<path>` back."
        ),
    },
}


class ClusterGatewayDocumentError(RuntimeError):
    """The manifest or a service document cannot be composed faithfully."""


# ─── the Gateway manifest ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rule:
    prefix: str
    service: str
    port: int


@dataclass(frozen=True)
class GatewaySpec:
    name: str
    namespace: str
    gateway_class: str
    controller: str
    listener: dict[str, Any]
    load_balancer: dict[str, Any]
    target_group: dict[str, Any]
    route_name: str
    rules: tuple[Rule, ...] = field(default_factory=tuple)

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "gatewayClassName": self.gateway_class,
            "controllerName": self.controller,
            "listener": self.listener,
            "loadBalancer": self.load_balancer,
            "targetGroup": self.target_group,
            "httpRoute": self.route_name,
        }


def _one(documents: list[dict[str, Any]], kind: str, name: str | None = None) -> dict[str, Any]:
    matches = [
        document
        for document in documents
        if document.get("kind") == kind
        and (name is None or document.get("metadata", {}).get("name") == name)
    ]
    if len(matches) != 1:
        raise ClusterGatewayDocumentError(
            f"expected exactly one {kind}{f' named {name}' if name else ''} in the gateway "
            f"manifest, found {len(matches)}"
        )
    return matches[0]


def load_gateway(project_root: Path) -> GatewaySpec:
    """The Gateway, its ALB configuration and the HTTPRoute rules, as data."""
    text = (project_root / GATEWAY_MANIFEST).read_text(encoding="utf-8")
    documents = [document for document in yaml.safe_load_all(text) if isinstance(document, dict)]
    gateway = _one(documents, "Gateway")
    gateway_class = _one(documents, "GatewayClass")
    route = _one(documents, "HTTPRoute")
    listeners = gateway["spec"].get("listeners", [])
    if len(listeners) != 1:
        raise ClusterGatewayDocumentError(f"expected one Gateway listener, found {len(listeners)}")
    parameters = gateway["spec"].get("infrastructure", {}).get("parametersRef", {})
    load_balancer = _one(documents, "LoadBalancerConfiguration", parameters.get("name"))
    lb_spec = load_balancer["spec"]
    target_group = _one(
        documents,
        "TargetGroupConfiguration",
        lb_spec.get("defaultTargetGroupConfiguration", {}).get("name"),
    )["spec"]["defaultConfiguration"]

    namespace = str(route["metadata"]["namespace"])
    rules: list[Rule] = []
    for rule in route["spec"]["rules"]:
        backends = rule.get("backendRefs", [])
        if len(backends) != 1:
            raise ClusterGatewayDocumentError(
                f"expected a single backendRef per rule, got {len(backends)}"
            )
        backend = backends[0]
        if (
            backend.get("kind", "Service") != "Service"
            or backend.get("namespace", namespace) != namespace
        ):
            raise ClusterGatewayDocumentError(
                f"unsupported backendRef {backend!r}: only same-namespace Services are modelled"
            )
        for match in rule.get("matches", []):
            path = match.get("path", {})
            if path.get("type") != "PathPrefix":
                raise ClusterGatewayDocumentError(
                    f"unsupported path match type {path.get('type')!r} for "
                    f"{path.get('value')!r}: only PathPrefix precedence is modelled"
                )
            if set(match) - {"path"}:
                raise ClusterGatewayDocumentError(
                    f"rule for {path.get('value')!r} matches on more than the path"
                )
            rules.append(Rule(str(path["value"]), str(backend["name"]), int(backend["port"])))
        if rule.get("filters"):
            raise ClusterGatewayDocumentError("HTTPRoute filters are not modelled")

    listener = listeners[0]
    return GatewaySpec(
        name=str(gateway["metadata"]["name"]),
        namespace=namespace,
        gateway_class=str(gateway["spec"]["gatewayClassName"]),
        controller=str(gateway_class["spec"]["controllerName"]),
        listener={
            "name": listener.get("name"),
            "protocol": listener.get("protocol"),
            "port": listener.get("port"),
            "allowedRoutes": listener.get("allowedRoutes"),
        },
        load_balancer={
            "scheme": lb_spec.get("scheme"),
            "listenerConfigurations": lb_spec.get("listenerConfigurations"),
            "loadBalancerAttributes": lb_spec.get("loadBalancerAttributes"),
            "tags": lb_spec.get("tags"),
        },
        target_group={
            "targetType": target_group.get("targetType"),
            "protocol": target_group.get("protocol"),
            "healthCheck": target_group.get("healthCheckConfig"),
            "targetGroupAttributes": target_group.get("targetGroupAttributes"),
        },
        route_name=str(route["metadata"]["name"]),
        rules=tuple(rules),
    )


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def resolve(path: str, rules: tuple[Rule, ...]) -> Rule | None:
    """The winning rule for ``path``: the longest ``PathPrefix`` match on whole segments."""
    best: Rule | None = None
    target = _segments(path)
    for rule in rules:
        candidate = _segments(rule.prefix)
        if target[: len(candidate)] != candidate:
            continue
        if best is None or len(candidate) > len(_segments(best.prefix)):
            best = rule
    return best


# ─── composition ─────────────────────────────────────────────────────────────


def load_services(openapi_dir: Path) -> dict[str, dict[str, Any]]:
    """``{service: document}`` for every FastAPI service document."""
    services: dict[str, dict[str, Any]] = {}
    for service in SERVICE_NAMES:
        path = openapi_dir / f"{service}.json"
        if not path.is_file():
            raise ClusterGatewayDocumentError(
                f"missing {path.relative_to(openapi_dir.parent.parent)}; run "
                "python scripts/generate_openapi.py first"
            )
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
            raise ClusterGatewayDocumentError(f"{service}: document has no paths object")
        services[service] = document
    return services


def _rewrite_refs(value: Any, renames: dict[str, str]) -> Any:
    """Apply ``renames`` to every ``#/components/schemas/<name>`` reference."""
    if isinstance(value, dict):
        rewritten = {key: _rewrite_refs(item, renames) for key, item in value.items()}
        ref = rewritten.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref[len("#/components/schemas/") :]
            rewritten["$ref"] = "#/components/schemas/" + renames.get(name, name)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_refs(item, renames) for item in value]
    return value


def merge_schemas(
    services: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """One schema namespace for every service document.

    Identical definitions (FastAPI's ``HTTPValidationError`` in every app) keep
    their name; a name two services define differently is prefixed with the
    service (``manifest-processor.Status``) in that service's operations only.
    Returns the merged schemas and, per service, the renames to apply.
    """
    merged: dict[str, Any] = {}
    owners: dict[str, str] = {}
    renames: dict[str, dict[str, str]] = {}
    for service in sorted(services):
        schemas = services[service].get("components", {}).get("schemas", {})
        renames[service] = {}
        for name, schema in sorted(schemas.items()):
            if name not in merged:
                merged[name] = schema
                owners[name] = service
            elif merged[name] != schema:
                renames[service][name] = f"{service}.{name}"
    # A rename must also be visible inside the renamed schema's own references.
    for service, mapping in renames.items():
        schemas = services[service].get("components", {}).get("schemas", {})
        for old, new in mapping.items():
            merged[new] = _rewrite_refs(schemas[old], mapping)
    return dict(sorted(merged.items())), renames


def compose(project_root: Path) -> dict[str, Any]:
    """The Gateway's OpenAPI document from the manifest and the service documents."""
    gateway = load_gateway(project_root)
    services = load_services(project_root / "docs" / "openapi")
    backends = {rule.service for rule in gateway.rules}
    unknown = backends - set(services)
    if unknown:
        raise ClusterGatewayDocumentError(
            f"HTTPRoute backends without an OpenAPI document: {sorted(unknown)}"
        )
    for service, consumer in INTERNAL_CONSUMERS.items():
        if service in backends:
            raise ClusterGatewayDocumentError(
                f"{service} is listed in INTERNAL_CONSUMERS but is an HTTPRoute backend"
            )
        front = services.get(consumer["via"])
        prefix = consumer["pathPrefix"]
        if front is None or not any(path.startswith(prefix) for path in front["paths"]):
            raise ClusterGatewayDocumentError(
                f"{consumer['via']} serves no {prefix}* path to front {service}"
            )

    from gco.services.auth_middleware import UNAUTHENTICATED_PATHS

    schemas, renames = merge_schemas(services)
    paths: dict[str, dict[str, Any]] = {}
    unreachable: list[dict[str, Any]] = []
    for service in sorted(services):
        document = services[service]
        for path in sorted(document["paths"]):
            item = document["paths"][path]
            winner = resolve(path, gateway.rules)
            if service not in backends:
                continue
            if winner is None or winner.service != service:
                unreachable.append(
                    {
                        "path": path,
                        "service": service,
                        "rule": winner.prefix if winner else None,
                        "deliveredTo": winner.service if winner else None,
                    }
                )
                continue
            # Exactly one Service wins a path, so no two services can both land here.
            composed = _rewrite_refs(item, renames[service])
            route = {
                "rule": winner.prefix,
                "service": service,
                "namespace": gateway.namespace,
                "port": winner.port,
            }
            for method, operation in composed.items():
                if method == "parameters" or not isinstance(operation, dict):
                    continue
                operation["tags"] = [
                    service,
                    *[t for t in operation.get("tags", []) if t != service],
                ]
                operation["security"] = [] if path in UNAUTHENTICATED_PATHS else [{"gcoHmac": []}]
                operation["x-gco-route"] = route
            paths[path] = composed

    tags = [
        {
            "name": service,
            "description": (
                f"{services[service].get('info', {}).get('title', service)} — operations the "
                f"HTTPRoute delivers to the `{service}` Service."
            ),
        }
        for service in sorted(backends)
    ]
    versions = {str(document.get("openapi", "3.1.0")) for document in services.values()}
    description = (
        f"The HTTP surface of the internal ALB that fronts a GCO region: the Kubernetes "
        f"Gateway `{gateway.namespace}/{gateway.name}` (GatewayClass `{gateway.gateway_class}`, "
        f"implemented by `{gateway.controller}`, the AWS Load Balancer Controller) with "
        f"one {gateway.listener.get('protocol')}:{gateway.listener.get('port')} listener. "
        f"Its HTTPRoute `{gateway.route_name}` sends each request to a Service by longest "
        f"path prefix; every operation below is one a backend Service serves *and* the "
        f"route delivers to it, tagged with that Service and annotated with the winning "
        f"rule (`x-gco-route`). The ALB is `{gateway.load_balancer.get('scheme')}` and "
        f"answers only the API Gateway proxy Lambdas (over Global Accelerator or from the "
        f"regional bridge's VPC), which sign every request with the HMAC envelope "
        f"(`{HMAC_SIGNATURE_HEADER}` and companions) that the services verify on every "
        f"path except {', '.join(f'`{p}`' for p in sorted(UNAUTHENTICATED_PATHS))}. "
        f"The ALB terminates TLS with a deployment-local certificate and re-encrypts to a "
        f"TLS proxy sidecar on each pod; the service documents describe the same operations "
        f"as their applications see them."
    )
    return {
        "openapi": max(versions),
        "info": {
            "title": "GCO Cluster Gateway (internal ALB)",
            "version": API_VERSION,
            "description": description,
        },
        "servers": [
            {
                "url": "https://{alb_hostname}",
                "description": (
                    "The region's internal ALB, registered in SSM as "
                    "`/<project>/alb-hostname-<region>` by ga-registration and kept current by "
                    "the health monitor; reachable from inside the VPC and from Global "
                    "Accelerator only. Clients present SNI `backend.<project>.gco.internal`."
                ),
                "variables": {
                    "alb_hostname": {
                        "default": "<internal-alb-hostname>",
                        "description": "Assigned by the AWS Load Balancer Controller at deploy time.",
                    }
                },
            }
        ],
        "tags": tags,
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                "gcoHmac": {
                    "type": "apiKey",
                    "in": "header",
                    "name": HMAC_SIGNATURE_HEADER,
                    "description": (
                        "Request-bound HMAC envelope minted by the API Gateway proxy Lambdas "
                        f"({', '.join(f'`{h}`' for h in HMAC_ENVELOPE_HEADERS)}): the "
                        "signature covers the method, exact path and query, body digest, "
                        "timestamp and nonce under the shared signing key from Secrets Manager. "
                        "Verified by `gco/services/auth_middleware.py`; a failure is a 403. "
                        "Original caller identity is authenticated upstream by API Gateway IAM."
                    ),
                }
            },
        },
        "x-gco-source": {
            "kind": "cluster-gateway",
            "generator": GENERATOR,
            "manifest": GATEWAY_MANIFEST.as_posix(),
            "composedFrom": [f"docs/openapi/{service}.json" for service in sorted(backends)],
        },
        "x-gco-gateway": gateway.as_json(),
        "x-gco-routes": [
            {"prefix": rule.prefix, "service": rule.service, "port": rule.port}
            for rule in gateway.rules
        ],
        "x-gco-unreachable": unreachable,
        "x-gco-not-exposed": {
            service: consumer
            for service, consumer in sorted(INTERNAL_CONSUMERS.items())
            if service in services
        },
        "x-gco-downstream": {
            service: downstream
            for service, downstream in sorted(DOWNSTREAM.items())
            if service in backends
        },
        "x-gco-unauthenticated-paths": sorted(UNAUTHENTICATED_PATHS),
    }


def render(document: dict[str, Any]) -> str:
    """Serialize deterministically so regeneration produces a stable diff."""
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if the committed document is stale.",
    )
    args = parser.parse_args(argv)
    try:
        rendered = render(compose(REPO_ROOT))
    except ClusterGatewayDocumentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUTPUT_DIR / f"{DOCUMENT_NAME}.json"
    current = target.read_text(encoding="utf-8") if target.is_file() else None
    relative = target.relative_to(REPO_ROOT)
    if args.check:
        if current == rendered:
            return 0
        print(f"{relative}: {'missing' if current is None else 'stale'}")
        print(f"\nRegenerate with: python {GENERATOR}", file=sys.stderr)
        return 1
    if current == rendered:
        print(f"{relative}: unchanged")
    else:
        target.write_text(rendered, encoding="utf-8")
        print(f"{relative}: written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
