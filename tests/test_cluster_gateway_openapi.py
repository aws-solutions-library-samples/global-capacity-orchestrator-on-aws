"""``scripts/generate_cluster_gateway_openapi.py`` — the cluster gateway OpenAPI document.

The document is a composition: the ``HTTPRoute`` rules of
``post-helm-gateway.yaml`` resolved against the paths of the committed FastAPI
documents. The composition is pinned on a synthetic checkout — a manifest with
a few prefixes, three services whose paths overlap — so the reachable set, the
unreachable list, the internal-only front and the schema merge are asserted
exactly, and every shape the composer refuses (a second HTTPRoute, a non-prefix
match, a filter, an undocumented backend, a stale INTERNAL_CONSUMERS entry) is
exercised. The committed document is then checked against a fresh composition
of the real manifest and documents, the same gate CI runs.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "generate_cluster_gateway_openapi.py"


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gco_generate_cluster_gateway_openapi", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ─── fixtures ────────────────────────────────────────────────────────────────


def _rule(prefix: str, service: str, **extra: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "matches": [{"path": {"type": "PathPrefix", "value": prefix}}],
        "backendRefs": [
            {"group": "", "kind": "Service", "name": service, "port": 443, "weight": 1}
        ],
    }
    rule.update(extra)
    return rule


def _manifest(
    rules: list[dict[str, Any]],
    *,
    listeners: int = 1,
    extra_documents: list[dict[str, Any]] | None = None,
) -> str:
    documents: list[dict[str, Any]] = [
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "GatewayClass",
            "metadata": {"name": "gco-aws-alb"},
            "spec": {"controllerName": "gateway.k8s.aws/alb"},
        },
        {
            "apiVersion": "gateway.k8s.aws/v1",
            "kind": "TargetGroupConfiguration",
            "metadata": {"name": "gco-default-target-group", "namespace": "gco-system"},
            "spec": {
                "defaultConfiguration": {
                    "targetType": "ip",
                    "protocol": "HTTPS",
                    "healthCheckConfig": {"healthCheckPath": "/healthz"},
                    "targetGroupAttributes": [
                        {"key": "deregistration_delay.timeout_seconds", "value": "900"}
                    ],
                }
            },
        },
        {
            "apiVersion": "gateway.k8s.aws/v1",
            "kind": "LoadBalancerConfiguration",
            "metadata": {"name": "gco-gateway-load-balancer", "namespace": "gco-system"},
            "spec": {
                "scheme": "internal",
                "tags": {"gco.aws/gateway": "gco-system/gco-gateway"},
                "loadBalancerAttributes": [{"key": "idle_timeout.timeout_seconds", "value": "300"}],
                "defaultTargetGroupConfiguration": {"name": "gco-default-target-group"},
                "listenerConfigurations": [{"protocolPort": "HTTPS:443", "sslPolicy": "policy"}],
            },
        },
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "Gateway",
            "metadata": {"name": "gco-gateway", "namespace": "gco-system"},
            "spec": {
                "gatewayClassName": "gco-aws-alb",
                "infrastructure": {
                    "parametersRef": {
                        "group": "gateway.k8s.aws",
                        "kind": "LoadBalancerConfiguration",
                        "name": "gco-gateway-load-balancer",
                    }
                },
                "listeners": [
                    {
                        "name": f"https{i or ''}",
                        "protocol": "HTTPS",
                        "port": 443,
                        "allowedRoutes": {"namespaces": {"from": "Same"}},
                    }
                    for i in range(listeners)
                ],
            },
        },
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {"name": "gco-routes", "namespace": "gco-system"},
            "spec": {
                "parentRefs": [
                    {
                        "group": "gateway.networking.k8s.io",
                        "kind": "Gateway",
                        "name": "gco-gateway",
                        "sectionName": "https",
                    }
                ],
                "rules": rules,
            },
        },
    ]
    documents.extend(extra_documents or [])
    return "# comment\n" + yaml.safe_dump_all(documents, sort_keys=False)


DEFAULT_RULES = [
    _rule("/api/v1/health", "svc-b"),
    _rule("/inference", "svc-inf"),
    _rule("/healthz", "svc-b"),
    _rule("/", "svc-a"),
]


def _service(
    title: str, paths: dict[str, Any], schemas: dict[str, Any] | None = None
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": "1"},
        "paths": paths,
    }
    if schemas is not None:
        document["components"] = {"schemas": schemas}
    return document


def _operation(**extra: Any) -> dict[str, Any]:
    operation: dict[str, Any] = {"summary": "op", "responses": {"200": {"description": "ok"}}}
    operation.update(extra)
    return operation


SERVICES: dict[str, dict[str, Any]] = {
    "svc-a": _service(
        "Service A",
        {
            "/": {"get": _operation()},
            "/api/v1/jobs": {
                "get": _operation(
                    tags=["Jobs"],
                    responses={
                        "200": {
                            "content": {
                                "application/json": {"schema": {"$ref": "#/components/schemas/Job"}}
                            }
                        }
                    },
                ),
                "parameters": [{"name": "x", "in": "query"}],
            },
            "/api/v1/health": {"get": _operation()},
            "/api/v1/cost/status": {"get": _operation()},
        },
        {
            "Job": {
                "type": "object",
                "properties": {"error": {"$ref": "#/components/schemas/Error"}},
            },
            "Error": {"type": "object", "properties": {"detail": {"type": "string"}}},
            "Common": {"type": "string"},
        },
    ),
    "svc-b": _service(
        "Service B",
        {
            "/api/v1/health": {"get": _operation()},
            "/healthz": {"get": _operation()},
            "/": {"get": _operation()},
        },
        {
            "Error": {"type": "object", "properties": {"code": {"type": "integer"}}},
            "Common": {"type": "string"},
        },
    ),
    "svc-inf": _service(
        "Inference",
        {"/inference/{name}": {"post": _operation(tags=["svc-inf", "Proxy"])}},
    ),
    "cost": _service("Cost", {"/internal/reports": {"get": _operation()}}),
}


def _checkout(
    tmp_path: Path,
    *,
    manifest: str | None = None,
    services: dict[str, dict[str, Any]] | None = None,
) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "openapi").mkdir(parents=True)
    target = root / "lambda" / "kubectl-applier-simple" / "manifests" / "post-helm-gateway.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        manifest if manifest is not None else _manifest(DEFAULT_RULES), encoding="utf-8"
    )
    for name, document in (services if services is not None else SERVICES).items():
        (root / "docs" / "openapi" / f"{name}.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
    return root


@pytest.fixture
def synthetic(gen: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gen, "SERVICE_NAMES", tuple(SERVICES))
    monkeypatch.setattr(
        gen,
        "INTERNAL_CONSUMERS",
        {"cost": {"via": "svc-a", "pathPrefix": "/api/v1/cost", "description": "Internal."}},
    )
    monkeypatch.setattr(
        gen,
        "DOWNSTREAM",
        {
            "svc-inf": {"hop": "inference-endpoint", "target": "pods", "description": "By name."},
            "absent": {"target": "x"},
        },
    )


# ─── the manifest ────────────────────────────────────────────────────────────


def test_script_puts_the_checkout_on_sys_path_when_run_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``python scripts/…`` only adds ``scripts/`` itself; the module adds the checkout."""
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != str(PROJECT_ROOT)])
    spec = importlib.util.spec_from_file_location(
        "gco_generate_cluster_gateway_openapi_direct", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert sys.path[0] == str(PROJECT_ROOT)
    assert module.SERVICE_NAMES


def test_load_gateway_reads_the_gateway_and_its_rules(gen: ModuleType, tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    gateway = gen.load_gateway(root)
    assert gateway.name == "gco-gateway" and gateway.namespace == "gco-system"
    assert gateway.gateway_class == "gco-aws-alb" and gateway.controller == "gateway.k8s.aws/alb"
    assert gateway.listener == {
        "name": "https",
        "protocol": "HTTPS",
        "port": 443,
        "allowedRoutes": {"namespaces": {"from": "Same"}},
    }
    assert gateway.load_balancer["scheme"] == "internal"
    assert gateway.target_group["healthCheck"] == {"healthCheckPath": "/healthz"}
    assert gateway.route_name == "gco-routes"
    assert [(r.prefix, r.service, r.port) for r in gateway.rules] == [
        ("/api/v1/health", "svc-b", 443),
        ("/inference", "svc-inf", 443),
        ("/healthz", "svc-b", 443),
        ("/", "svc-a", 443),
    ]
    assert gateway.as_json()["httpRoute"] == "gco-routes"


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        (_manifest(DEFAULT_RULES, listeners=2), "expected one Gateway listener, found 2"),
        (
            _manifest(
                DEFAULT_RULES,
                extra_documents=[
                    {
                        "kind": "HTTPRoute",
                        "metadata": {"name": "other", "namespace": "gco-system"},
                        "spec": {"rules": []},
                    }
                ],
            ),
            "expected exactly one HTTPRoute in the gateway manifest, found 2",
        ),
        (
            _manifest(
                [{"matches": [{"path": {"type": "PathPrefix", "value": "/"}}], "backendRefs": []}]
            ),
            "expected a single backendRef per rule, got 0",
        ),
        (
            _manifest(
                [
                    {
                        "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                        "backendRefs": [{"kind": "ServiceImport", "name": "x", "port": 443}],
                    }
                ]
            ),
            "only same-namespace Services are modelled",
        ),
        (
            _manifest(
                [
                    {
                        "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                        "backendRefs": [
                            {"kind": "Service", "name": "x", "port": 443, "namespace": "other"}
                        ],
                    }
                ]
            ),
            "only same-namespace Services are modelled",
        ),
        (
            _manifest(
                [
                    {
                        "matches": [{"path": {"type": "Exact", "value": "/x"}}],
                        "backendRefs": [{"name": "x", "port": 443}],
                    }
                ]
            ),
            "unsupported path match type 'Exact'",
        ),
        (
            _manifest(
                [
                    {
                        "matches": [{"path": {"type": "PathPrefix", "value": "/x"}, "headers": []}],
                        "backendRefs": [{"name": "x", "port": 443}],
                    }
                ]
            ),
            "matches on more than the path",
        ),
        (
            _manifest([_rule("/", "svc-a", filters=[{"type": "URLRewrite"}])]),
            "HTTPRoute filters are not modelled",
        ),
    ],
)
def test_load_gateway_refuses_shapes_it_does_not_model(
    gen: ModuleType, tmp_path: Path, manifest: str, message: str
) -> None:
    root = _checkout(tmp_path, manifest=manifest)
    with pytest.raises(gen.ClusterGatewayDocumentError, match=message):
        gen.load_gateway(root)


def test_load_gateway_requires_the_referenced_configurations(
    gen: ModuleType, tmp_path: Path
) -> None:
    broken = _manifest(DEFAULT_RULES).replace("name: gco-gateway-load-balancer", "name: renamed", 1)
    root = _checkout(tmp_path, manifest=broken)
    with pytest.raises(
        gen.ClusterGatewayDocumentError,
        match="LoadBalancerConfiguration named gco-gateway-load-balancer",
    ):
        gen.load_gateway(root)


def test_resolve_prefers_the_longest_whole_segment_prefix(gen: ModuleType) -> None:
    rules = (
        gen.Rule("/api/v1/health", "b", 443),
        gen.Rule("/", "a", 443),
        gen.Rule("/api", "c", 443),
    )
    assert gen.resolve("/api/v1/health/deep", rules).service == "b"
    assert gen.resolve("/api/v1/healthz", rules).service == "c", "prefixes match whole segments"
    assert gen.resolve("/other", rules).service == "a"
    assert gen.resolve("/x", (gen.Rule("/y", "y", 443),)) is None


# ─── the services and their schemas ──────────────────────────────────────────


def test_load_services_requires_every_generated_document(
    gen: ModuleType, tmp_path: Path, synthetic: None
) -> None:
    root = _checkout(tmp_path)
    assert set(gen.load_services(root / "docs" / "openapi")) == set(SERVICES)
    (root / "docs" / "openapi" / "cost.json").unlink()
    with pytest.raises(gen.ClusterGatewayDocumentError, match=r"missing docs/openapi/cost\.json"):
        gen.load_services(root / "docs" / "openapi")
    (root / "docs" / "openapi" / "cost.json").write_text('{"info": {}}', encoding="utf-8")
    with pytest.raises(gen.ClusterGatewayDocumentError, match="cost: document has no paths object"):
        gen.load_services(root / "docs" / "openapi")


def test_merge_schemas_keeps_identical_definitions_and_prefixes_conflicts(gen: ModuleType) -> None:
    merged, renames = gen.merge_schemas(SERVICES)
    assert set(merged) == {"Job", "Error", "Common", "svc-b.Error"}
    assert renames == {"cost": {}, "svc-a": {}, "svc-b": {"Error": "svc-b.Error"}, "svc-inf": {}}
    assert merged["Common"] == {"type": "string"}, "identical schemas keep one definition"
    assert merged["svc-b.Error"]["properties"]["code"] == {"type": "integer"}
    assert merged["Job"]["properties"]["error"] == {"$ref": "#/components/schemas/Error"}


def test_rewrite_refs_follows_renames_through_nested_structures(gen: ModuleType) -> None:
    value = {
        "a": [{"$ref": "#/components/schemas/Error"}, {"$ref": "#/components/schemas/Keep"}],
        "b": {"$ref": "#/other/Error"},
    }
    assert gen._rewrite_refs(value, {"Error": "svc.Error"}) == {
        "a": [{"$ref": "#/components/schemas/svc.Error"}, {"$ref": "#/components/schemas/Keep"}],
        "b": {"$ref": "#/other/Error"},
    }
    assert gen._rewrite_refs("plain", {}) == "plain"


# ─── composition ─────────────────────────────────────────────────────────────


def test_compose_keeps_only_operations_the_route_delivers_to_their_service(
    gen: ModuleType, tmp_path: Path, synthetic: None
) -> None:
    document = gen.compose(_checkout(tmp_path))
    assert document["openapi"] == "3.1.0"
    assert document["info"]["title"] == "GCO Cluster Gateway (internal ALB)"
    assert set(document["paths"]) == {
        "/",
        "/api/v1/jobs",
        "/api/v1/cost/status",
        "/api/v1/health",
        "/healthz",
        "/inference/{name}",
    }

    jobs = document["paths"]["/api/v1/jobs"]
    assert jobs["parameters"] == [{"name": "x", "in": "query"}], (
        "path-level parameters travel with the item"
    )
    assert jobs["get"]["tags"] == ["svc-a", "Jobs"]
    assert jobs["get"]["security"] == [{"gcoHmac": []}]
    assert jobs["get"]["x-gco-route"] == {
        "rule": "/",
        "service": "svc-a",
        "namespace": "gco-system",
        "port": 443,
    }
    assert document["paths"]["/api/v1/health"]["get"]["security"] == [], (
        "the auth middleware exempts it"
    )
    assert document["paths"]["/api/v1/health"]["get"]["x-gco-route"]["service"] == "svc-b"
    assert document["paths"]["/inference/{name}"]["post"]["tags"] == ["svc-inf", "Proxy"], (
        "the service tag is not duplicated"
    )

    assert document["x-gco-unreachable"] == [
        {
            "path": "/api/v1/health",
            "service": "svc-a",
            "rule": "/api/v1/health",
            "deliveredTo": "svc-b",
        },
        {"path": "/", "service": "svc-b", "rule": "/", "deliveredTo": "svc-a"},
    ]
    assert document["x-gco-not-exposed"] == {
        "cost": {"via": "svc-a", "pathPrefix": "/api/v1/cost", "description": "Internal."}
    }
    assert document["x-gco-downstream"] == {
        "svc-inf": {"hop": "inference-endpoint", "target": "pods", "description": "By name."}
    }
    assert [tag["name"] for tag in document["tags"]] == ["svc-a", "svc-b", "svc-inf"]
    assert document["x-gco-routes"] == [
        {"prefix": "/api/v1/health", "service": "svc-b", "port": 443},
        {"prefix": "/inference", "service": "svc-inf", "port": 443},
        {"prefix": "/healthz", "service": "svc-b", "port": 443},
        {"prefix": "/", "service": "svc-a", "port": 443},
    ]
    assert document["x-gco-gateway"]["name"] == "gco-gateway"
    assert document["x-gco-source"] == {
        "kind": "cluster-gateway",
        "generator": gen.GENERATOR,
        "manifest": "lambda/kubectl-applier-simple/manifests/post-helm-gateway.yaml",
        "composedFrom": [
            "docs/openapi/svc-a.json",
            "docs/openapi/svc-b.json",
            "docs/openapi/svc-inf.json",
        ],
    }
    assert set(document["components"]["schemas"]) == {"Job", "Error", "Common", "svc-b.Error"}
    assert document["components"]["securitySchemes"]["gcoHmac"]["name"] == gen.HMAC_SIGNATURE_HEADER
    assert document["x-gco-unauthenticated-paths"] == [
        "/api/v1/health",
        "/healthz",
        "/metrics",
        "/readyz",
    ]
    assert "`/healthz`" in document["info"]["description"]
    assert json.loads(gen.render(document)) == document


def test_compose_refuses_backends_without_documents(
    gen: ModuleType, tmp_path: Path, synthetic: None
) -> None:
    root = _checkout(tmp_path, manifest=_manifest([_rule("/", "ghost")]))
    with pytest.raises(
        gen.ClusterGatewayDocumentError, match=r"backends without an OpenAPI document: \['ghost'\]"
    ):
        gen.compose(root)


def test_compose_refuses_a_stale_internal_consumer_table(
    gen: ModuleType, tmp_path: Path, synthetic: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    exposed = _checkout(
        tmp_path,
        manifest=_manifest([*DEFAULT_RULES[:-1], _rule("/internal", "cost"), _rule("/", "svc-a")]),
    )
    with pytest.raises(
        gen.ClusterGatewayDocumentError,
        match="cost is listed in INTERNAL_CONSUMERS but is an HTTPRoute backend",
    ):
        gen.compose(exposed)

    monkeypatch.setattr(
        gen,
        "INTERNAL_CONSUMERS",
        {"cost": {"via": "svc-b", "pathPrefix": "/api/v1/cost", "description": ""}},
    )
    with pytest.raises(
        gen.ClusterGatewayDocumentError, match=r"svc-b serves no /api/v1/cost\* path to front cost"
    ):
        gen.compose(_checkout(tmp_path / "second"))

    monkeypatch.setattr(
        gen,
        "INTERNAL_CONSUMERS",
        {"cost": {"via": "nobody", "pathPrefix": "/x", "description": ""}},
    )
    with pytest.raises(
        gen.ClusterGatewayDocumentError, match=r"nobody serves no /x\* path to front cost"
    ):
        gen.compose(_checkout(tmp_path / "third"))


# ─── the CLI ─────────────────────────────────────────────────────────────────


def test_main_writes_checks_and_reports(
    gen: ModuleType,
    tmp_path: Path,
    synthetic: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _checkout(tmp_path)
    monkeypatch.setattr(gen, "REPO_ROOT", root)
    monkeypatch.setattr(gen, "OUTPUT_DIR", root / "docs" / "openapi")
    target = root / "docs" / "openapi" / "cluster-gateway.json"

    assert gen.main(["--check"]) == 1
    captured = capsys.readouterr()
    assert "docs/openapi/cluster-gateway.json: missing" in captured.out
    assert f"Regenerate with: python {gen.GENERATOR}" in captured.err
    assert not target.exists(), "--check never writes"

    assert gen.main([]) == 0
    assert "docs/openapi/cluster-gateway.json: written" in capsys.readouterr().out
    assert json.loads(target.read_text(encoding="utf-8")) == gen.compose(root)
    assert gen.main([]) == 0
    assert "docs/openapi/cluster-gateway.json: unchanged" in capsys.readouterr().out
    assert gen.main(["--check"]) == 0

    target.write_text("{}\n", encoding="utf-8")
    assert gen.main(["--check"]) == 1
    assert "docs/openapi/cluster-gateway.json: stale" in capsys.readouterr().out

    monkeypatch.setattr(gen, "SERVICE_NAMES", ("svc-a",))
    assert gen.main([]) == 2
    assert "ERROR: HTTPRoute backends without an OpenAPI document" in capsys.readouterr().err


# ─── the committed document ──────────────────────────────────────────────────


def test_committed_document_matches_a_fresh_composition(gen: ModuleType) -> None:
    """The gate: a route rule or a service path change must regenerate the document."""
    assert gen.main(["--check"]) == 0, (
        f"docs/openapi/cluster-gateway.json is stale — regenerate with `python {gen.GENERATOR}`"
    )
    document = json.loads(
        (PROJECT_ROOT / "docs" / "openapi" / "cluster-gateway.json").read_text(encoding="utf-8")
    )
    gateway = gen.load_gateway(PROJECT_ROOT)
    for path, item in document["paths"].items():
        winner = gen.resolve(path, gateway.rules)
        assert winner is not None
        for method, operation in item.items():
            if method == "parameters":
                continue
            assert operation["x-gco-route"]["service"] == winner.service == operation["tags"][0]
            assert (operation["security"] == []) == (
                path in document["x-gco-unauthenticated-paths"]
            )
    assert "cost-monitor" in document["x-gco-not-exposed"]
    assert "cost-monitor" not in {rule["service"] for rule in document["x-gco-routes"]}
    assert document["x-gco-downstream"]["inference-proxy"]["hop"] == "inference-endpoint"
