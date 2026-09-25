"""Offline validation of every example against its documented contract.

Runs with no AWS access and no cluster: parses each example, checks the
spec registry's symmetry with the ``examples/`` directory and the
``gco_mcp`` catalog, and — for examples documented to travel the API/SQS
submission paths — proves every document clears the exact transport gates
(kind/GVK allowlist, image-source trust, target namespace) that the
deployed services enforce. This is the half that runs in CI on every PR
(``tests/test_example_job_validation.py``); the live half in ``drivers.py``
builds on the same parse.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .specs import (
    CAPABILITY_TYPES,
    COMPANION,
    DAG_RUN,
    EXAMPLE_SPECS,
    SUBMISSION_PATHS,
    SUBMIT_API,
    SUBMIT_DIRECT,
    SUBMIT_SQS,
    ExampleSpec,
)

#: Namespaces the platform provisions for user workloads.
_WORKLOAD_NAMESPACES = frozenset({"gco-jobs", "gco-inference"})

#: This repository's clone URLs. An Argo CD example must source one of them so
#: the offline checks (and the live waiter) can read exactly what it syncs.
THIS_REPOSITORY_URLS = frozenset(
    {
        "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws",
        "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws.git",
    }
)

#: The self-managed Argo CD's namespace, fenced project and in-cluster server
#: (gco/argocd_config.py; post-helm-argocd-access.yaml).
_ARGOCD_NAMESPACE = "argocd"
_ARGOCD_PROJECT = "gco-tenants"
_IN_CLUSTER_SERVER = "https://kubernetes.default.svc"

#: A whole-line Go template action (``{{ if ... }}`` / ``{{ end }}``). The
#: static read drops these lines; inline actions sit inside quoted YAML
#: strings, so what is left parses as the YAML the function renders.
_TEMPLATE_ACTION_LINE = re.compile(r"^[ \t]*\{\{.*\}\}[ \t]*$", re.MULTILINE)


@dataclass
class StaticFinding:
    """One offline check outcome for one example."""

    example: str
    check: str
    passed: bool
    detail: str = ""


@dataclass
class ParsedExample:
    """An example file parsed into documents plus its spec."""

    name: str
    path: Path
    spec: ExampleSpec
    documents: list[dict[str, Any]] = field(default_factory=list)


def examples_dir(repo_root: Path) -> Path:
    return repo_root / "examples"


def example_names(repo_root: Path) -> list[str]:
    return sorted(path.stem for path in examples_dir(repo_root).glob("*.yaml"))


def parse_example(repo_root: Path, name: str) -> ParsedExample:
    """Parse one example's YAML documents (raises on unknown name or bad YAML)."""
    spec = EXAMPLE_SPECS.get(name)
    if spec is None:
        raise KeyError(f"No validation spec for example {name!r} (add one in specs.py)")
    path = examples_dir(repo_root) / f"{name}.yaml"
    documents = [
        doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc is not None
    ]
    return ParsedExample(name=name, path=path, spec=spec, documents=documents)


def _catalog_metadata(repo_root: Path) -> dict[str, dict[str, Any]]:
    """Read ``gco_mcp``'s EXAMPLE_METADATA literal without executing the module.

    ``gco_mcp/resources/docs.py`` imports flat sibling modules (the MCP server
    puts ``gco_mcp/`` itself on ``sys.path``), so importing it from here would
    require path surgery. The catalog is a pure literal dict, so an AST read
    is sufficient — and side-effect free.
    """
    import ast

    docs_path = repo_root / "gco_mcp" / "resources" / "docs.py"
    tree = ast.parse(docs_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        value: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "EXAMPLE_METADATA":
                assert value is not None
                catalog = ast.literal_eval(value)
                if not isinstance(catalog, dict):
                    raise RuntimeError("EXAMPLE_METADATA is not a dict literal")
                return catalog
    raise RuntimeError(f"EXAMPLE_METADATA literal not found in {docs_path}")


def check_registry_symmetry(repo_root: Path) -> list[StaticFinding]:
    """Specs, files, and the MCP catalog must describe the same example set."""
    findings: list[StaticFinding] = []
    files = set(example_names(repo_root))
    specs = set(EXAMPLE_SPECS)
    catalog = set(_catalog_metadata(repo_root))
    findings.append(
        StaticFinding(
            example="*",
            check="spec/file symmetry",
            passed=files == specs,
            detail=(
                f"only in examples/: {sorted(files - specs)}; only in specs: {sorted(specs - files)}"
                if files != specs
                else ""
            ),
        )
    )
    findings.append(
        StaticFinding(
            example="*",
            check="spec/catalog symmetry",
            passed=catalog == specs,
            detail=(
                f"only in catalog: {sorted(catalog - specs)}; only in specs: {sorted(specs - catalog)}"
                if catalog != specs
                else ""
            ),
        )
    )
    return findings


def check_submission_matches_catalog(repo_root: Path, name: str) -> StaticFinding:
    """The spec's submission path must agree with the catalog's documented command."""
    meta = _catalog_metadata(repo_root).get(name, {})
    documented = str(meta.get("submission", ""))
    spec = EXAMPLE_SPECS[name]
    expectations = {
        SUBMIT_DIRECT: "gco jobs submit-direct",
        SUBMIT_SQS: "gco jobs submit-sqs",
        SUBMIT_API: "gco jobs submit ",
        DAG_RUN: "gco dag run",
    }
    if spec.submission == SUBMIT_DIRECT:
        # Inference examples document `gco inference deploy` as the
        # recommended path and manifest-direct submission as the alternative;
        # both are valid documented shapes for a submit-direct spec.
        ok = "gco jobs submit-direct" in documented or "gco inference deploy" in documented
        detail = "" if ok else f"catalog documents {documented!r}, spec says {spec.submission}"
    elif spec.submission in expectations:
        ok = expectations[spec.submission] in documented
        detail = "" if ok else f"catalog documents {documented!r}, spec says {spec.submission}"
    elif spec.submission == COMPANION:
        ok = True
        detail = ""
    else:  # kubectl-apply
        ok = "kubectl apply" in documented or documented == ""
        detail = "" if ok else f"catalog documents {documented!r}, spec says kubectl-apply"
    return StaticFinding(example=name, check="documented submission path", passed=ok, detail=detail)


def check_transport_acceptance(parsed: ParsedExample) -> list[StaticFinding]:
    """API/SQS-documented examples must clear the deployed validation gates."""
    if parsed.spec.submission not in {SUBMIT_DIRECT, SUBMIT_SQS, SUBMIT_API}:
        return []
    # Module-level pure functions: no Kubernetes client construction, so the
    # checks run on machines with no kubeconfig (CI runners, fresh laptops).
    from gco.services.manifest_processor import validate_image_sources, validate_resource_kind

    findings: list[StaticFinding] = []
    for doc in parsed.documents:
        label = f"{doc.get('kind')}/{(doc.get('metadata') or {}).get('name')}"
        if parsed.spec.submission in {SUBMIT_SQS, SUBMIT_API}:
            # Only the SQS/API services enforce the kind allowlist;
            # submit-direct is client-side kubectl and takes any kind.
            kind_ok, kind_reason = validate_resource_kind(doc)
            findings.append(
                StaticFinding(
                    example=parsed.name,
                    check=f"transport kind allowlist ({label})",
                    passed=kind_ok,
                    detail=kind_reason or "",
                )
            )
        image_ok, image_reason = validate_image_sources(doc)
        findings.append(
            StaticFinding(
                example=parsed.name,
                check=f"trusted image sources ({label})",
                passed=image_ok,
                detail=image_reason or "",
            )
        )
    return findings


def _is_argocd_application(doc: dict[str, Any]) -> bool:
    return doc.get("kind") == "Application" and str(doc.get("apiVersion", "")).startswith(
        "argoproj.io/"
    )


def application_source_documents(repo_root: Path, doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The manifests an Argo CD Application syncs from this repository, as Argo CD applies them.

    Documents without a namespace get the Application's destination namespace
    (what Argo CD does). Returns ``[]`` when the Application sources another
    repository or a path outside this checkout: there is nothing to read.
    """
    spec = doc.get("spec") or {}
    source = spec.get("source") or {}
    if str(source.get("repoURL", "")) not in THIS_REPOSITORY_URLS:
        return []
    root = repo_root.resolve()
    directory = (root / str(source.get("path", ""))).resolve()
    if not directory.is_relative_to(root) or not directory.is_dir():
        return []
    destination = str((spec.get("destination") or {}).get("namespace", ""))
    documents: list[dict[str, Any]] = []
    for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
        for loaded in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(loaded, dict):
                continue
            item = copy.deepcopy(loaded)
            metadata = item.setdefault("metadata", {})
            if not metadata.get("namespace"):
                metadata["namespace"] = destination
            documents.append(item)
    return documents


def _tenant_namespace(namespace: object) -> object:
    """A CEL / Go-template namespace expression resolves to the instance's tenant namespace.

    Instances live in gco-jobs or gco-inference; gco-jobs carries the
    governance, so the offline checks evaluate templated namespaces there.
    """
    if namespace is None or (
        isinstance(namespace, str) and ("${" in namespace or "{{" in namespace)
    ):
        return "gco-jobs"
    return namespace


def embedded_workload_documents(repo_root: Path, doc: dict[str, Any]) -> list[dict[str, Any]]:
    """What an Application, a kro RGD or a go-templating Composition will create.

    * Argo CD Application: the manifests at its Git path in this checkout.
    * kro ResourceGraphDefinition: each ``resources[].template``.
    * Crossplane Composition: each document an inline ``GoTemplate`` renders
      (the composite's own status document excluded).

    Templated namespaces resolve to gco-jobs (see :func:`_tenant_namespace`).
    """
    kind = doc.get("kind")
    api_version = str(doc.get("apiVersion", ""))
    spec = doc.get("spec") or {}
    if _is_argocd_application(doc):
        return application_source_documents(repo_root, doc)
    templates: list[dict[str, Any]] = []
    if kind == "ResourceGraphDefinition" and api_version.startswith("kro.run/"):
        templates = [
            copy.deepcopy(resource["template"])
            for resource in spec.get("resources") or []
            if isinstance(resource, dict) and isinstance(resource.get("template"), dict)
        ]
    elif kind == "Composition" and api_version.startswith("apiextensions.crossplane.io/"):
        composite = spec.get("compositeTypeRef") or {}
        for step in spec.get("pipeline") or []:
            step_input = (step or {}).get("input") or {}
            if step_input.get("kind") != "GoTemplate" or step_input.get("source") != "Inline":
                continue
            text = _TEMPLATE_ACTION_LINE.sub(
                "", str((step_input.get("inline") or {}).get("template", ""))
            )
            templates.extend(
                item
                for item in yaml.safe_load_all(text)
                if isinstance(item, dict)
                and (item.get("apiVersion"), item.get("kind"))
                != (composite.get("apiVersion"), composite.get("kind"))
            )
    for template in templates:
        metadata = template.setdefault("metadata", {})
        metadata["namespace"] = _tenant_namespace(metadata.get("namespace"))
    return templates


def check_namespaces(parsed: ParsedExample) -> list[StaticFinding]:
    """Namespaced example documents must target a provisioned workload namespace.

    An Argo CD Application is the one exception: it lives in the argocd
    namespace by design, so it must instead stay inside the fence — the
    gco-tenants project, the in-cluster server and a tenant destination.
    """
    findings: list[StaticFinding] = []
    for doc in parsed.documents:
        metadata = doc.get("metadata") or {}
        namespace = metadata.get("namespace")
        kind = str(doc.get("kind", ""))
        if kind in {"ResourceFlavor", "ClusterQueue"}:  # cluster-scoped
            continue
        if _is_argocd_application(doc):
            spec = doc.get("spec") or {}
            destination = spec.get("destination") or {}
            problems = [
                problem
                for problem, failed in (
                    (
                        f"namespace {namespace!r} is not {_ARGOCD_NAMESPACE!r}",
                        namespace != _ARGOCD_NAMESPACE,
                    ),
                    (
                        f"project {spec.get('project')!r} is not {_ARGOCD_PROJECT!r}",
                        spec.get("project") != _ARGOCD_PROJECT,
                    ),
                    (
                        f"destination server {destination.get('server')!r} is not the in-cluster server",
                        destination.get("server") != _IN_CLUSTER_SERVER,
                    ),
                    (
                        f"destination namespace {destination.get('namespace')!r} is not a workload namespace",
                        destination.get("namespace") not in _WORKLOAD_NAMESPACES,
                    ),
                )
                if failed
            ]
            findings.append(
                StaticFinding(
                    example=parsed.name,
                    check=f"Argo CD fence ({kind}/{metadata.get('name')})",
                    passed=not problems,
                    detail="; ".join(problems),
                )
            )
            continue
        if namespace is None:
            continue
        findings.append(
            StaticFinding(
                example=parsed.name,
                check=f"workload namespace ({kind}/{metadata.get('name')})",
                passed=namespace in _WORKLOAD_NAMESPACES,
                detail="" if namespace in _WORKLOAD_NAMESPACES else f"namespace {namespace!r}",
            )
        )
    return findings


def check_spec_shape(name: str) -> StaticFinding:
    """Spec fields must use known enumerations and name real companions."""
    spec = EXAMPLE_SPECS[name]
    problems: list[str] = []
    if spec.submission not in SUBMISSION_PATHS:
        problems.append(f"unknown submission path {spec.submission!r}")
    unknown_capabilities = sorted(set(spec.capability_overrides) - set(CAPABILITY_TYPES))
    if unknown_capabilities:
        problems.append(f"unknown EKS capability type(s) {unknown_capabilities}")
    if spec.ack_iam_policy_arns and "ack" not in spec.capability_overrides:
        problems.append("ack_iam_policy_arns needs capability_overrides to include 'ack'")
    if spec.companion:
        companion = EXAMPLE_SPECS.get(spec.companion)
        if companion is None or companion.submission != COMPANION:
            problems.append(f"companion {spec.companion!r} is not a companion-artifact spec")
    return StaticFinding(
        example=name,
        check="spec shape",
        passed=not problems,
        detail="; ".join(problems),
    )


def check_embedded_workloads(repo_root: Path, parsed: ParsedExample) -> list[StaticFinding]:
    """What an Application syncs, or an RGD / Composition composes, meets the example rules.

    These workloads never cross the GCO API, so nothing else checks them
    before they reach a cluster: they must land in a workload namespace, pull
    from trusted image sources and fit the default gco-jobs governance. An
    Application must also source a non-empty path of this repository.
    """
    from gco.services.manifest_processor import validate_image_sources

    findings: list[StaticFinding] = []
    for doc in parsed.documents:
        embedded = embedded_workload_documents(repo_root, doc)
        label = f"{doc.get('kind')}/{(doc.get('metadata') or {}).get('name')}"
        if _is_argocd_application(doc):
            findings.append(
                StaticFinding(
                    example=parsed.name,
                    check=f"Git source resolves ({label})",
                    passed=bool(embedded),
                    detail=""
                    if embedded
                    else "the Application must source a non-empty directory of this repository",
                )
            )
        for item in embedded:
            item_label = f"{label} -> {item.get('kind')}/{(item.get('metadata') or {}).get('name')}"
            namespace = (item.get("metadata") or {}).get("namespace")
            findings.append(
                StaticFinding(
                    example=parsed.name,
                    check=f"embedded workload namespace ({item_label})",
                    passed=namespace in _WORKLOAD_NAMESPACES,
                    detail="" if namespace in _WORKLOAD_NAMESPACES else f"namespace {namespace!r}",
                )
            )
            image_ok, image_reason = validate_image_sources(item)
            findings.append(
                StaticFinding(
                    example=parsed.name,
                    check=f"trusted image sources ({item_label})",
                    passed=image_ok,
                    detail=image_reason or "",
                )
            )
        findings.extend(_governance_findings(parsed, embedded))
    return findings


#: Container resource dimensions governed by the gco-jobs LimitRange, mapped
#: to their per-container default ceiling key in DEFAULT_RESOURCE_QUOTA.
_LIMIT_RANGE_DIMENSIONS = {
    "cpu": "container_max_cpu",
    "memory": "container_max_memory",
    "nvidia.com/gpu": "container_max_gpu",
}

#: Aggregate request dimensions governed by the gco-jobs ResourceQuota.
_QUOTA_DIMENSIONS = {
    "cpu": "max_cpu",
    "memory": "max_memory",
    "nvidia.com/gpu": "max_gpu",
}


def check_resource_governance_fit(parsed: ParsedExample) -> list[StaticFinding]:
    """Every example must be admissible under the default gco-jobs governance.

    A container exceeding the LimitRange maxima is rejected at pod creation
    with only namespace events explaining why, and the Job sits podless until
    the caller gives up — the previous defaults rejected the platform's own
    EFA training example exactly that way (live run ex241-df723811). Proving
    the fit offline keeps the shipped examples and the shipped guardrails
    from contradicting each other again. Only gco-jobs-namespaced pod specs
    are checked: the LimitRange and ResourceQuota bind that namespace.
    """
    return _governance_findings(parsed, parsed.documents)


def _governance_findings(
    parsed: ParsedExample, documents: list[dict[str, Any]]
) -> list[StaticFinding]:
    """LimitRange / ResourceQuota (and front-door cap) fit for ``documents``."""
    from gco.stacks.constants import DEFAULT_RESOURCE_QUOTA, parse_k8s_quantity

    findings: list[StaticFinding] = []
    for doc in documents:
        kind = str(doc.get("kind", ""))
        metadata = doc.get("metadata") or {}
        if metadata.get("namespace", "gco-jobs") != "gco-jobs":
            continue
        pod_spec, replicas = _pod_spec_and_parallelism(doc, kind)
        if pod_spec is None:
            continue
        containers = list(pod_spec.get("containers") or []) + list(
            pod_spec.get("initContainers") or []
        )
        aggregate: dict[str, float] = dict.fromkeys(_QUOTA_DIMENSIONS, 0.0)
        for container in containers:
            resources = container.get("resources") or {}
            requests = resources.get("requests") or {}
            limits = resources.get("limits") or {}
            for dimension, ceiling_key in _LIMIT_RANGE_DIMENSIONS.items():
                ceiling = parse_k8s_quantity(DEFAULT_RESOURCE_QUOTA[ceiling_key])
                for source_name, source in (("requests", requests), ("limits", limits)):
                    if dimension not in source:
                        continue
                    value = parse_k8s_quantity(source[dimension])
                    findings.append(
                        StaticFinding(
                            example=parsed.name,
                            check=(
                                f"LimitRange fit ({kind}/{metadata.get('name')}: "
                                f"{container.get('name')} {source_name}.{dimension})"
                            ),
                            passed=value <= ceiling,
                            detail=""
                            if value <= ceiling
                            else (
                                f"{source[dimension]} exceeds the default per-container "
                                f"ceiling {DEFAULT_RESOURCE_QUOTA[ceiling_key]} "
                                f"({ceiling_key})"
                            ),
                        )
                    )
            for dimension in _QUOTA_DIMENSIONS:
                if dimension in requests:
                    aggregate[dimension] += parse_k8s_quantity(requests[dimension])
        for dimension, quota_key in _QUOTA_DIMENSIONS.items():
            total = aggregate[dimension] * replicas
            quota = parse_k8s_quantity(DEFAULT_RESOURCE_QUOTA[quota_key])
            findings.append(
                StaticFinding(
                    example=parsed.name,
                    check=(
                        f"ResourceQuota fit ({kind}/{metadata.get('name')}: "
                        f"{replicas}x pod requests.{dimension})"
                    ),
                    passed=total <= quota,
                    detail=""
                    if total <= quota
                    else (
                        f"aggregate {total:g} exceeds the default namespace quota "
                        f"{DEFAULT_RESOURCE_QUOTA[quota_key]} ({quota_key})"
                    ),
                )
            )
        if parsed.spec.submission in {SUBMIT_API, SUBMIT_SQS}:
            findings.extend(
                _manifest_cap_findings(parsed, kind, str(metadata.get("name")), aggregate, replicas)
            )
    return findings


#: Front-door budget dimensions (manifest/queue processor caps) by
#: DEFAULT_MANIFEST_RESOURCE_CAPS key.
_MANIFEST_CAP_DIMENSIONS = {
    "cpu": "max_cpu_per_manifest",
    "memory": "max_memory_per_manifest",
    "nvidia.com/gpu": "max_gpu_per_manifest",
}


def _manifest_cap_findings(
    parsed: ParsedExample,
    kind: str,
    name: str,
    aggregate: dict[str, float],
    replicas: int,
) -> list[StaticFinding]:
    """API/SQS-submitted manifests must also fit the front-door budget.

    The manifest and queue processors cap what one submitted manifest may
    total; an example the front door rejects while kubectl admits it (or
    vice versa) means the layers contradict each other.
    """
    from gco.stacks.constants import DEFAULT_MANIFEST_RESOURCE_CAPS, parse_k8s_quantity

    findings: list[StaticFinding] = []
    for dimension, cap_key in _MANIFEST_CAP_DIMENSIONS.items():
        total = aggregate[dimension] * replicas
        cap = parse_k8s_quantity(DEFAULT_MANIFEST_RESOURCE_CAPS[cap_key])
        findings.append(
            StaticFinding(
                example=parsed.name,
                check=f"manifest-cap fit ({kind}/{name}: {replicas}x pod requests.{dimension})",
                passed=total <= cap,
                detail=""
                if total <= cap
                else (
                    f"aggregate {total:g} exceeds the default per-manifest cap "
                    f"{DEFAULT_MANIFEST_RESOURCE_CAPS[cap_key]} ({cap_key})"
                ),
            )
        )
    return findings


def _pod_spec_and_parallelism(doc: dict[str, Any], kind: str) -> tuple[dict[str, Any] | None, int]:
    """Extract the pod template spec and concurrent-pod count for a workload."""
    spec = doc.get("spec") or {}
    if kind == "Job":
        template_spec = ((spec.get("template") or {}).get("spec")) or None
        return template_spec, int(spec.get("parallelism", 1) or 1)
    if kind in {"Deployment", "StatefulSet"}:
        template_spec = ((spec.get("template") or {}).get("spec")) or None
        return template_spec, int(spec.get("replicas", 1) or 1)
    if kind == "Pod":
        return spec or None, 1
    if kind == "TrainJob":
        # A TrainJob runs its spec.trainer view once per node; the shared
        # decomposition builds the same synthetic pod spec the deployed
        # validators check, so the offline governance math matches theirs.
        from gco.services.manifest_processor import extract_trainjob_pod_specs

        trainjob_specs = extract_trainjob_pod_specs(doc)
        return trainjob_specs.trainer, trainjob_specs.num_nodes
    return None, 1


def run_static_checks(repo_root: Path, names: list[str] | None = None) -> list[StaticFinding]:
    """Run every offline check; returns findings (all must pass)."""
    findings = check_registry_symmetry(repo_root)
    for name in names or example_names(repo_root):
        findings.append(check_spec_shape(name))
        if EXAMPLE_SPECS.get(name) is None:
            continue
        parsed = parse_example(repo_root, name)
        findings.append(check_submission_matches_catalog(repo_root, name))
        findings.extend(check_transport_acceptance(parsed))
        findings.extend(check_namespaces(parsed))
        findings.extend(check_resource_governance_fit(parsed))
        findings.extend(check_embedded_workloads(repo_root, parsed))
    return findings
