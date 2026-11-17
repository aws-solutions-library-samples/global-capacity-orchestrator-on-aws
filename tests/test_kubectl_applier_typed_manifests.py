"""Every platform manifest the kubectl-applier Lambda applies through a typed method
deserializes under the pinned kubernetes client's models.

Since kubernetes-client 37.0 the generated ``create_*`` / ``patch_*`` methods
validate a ``dict`` body against Pydantic models before sending it: an unknown
field, a quantity written as a number where the model wants a string, or a
string where it wants an integer is rejected client-side with
``pydantic.ValidationError``. The Lambda applies GCO's own manifests (the
``lambda/kubectl-applier-simple/manifests/*.yaml`` documents, placeholders
rendered) through exactly those methods, so a manifest edit that trips the
models would otherwise surface only at deploy time, inside the convergence
Step Function. This test renders the shipped manifests the way the planner
does and runs each typed-kind document through the model the handler's
``kind`` dispatch would send it to.

Custom resources (NodePools, Kueue, KEDA, Volcano, ... anything the handler
routes to ``CustomObjectsApi``) are untyped on the wire and are not checked.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from kubernetes import client

from tests.test_kubectl_applier import _fully_enabled_replacements

MANIFESTS_DIR = Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple" / "manifests"

# The typed model behind each `kind` branch of the handler's apply loop
# (`apply_manifests`: `if kind == "Namespace": v1.create_namespace(body=doc)` ...).
# A kind the handler sends to CustomObjectsApi has no entry here on purpose.
TYPED_KIND_MODELS: dict[str, str] = {
    "Namespace": "V1Namespace",
    "ServiceAccount": "V1ServiceAccount",
    "ClusterRole": "V1ClusterRole",
    "ClusterRoleBinding": "V1ClusterRoleBinding",
    "Role": "V1Role",
    "RoleBinding": "V1RoleBinding",
    "Lease": "V1Lease",
    "Deployment": "V1Deployment",
    "StatefulSet": "V1StatefulSet",
    "DaemonSet": "V1DaemonSet",
    "Job": "V1Job",
    "CronJob": "V1CronJob",
    "HorizontalPodAutoscaler": "V2HorizontalPodAutoscaler",
    "PodDisruptionBudget": "V1PodDisruptionBudget",
    "Service": "V1Service",
    "Pod": "V1Pod",
    "ConfigMap": "V1ConfigMap",
    "Secret": "V1Secret",
    "StorageClass": "V1StorageClass",
    "PriorityClass": "V1PriorityClass",
    "PersistentVolume": "V1PersistentVolume",
    "PersistentVolumeClaim": "V1PersistentVolumeClaim",
    "APIService": "V1APIService",
    "ValidatingAdmissionPolicy": "V1ValidatingAdmissionPolicy",
    "ValidatingAdmissionPolicyBinding": "V1ValidatingAdmissionPolicyBinding",
    "CustomResourceDefinition": "V1CustomResourceDefinition",
    "NetworkPolicy": "V1NetworkPolicy",
    "ResourceQuota": "V1ResourceQuota",
    "LimitRange": "V1LimitRange",
}


@pytest.fixture(scope="module")
def planned_documents() -> Iterator[list[dict[str, Any]]]:
    """Every document the planner would apply, both phases, every feature on."""
    handler_path = str(MANIFESTS_DIR.parent)
    sys.path.insert(0, handler_path)
    sys.modules.pop("handler", None)
    try:
        import handler

        # plan_manifests raises on a planning error; no result to inspect.
        plan = handler.plan_manifests(
            str(MANIFESTS_DIR), _fully_enabled_replacements(MANIFESTS_DIR)
        )
        yield [*plan["phases"]["base"], *plan["phases"]["post-helm"]]
    finally:
        sys.path.remove(handler_path)
        sys.modules.pop("handler", None)


def _typed_documents(planned: list[dict[str, Any]]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (item["sourceFile"], f"{item['kind']}/{item['name']}", item["document"])
        for item in planned
        if item["kind"] in TYPED_KIND_MODELS
    ]


def test_the_plan_covers_the_kinds_the_handler_types(
    planned_documents: list[dict[str, Any]],
) -> None:
    """The mapping above is only useful while it matches what the manifests ship."""
    typed = _typed_documents(planned_documents)
    assert len(typed) >= 150, len(typed)
    kinds_seen = {ident.split("/")[0] for _, ident, _ in typed}
    # The kinds the platform relies on most; a manifest sweep that dropped
    # one of them from the plan would be news.
    assert {
        "Deployment",
        "Service",
        "ConfigMap",
        "NetworkPolicy",
        "Role",
        "RoleBinding",
    } <= kinds_seen


def test_every_typed_document_validates_under_the_pinned_client(
    planned_documents: list[dict[str, Any]],
) -> None:
    failures: list[str] = []
    for source, ident, document in _typed_documents(planned_documents):
        model = getattr(client, TYPED_KIND_MODELS[document["kind"]])
        try:
            model.from_dict(document)
        except Exception as exc:  # pydantic.ValidationError, by its full text
            first_lines = " ".join(line.strip() for line in str(exc).splitlines()[:3])
            failures.append(f"{source} {ident}: {first_lines[:300]}")
    assert not failures, (
        "These platform manifests fail the pinned kubernetes client's model validation, "
        "which the kubectl-applier Lambda runs before every typed create/patch:\n  "
        + "\n  ".join(failures)
    )


def test_validated_documents_round_trip_to_the_same_wire_body(
    planned_documents: list[dict[str, Any]],
) -> None:
    """What the typed method puts on the wire is the document itself.

    The model layer must not rename, drop or coerce anything in a manifest
    that passes validation (a key the model lacks would be dropped silently
    on the way out); every shipped document survives the round trip intact.
    """
    api = client.ApiClient()
    changed: list[str] = []
    for source, ident, document in _typed_documents(planned_documents):
        model = getattr(client, TYPED_KIND_MODELS[document["kind"]]).from_dict(document)
        if api.sanitize_for_serialization(model) != document:
            changed.append(f"{source} {ident}")
    assert not changed, "Typed serialization changed these documents:\n  " + "\n  ".join(changed)
