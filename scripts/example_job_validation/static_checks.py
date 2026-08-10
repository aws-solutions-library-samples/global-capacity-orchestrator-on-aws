"""Offline validation of every example against its documented contract.

Runs with no AWS access and no cluster: parses each example, checks the
spec registry's symmetry with the ``examples/`` directory and the
``gco_mcp`` catalog, and — for examples documented to travel the API/SQS
submission paths — proves every document clears the exact transport gates
(kind/GVK allowlist, image-source trust, target namespace) that the
deployed services enforce. This is the half that runs in CI on every PR
(``tests/test_example_job_validation.py``); the live half in
``checks/examples.py`` builds on the same parse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .specs import (
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


def check_namespaces(parsed: ParsedExample) -> list[StaticFinding]:
    """Namespaced example documents must target a provisioned workload namespace."""
    findings: list[StaticFinding] = []
    for doc in parsed.documents:
        metadata = doc.get("metadata") or {}
        namespace = metadata.get("namespace")
        kind = str(doc.get("kind", ""))
        if kind in {"ResourceFlavor", "ClusterQueue"}:  # cluster-scoped
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
    """Spec fields must use known enumerations."""
    spec = EXAMPLE_SPECS[name]
    ok = spec.submission in SUBMISSION_PATHS
    return StaticFinding(
        example=name,
        check="spec shape",
        passed=ok,
        detail="" if ok else f"unknown submission path {spec.submission!r}",
    )


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
    return findings
