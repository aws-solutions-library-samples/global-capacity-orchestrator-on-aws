"""The tenant write fence on GCO's objects in gco-inference and gco-jobs.

``09-tenant-write-fence.yaml`` is a ValidatingAdmissionPolicy for the gap the
optional kro, Argo CD and Crossplane tenant roles leave: they may write
ConfigMaps, Secrets, Services, Deployments and StatefulSets in the two tenant
namespaces, and RBAC cannot tell a tenant's object there from GCO's. The
policy keeps what the inference monitor manages its own (the objects it
labels, the shared Mooncake master, the ConfigMaps its pods mount by name, its
provenance annotations), the model endpoints' keypair cert-manager's, and the
CA bundle trust-manager publishes in gco-jobs trust-manager's.

Its CEL compiles and runs in cel-expr-python (``tests/_cel.py``). The names it keys on are pinned to
their owners here (the monitor's constants and endpoint inventory, the
monitor's Deployment, the cert-manager and trust-manager releases, the shipped
Bundle), so renaming one without the other fails. Every object GCO ships, and every example and harness manifest,
is admitted for the identity that creates it. ``integration:kind:examples-smoke``
runs the same policy in a real API server (``tests/test_supply_chain_integrity.py``
pins its probes).
"""

from __future__ import annotations

import base64
import copy
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from gco.services import inference_monitor
from gco.services.inference_monitor import InferenceMonitor, ReconcileAuthority
from tests._cel import CelError, Variables, check, evaluate, evaluate_bool

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFESTS_DIR = REPO_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
CHARTS_FILE = REPO_ROOT / "lambda" / "helm-installer" / "charts.yaml"
EXAMPLES_DIR = REPO_ROOT / "examples"
HARNESS_MANIFESTS_DIR = REPO_ROOT / "scripts" / "live_release_validation" / "manifests"
FENCE = "09-tenant-write-fence.yaml"
POLICY = "gco-tenant-write-fence"
INFERENCE = "gco-inference"
JOBS = "gco-jobs"

MONITOR = "system:serviceaccount:gco-system:gco-inference-monitor-sa"
CERT_MANAGER = "system:serviceaccount:cert-manager:cert-manager"
TRUST_MANAGER = "system:serviceaccount:trust-manager:trust-manager"
#: A kro, Argo CD or Crossplane identity bound to the tenant roles.
TENANT = "gco-tenant"
#: The kubectl applier, and any cluster administrator: not exempt either.
APPLIER = "kubernetes-admin"
GARBAGE_COLLECTOR = "system:serviceaccount:kube-system:generic-garbage-collector"

LABEL_KEY, LABEL_VALUE = next(iter(inference_monitor.INFERENCE_POD_SELECTOR.items()))
MANAGED_LABELS = {"app": "ep", "project": "gco", LABEL_KEY: LABEL_VALUE}
PROVENANCE_KEYS = tuple(
    ReconcileAuthority(
        endpoint_name="ep", lifecycle_id="l", region_generation="r", leader_epoch="e"
    ).annotations
)
PROVENANCE = {key: f"value-of-{key}" for key in PROVENANCE_KEYS}
KEYPAIR = inference_monitor.ENDPOINT_TLS_SECRET
MASTER = inference_monitor.MOONCAKE_MASTER_SERVICE
POD_PROGRAMS = InferenceMonitor._endpoint_resource_inventory("ep").config_maps

# The first message the API server returns for each rule, in policy order.
ONLY_THE_MONITOR = (
    "only the inference monitor (system:serviceaccount:gco-system:gco-inference-monitor-sa) may"
)
MANAGED_CONTENT = "is managed by the inference monitor: only"
POD_PROGRAM = "the inference monitor mounts ConfigMaps named"
PROVENANCE_OWNED = "may set, change or remove the gco.io/lifecycle-id"
KEYPAIR_OWNED = "only the cert-manager controller (system:serviceaccount:cert-manager:cert-manager)"
BUNDLE_OWNED = "only trust-manager (system:serviceaccount:trust-manager:trust-manager)"

#: group, version, plural for every kind these tests send.
_RESOURCES = {
    "ConfigMap": ("", "v1", "configmaps"),
    "Secret": ("", "v1", "secrets"),
    "Service": ("", "v1", "services"),
    "Deployment": ("apps", "v1", "deployments"),
    "StatefulSet": ("apps", "v1", "statefulsets"),
    "Pod": ("", "v1", "pods"),
    "Job": ("batch", "v1", "jobs"),
    "HorizontalPodAutoscaler": ("autoscaling", "v2", "horizontalpodautoscalers"),
    "ScaledObject": ("keda.sh", "v1alpha1", "scaledobjects"),
}
_FENCED_RESOURCES = {"configmaps", "secrets", "services", "deployments", "statefulsets"}


def _render(text: str) -> str:
    return re.sub(r"\{\{[A-Za-z0-9_]+\}\}", "placeholder", text).replace("__RUN_TOKEN__", "run")


def _documents(path: Path) -> list[dict[str, Any]]:
    loaded = yaml.safe_load_all(_render(path.read_text(encoding="utf-8")))
    return [doc for doc in loaded if isinstance(doc, dict)]


def _policy() -> dict[str, Any]:
    (policy,) = [
        doc
        for doc in _documents(MANIFESTS_DIR / FENCE)
        if doc["kind"] == "ValidatingAdmissionPolicy"
    ]
    return policy


def _selected(namespace: str, kind: str, operation: str, subresource: str) -> bool:
    """Whether the policy's matchConstraints select one request."""
    constraints = _policy()["spec"]["matchConstraints"]
    (expression,) = constraints["namespaceSelector"]["matchExpressions"]
    assert (expression["key"], expression["operator"]) == ("kubernetes.io/metadata.name", "In")
    if namespace not in expression["values"]:
        return False
    group, version, plural = _RESOURCES[kind]
    requested = f"{plural}/{subresource}" if subresource else plural
    return any(
        group in rule["apiGroups"]
        and version in rule["apiVersions"]
        and operation in rule["operations"]
        and requested in rule["resources"]
        for rule in constraints["resourceRules"]
    )


def _activation(
    obj: dict[str, Any],
    *,
    user: str,
    old: dict[str, Any] | None,
    operation: str,
    subresource: str = "",
) -> dict[str, Any]:
    """The variables the API server binds for one admission request."""
    group, version, plural = _RESOURCES[obj["kind"]]
    metadata = obj["metadata"]
    activation: dict[str, Any] = {
        "object": obj,
        "oldObject": old,
        "request": {
            "operation": operation,
            "kind": {"group": group, "version": version, "kind": obj["kind"]},
            "resource": {"group": group, "version": version, "resource": plural},
            "subResource": subresource,
            "namespace": metadata["namespace"],
            "name": metadata.get("name", ""),
            "userInfo": {"username": user, "groups": ["system:authenticated"]},
        },
    }
    activation["variables"] = Variables(_policy()["spec"]["variables"], activation)
    return activation


def _review(
    obj: dict[str, Any],
    *,
    user: str,
    old: dict[str, Any] | None = None,
    operation: str | None = None,
    subresource: str = "",
) -> list[str] | None:
    """Run the policy as the API server does for one request.

    None when its matchConstraints do not select the request; otherwise the
    messages of the validations that failed, in order ([] admits it; the API
    server returns the first). An UPDATE is a request with ``old``.
    """
    operation = operation or ("UPDATE" if old is not None else "CREATE")
    if not _selected(obj["metadata"]["namespace"], obj["kind"], operation, subresource):
        return None
    activation = _activation(obj, user=user, old=old, operation=operation, subresource=subresource)
    failures = []
    for validation in _policy()["spec"]["validations"]:
        try:
            if evaluate_bool(validation["expression"], activation):
                continue
            message = evaluate(validation["messageExpression"], activation)
        except CelError as error:
            pytest.fail(f"CEL error in {validation['expression']!r}: {error}")
        assert isinstance(message, str) and message.strip() and "\n" not in message
        failures.append(message)
    return failures


def _variable(obj: dict[str, Any], name: str, *, user: str) -> Any:
    return _activation(obj, user=user, old=None, operation="CREATE")["variables"].get(name)


def _object(
    kind: str,
    name: str,
    namespace: str = INFERENCE,
    *,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    **body: Any,
) -> dict[str, Any]:
    group, version, _plural = _RESOURCES[kind]
    metadata: dict[str, Any] = {"name": name, "namespace": namespace}
    if labels is not None:
        metadata["labels"] = dict(labels)
    if annotations is not None:
        metadata["annotations"] = dict(annotations)
    return {
        "apiVersion": f"{group}/{version}" if group else version,
        "kind": kind,
        "metadata": metadata,
        **body,
    }


def _config_map(name: str, namespace: str = INFERENCE, **fields: Any) -> dict[str, Any]:
    fields.setdefault("data", {"program.py": "print('served')"})
    return _object("ConfigMap", name, namespace, **fields)


def _secret(name: str, namespace: str = INFERENCE, **fields: Any) -> dict[str, Any]:
    fields.setdefault("type", "Opaque")
    fields.setdefault("data", {"key": base64.b64encode(b"generated").decode()})
    return _object("Secret", name, namespace, **fields)


def _pod_template(labels: dict[str, str], image: str) -> dict[str, Any]:
    return {
        "metadata": {"labels": labels},
        "spec": {"containers": [{"name": "model", "image": image}]},
    }


def _deployment(
    name: str,
    *,
    labels: dict[str, str] | None = None,
    pod_labels: dict[str, str] | None = None,
    image: str = "registry.example/model:v1",
) -> dict[str, Any]:
    selector = {"app": name}
    return _object(
        "Deployment",
        name,
        labels=labels,
        spec={
            "replicas": 1,
            "selector": {"matchLabels": selector},
            "template": _pod_template({**selector, **(pod_labels or {})}, image),
        },
    )


def _stateful_set(name: str, *, labels: dict[str, str] | None = None) -> dict[str, Any]:
    return _object(
        "StatefulSet",
        name,
        labels=labels,
        spec={
            "serviceName": name,
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": _pod_template({"app": name}, "registry.example/vllm:v1"),
        },
    )


def _service(name: str, *, labels: dict[str, str] | None = None) -> dict[str, Any]:
    return _object(
        "Service",
        name,
        labels=labels,
        spec={
            "selector": {"app": name, LABEL_KEY: LABEL_VALUE},
            "ports": [{"name": "https", "port": 8443, "targetPort": "https"}],
        },
    )


def _changed(obj: dict[str, Any], change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    updated = copy.deepcopy(obj)
    change(updated)
    return updated


def _set(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    """A change that sets one dotted path, creating the maps on the way."""

    def change(obj: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        node = obj
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value

    return change


def _drop(path: str) -> Callable[[dict[str, Any]], None]:
    def change(obj: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        node = obj
        for part in parents:
            node = node[part]
        del node[leaf]

    return change


def _set_label(key: str, value: str) -> Callable[[dict[str, Any]], None]:
    def change(obj: dict[str, Any]) -> None:
        obj["metadata"].setdefault("labels", {})[key] = value

    return change


def _set_annotation(key: str, value: str) -> Callable[[dict[str, Any]], None]:
    def change(obj: dict[str, Any]) -> None:
        obj["metadata"].setdefault("annotations", {})[key] = value

    return change


def _drop_label(key: str) -> Callable[[dict[str, Any]], None]:
    def change(obj: dict[str, Any]) -> None:
        del obj["metadata"]["labels"][key]

    return change


def _drop_annotation(key: str) -> Callable[[dict[str, Any]], None]:
    def change(obj: dict[str, Any]) -> None:
        del obj["metadata"]["annotations"][key]

    return change


def _set_image(image: str) -> Callable[[dict[str, Any]], None]:
    def change(obj: dict[str, Any]) -> None:
        obj["spec"]["template"]["spec"]["containers"][0]["image"] = image

    return change


def _no_change(obj: dict[str, Any]) -> None:
    return None


def _both(
    first: Callable[[dict[str, Any]], None], second: Callable[[dict[str, Any]], None]
) -> Callable[[dict[str, Any]], None]:
    def change(obj: dict[str, Any]) -> None:
        first(obj)
        second(obj)

    return change


def _assert_verdict(verdict: list[str] | None, expected: str | None) -> None:
    assert verdict is not None, "the policy did not evaluate the request"
    if expected is None:
        assert verdict == []
    else:
        assert verdict, "the policy admitted the request"
        assert expected in verdict[0], verdict[0]


# ─── The admission objects ─────────────────────────────────────────


class TestFencePolicy:
    def test_the_fence_is_an_ungated_base_pass_file_of_its_own(self) -> None:
        documents = _documents(MANIFESTS_DIR / FENCE)
        assert [(doc["kind"], doc["metadata"]["name"]) for doc in documents] == [
            ("ValidatingAdmissionPolicy", POLICY),
            ("ValidatingAdmissionPolicyBinding", POLICY),
        ]
        for doc in documents:
            assert doc["apiVersion"] == "admissionregistration.k8s.io/v1"
            assert "namespace" not in doc["metadata"]
        raw = (MANIFESTS_DIR / FENCE).read_text(encoding="utf-8")
        assert not re.search(r"\{\{[A-Za-z0-9_]+\}\}", raw)
        # The base pass runs before Helm installs cert-manager and before the
        # post-Helm pass creates gco-inference-tls, like the issuance fence.
        assert not FENCE.startswith("post-helm-")
        assert FENCE > "08-internal-ca-issuance.yaml"

    def test_the_policy_fails_closed_and_its_binding_denies_everywhere(self) -> None:
        spec = _policy()["spec"]
        assert spec["failurePolicy"] == "Fail"
        assert "paramKind" not in spec and "matchConditions" not in spec
        (binding,) = [
            doc
            for doc in _documents(MANIFESTS_DIR / FENCE)
            if doc["kind"] == "ValidatingAdmissionPolicyBinding"
        ]
        assert binding["spec"] == {"policyName": POLICY, "validationActions": ["Deny"]}

    def test_it_matches_creates_and_updates_of_the_fenced_kinds_in_the_tenant_namespaces(
        self,
    ) -> None:
        assert _policy()["spec"]["matchConstraints"] == {
            "namespaceSelector": {
                "matchExpressions": [
                    {
                        "key": "kubernetes.io/metadata.name",
                        "operator": "In",
                        "values": [INFERENCE, JOBS],
                    }
                ]
            },
            "resourceRules": [
                {
                    "apiGroups": [""],
                    "apiVersions": ["v1"],
                    "operations": ["CREATE", "UPDATE"],
                    "resources": ["configmaps", "secrets", "services"],
                },
                {
                    "apiGroups": ["apps"],
                    "apiVersions": ["v1"],
                    "operations": ["CREATE", "UPDATE"],
                    "resources": ["deployments", "statefulsets"],
                },
            ],
        }

    def test_every_rule_is_forbidden_with_a_one_line_message(self) -> None:
        validations = _policy()["spec"]["validations"]
        assert len(validations) == 6
        for validation in validations:
            assert validation["reason"] == "Forbidden"
            assert validation["message"].strip() and "\n" not in validation["message"]
            assert validation["messageExpression"].strip()

    def test_expressions_compile_and_use_only_variables_defined_before_them(self) -> None:
        spec = _policy()["spec"]
        defined: list[str] = []
        for variable in spec["variables"]:
            check(variable["expression"], defined)
            defined.append(variable["name"])
        assert len(defined) == len(set(defined))
        for validation in spec["validations"]:
            for field in ("expression", "messageExpression"):
                check(validation[field], defined)


# ─── The names it keys on, pinned to their owners ──────────────────


class TestNamesMatchTheirOwners:
    def test_the_monitor_is_the_inference_monitor_deployments_service_account(self) -> None:
        (deployment,) = [
            doc
            for doc in _documents(MANIFESTS_DIR / "32-inference-monitor.yaml")
            if doc["kind"] == "Deployment"
        ]
        namespace = deployment["metadata"]["namespace"]
        account = deployment["spec"]["template"]["spec"]["serviceAccountName"]
        assert f"system:serviceaccount:{namespace}:{account}" == MONITOR
        assert (namespace, account) in {
            (doc["metadata"].get("namespace"), doc["metadata"]["name"])
            for doc in _documents(MANIFESTS_DIR / "02-rbac.yaml")
            if doc["kind"] == "ServiceAccount"
        }
        probe = _config_map("probe")
        assert _variable(probe, "fromMonitor", user=MONITOR) is True
        for lookalike in (
            f"{MONITOR}-2",
            "system:serviceaccount:gco-inference:gco-inference-monitor-sa",
            "system:serviceaccount:gco-system:gco-manifest-processor-sa",
            "gco-inference-monitor-sa",
        ):
            assert _variable(probe, "fromMonitor", user=lookalike) is False

    def test_cert_manager_is_the_charts_controller(self) -> None:
        release = "cert-manager"
        chart = yaml.safe_load(CHARTS_FILE.read_text(encoding="utf-8"))["charts"][release]
        values = chart.get("values", {})
        assert chart["chart"] in release
        assert not {"fullnameOverride", "nameOverride"} & set(values)
        assert values.get("serviceAccount", {}).get("create", True) is True
        assert "name" not in values.get("serviceAccount", {})
        assert f"system:serviceaccount:{chart['namespace']}:{release}" == CERT_MANAGER
        probe = _secret(KEYPAIR)
        assert _variable(probe, "fromCertManager", user=CERT_MANAGER) is True
        assert _variable(probe, "fromCertManager", user=f"{CERT_MANAGER}-webhook") is False

    def test_trust_manager_is_the_charts_controller(self) -> None:
        """The chart runs trust-manager as a ServiceAccount named after the chart
        (no nameOverride), in the release namespace the installer passes
        (charts.yaml ``namespace``; the chart's own ``namespace`` value unset)."""
        release = "trust-manager"
        chart = yaml.safe_load(CHARTS_FILE.read_text(encoding="utf-8"))["charts"][release]
        values = chart.get("values", {})
        assert chart["chart"] == release
        assert not {"fullnameOverride", "nameOverride", "namespace"} & set(values)
        assert values.get("serviceAccount", {}).get("create", True) is True
        assert "name" not in values.get("serviceAccount", {})
        assert f"system:serviceaccount:{chart['namespace']}:{chart['chart']}" == TRUST_MANAGER
        probe = _config_map("gco-internal-ca", JOBS)
        assert _variable(probe, "fromTrustManager", user=TRUST_MANAGER) is True
        for lookalike in (
            "system:serviceaccount:cert-manager:trust-manager",
            "system:serviceaccount:gco-jobs:trust-manager",
            "trust-manager",
        ):
            assert _variable(probe, "fromTrustManager", user=lookalike) is False

    def test_the_bundle_is_the_one_trust_manager_writes(self) -> None:
        """The fenced ConfigMap is the shipped Bundle's target: same name (trust-manager
        names every target after its Bundle), and gco-jobs the only namespace the
        Bundle selects and the chart lets trust-manager write."""
        (bundle,) = [
            doc
            for doc in _documents(MANIFESTS_DIR / "post-helm-mlflow-tls.yaml")
            if doc["kind"] == "Bundle"
        ]
        target = bundle["spec"]["target"]
        assert target["namespaceSelector"] == {"matchLabels": {"kubernetes.io/metadata.name": JOBS}}
        chart = yaml.safe_load(CHARTS_FILE.read_text(encoding="utf-8"))["charts"]["trust-manager"]
        assert chart["values"]["app"]["targetNamespaces"] == [JOBS]
        assert (
            _variable(_config_map(bundle["metadata"]["name"], JOBS), "trustBundle", user=TENANT)
            is True
        )

    def test_the_label_is_the_monitors_provenance_label(self) -> None:
        assert InferenceMonitor._has_monitor_provenance({"metadata": {"labels": MANAGED_LABELS}})
        for kind, obj in (
            ("ConfigMap", _config_map("ep-config", labels=MANAGED_LABELS)),
            ("Secret", _secret("ep-admin", labels=MANAGED_LABELS)),
            ("Service", _service("ep", labels=MANAGED_LABELS)),
            ("Deployment", _deployment("ep", labels=MANAGED_LABELS)),
            ("StatefulSet", _stateful_set("ep-store", labels=MANAGED_LABELS)),
        ):
            _assert_verdict(_review(obj, user=TENANT), ONLY_THE_MONITOR)
            assert _review(obj, user=MONITOR) == [], kind

    @pytest.mark.parametrize("key", PROVENANCE_KEYS)
    def test_the_annotations_are_the_monitors_authority_claim(self, key: str) -> None:
        assert set(PROVENANCE_KEYS) == {
            inference_monitor._LIFECYCLE_ANNOTATION,
            inference_monitor._REGION_GENERATION_ANNOTATION,
            inference_monitor._LEADER_EPOCH_ANNOTATION,
        }
        forged = _config_map("tenant-config", annotations={key: "forged"})
        _assert_verdict(_review(forged, user=TENANT), PROVENANCE_OWNED)
        assert _review(forged, user=MONITOR) == []

    @pytest.mark.parametrize("name", POD_PROGRAMS)
    def test_every_config_map_an_endpoint_mounts_is_reserved(self, name: str) -> None:
        """Deleting one and recreating it unlabelled would change what the pods run."""
        _assert_verdict(_review(_config_map(name), user=TENANT), POD_PROGRAM)
        _assert_verdict(_review(_config_map(name), user=APPLIER), POD_PROGRAM)
        assert _review(_config_map(name, labels=MANAGED_LABELS), user=MONITOR) == []
        # Only in gco-inference, where the monitor runs endpoints.
        assert _review(_config_map(name, JOBS), user=TENANT) == []

    def test_the_reserved_names_are_exactly_the_endpoint_inventorys_config_maps(self) -> None:
        assert len(POD_PROGRAMS) == 3
        for endpoint in ("ep", "a", "model-with-dashes"):
            inventory = InferenceMonitor._endpoint_resource_inventory(endpoint)
            for name in inventory.config_maps:
                assert _variable(_config_map(name), "podProgram", user=TENANT) is True
        # The suffix alone, or in the middle, is an ordinary name.
        for name in ("tls-proxy-notes", "ep-tls-proxy-v2", "mooncake", "pd-proxy"):
            assert _variable(_config_map(name), "podProgram", user=TENANT) is False
        # A Service or Deployment with such a name is not a ConfigMap its pods mount.
        assert _variable(_deployment("ep-pd-proxy"), "podProgram", user=TENANT) is False

    def test_the_keypair_is_the_model_endpoint_leafs_secret(self) -> None:
        (certificate,) = [
            doc
            for doc in _documents(MANIFESTS_DIR / "post-helm-api-workload-certificates.yaml")
            if doc["kind"] == "Certificate" and doc["metadata"]["namespace"] == INFERENCE
        ]
        assert certificate["spec"]["secretName"] == KEYPAIR

    @pytest.mark.parametrize("builder", [_stateful_set, _service])
    def test_the_shared_master_is_the_monitors(self, builder: Callable[..., Any]) -> None:
        master = builder(MASTER, labels={"app": MASTER, "project": "gco"})
        _assert_verdict(_review(master, user=TENANT), ONLY_THE_MONITOR)
        assert _review(master, user=MONITOR) == []
        # Only that one StatefulSet and Service.
        assert _review(_config_map(MASTER), user=TENANT) == []
        assert _review(builder(f"{MASTER}-mine"), user=TENANT) == []


# ─── gco-inference, evaluated ──────────────────────────────────────

_MANAGED_CONFIG_MAP = _config_map(
    "ep-tls-proxy", labels=MANAGED_LABELS, annotations={**PROVENANCE, "note": "x"}
)
_MANAGED_SECRET = _secret("ep-admin", labels=MANAGED_LABELS, annotations=PROVENANCE)
_MANAGED_DEPLOYMENT = _deployment("ep", labels=MANAGED_LABELS, pod_labels=MANAGED_LABELS)
_MANAGED_SERVICE = _service("ep", labels=MANAGED_LABELS)
_MASTER = _stateful_set(MASTER, labels={"app": MASTER, "project": "gco"})
_TENANT_CONFIG_MAP = _config_map("tenant-config", labels={"app": "mine"})
_STAMPED_CONFIG_MAP = _config_map("tenant-config", annotations=PROVENANCE)
#: A pod program a tenant created before the fence existed, without the label.
_LEGACY_POD_PROGRAM = _config_map("ep2-tls-proxy")

CREATE_CASES = [
    # Refused.
    ("a tenant labels a ConfigMap as the monitor's", TENANT, _MANAGED_CONFIG_MAP, ONLY_THE_MONITOR),
    (
        "a tenant labels a Deployment as the monitor's",
        TENANT,
        _MANAGED_DEPLOYMENT,
        ONLY_THE_MONITOR,
    ),
    ("the applier is not the monitor", APPLIER, _MANAGED_SERVICE, ONLY_THE_MONITOR),
    ("a tenant creates the shared master", TENANT, _MASTER, ONLY_THE_MONITOR),
    (
        "a tenant recreates a pod program unlabelled",
        TENANT,
        _config_map("ep-tls-proxy"),
        POD_PROGRAM,
    ),
    (
        "a tenant forges provenance",
        TENANT,
        _config_map("forged", annotations={PROVENANCE_KEYS[2]: "7"}),
        PROVENANCE_OWNED,
    ),
    # Admitted.
    ("the monitor creates its ConfigMap", MONITOR, _MANAGED_CONFIG_MAP, None),
    ("the monitor creates its admin Secret", MONITOR, _MANAGED_SECRET, None),
    ("the monitor creates its Deployment", MONITOR, _MANAGED_DEPLOYMENT, None),
    ("the monitor creates its Service", MONITOR, _MANAGED_SERVICE, None),
    ("the monitor creates the shared master", MONITOR, _MASTER, None),
    ("a tenant creates its own ConfigMap", TENANT, _TENANT_CONFIG_MAP, None),
    ("a tenant creates a Secret of its own", TENANT, _secret("tenant-secret"), None),
    (
        "a tenant Deployment whose pods carry the label (the look-alike residual)",
        TENANT,
        _deployment("tenant-ep", pod_labels=MANAGED_LABELS),
        None,
    ),
    (
        "a generated name",
        TENANT,
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"generateName": "gen-", "namespace": INFERENCE},
        },
        None,
    ),
]


@pytest.mark.parametrize(
    ("user", "obj", "expected"),
    [case[1:] for case in CREATE_CASES],
    ids=[case[0] for case in CREATE_CASES],
)
def test_creates_in_gco_inference(user: str, obj: dict[str, Any], expected: str | None) -> None:
    _assert_verdict(_review(obj, user=user), expected)


UPDATE_CASES = [
    # What a managed object runs, serves or carries.
    (
        "a tenant rewrites the sidecar program",
        TENANT,
        _MANAGED_CONFIG_MAP,
        _set("data.program.py", "evil()"),
        MANAGED_CONTENT,
    ),
    (
        "a tenant adds binaryData",
        TENANT,
        _MANAGED_CONFIG_MAP,
        _set("binaryData", {"x": "AA=="}),
        MANAGED_CONTENT,
    ),
    ("a tenant freezes it", TENANT, _MANAGED_CONFIG_MAP, _set("immutable", True), MANAGED_CONTENT),
    (
        # stringData arrives merged into data.
        "a tenant rewrites the admin key",
        TENANT,
        _MANAGED_SECRET,
        _set("data.key", base64.b64encode(b"known").decode()),
        MANAGED_CONTENT,
    ),
    (
        "a tenant swaps the model image",
        TENANT,
        _MANAGED_DEPLOYMENT,
        _set_image("registry.example/evil:v1"),
        MANAGED_CONTENT,
    ),
    (
        "a tenant restarts the endpoint",
        TENANT,
        _MANAGED_DEPLOYMENT,
        _set("spec.template.metadata.annotations", {"kubectl.kubernetes.io/restartedAt": "now"}),
        MANAGED_CONTENT,
    ),
    (
        "a tenant repoints the Service",
        TENANT,
        _MANAGED_SERVICE,
        _set("spec.selector", {"app": "mine"}),
        MANAGED_CONTENT,
    ),
    (
        "a tenant swaps the master's image",
        TENANT,
        _MASTER,
        _set_image("registry.example/evil:v1"),
        MANAGED_CONTENT,
    ),
    (
        "a tenant rewrites an unlabelled pod program from before the fence",
        TENANT,
        _LEGACY_POD_PROGRAM,
        _set("data.program.py", "evil()"),
        MANAGED_CONTENT,
    ),
    (
        "the applier is not the monitor",
        APPLIER,
        _MANAGED_CONFIG_MAP,
        _set("data.program.py", "x"),
        MANAGED_CONTENT,
    ),
    # The label.
    (
        "a tenant removes the label",
        TENANT,
        _MANAGED_CONFIG_MAP,
        _drop_label(LABEL_KEY),
        ONLY_THE_MONITOR,
    ),
    (
        "a tenant unlabels a Deployment and swaps its image in one request",
        TENANT,
        _MANAGED_DEPLOYMENT,
        _both(_drop_label(LABEL_KEY), _set_image("registry.example/evil:v1")),
        ONLY_THE_MONITOR,
    ),
    (
        "a tenant changes the label",
        TENANT,
        _MANAGED_DEPLOYMENT,
        _set_label(LABEL_KEY, "other"),
        ONLY_THE_MONITOR,
    ),
    (
        "a tenant labels its own object",
        TENANT,
        _TENANT_CONFIG_MAP,
        _set_label(LABEL_KEY, LABEL_VALUE),
        ONLY_THE_MONITOR,
    ),
    # Provenance, on any object.
    (
        "a tenant stamps provenance",
        TENANT,
        _TENANT_CONFIG_MAP,
        _set_annotation(PROVENANCE_KEYS[0], "x"),
        PROVENANCE_OWNED,
    ),
    (
        "a tenant changes stamped provenance",
        TENANT,
        _STAMPED_CONFIG_MAP,
        _set_annotation(PROVENANCE_KEYS[1], "x"),
        PROVENANCE_OWNED,
    ),
    (
        "a tenant removes stamped provenance",
        TENANT,
        _STAMPED_CONFIG_MAP,
        _drop_annotation(PROVENANCE_KEYS[2]),
        PROVENANCE_OWNED,
    ),
    # Admitted: metadata that decides nothing, the tenant's own objects, the monitor.
    (
        "a tenant annotates a managed ConfigMap",
        TENANT,
        _MANAGED_CONFIG_MAP,
        _set_annotation("example.com/note", "hi"),
        None,
    ),
    (
        "a tenant annotates a managed Deployment",
        TENANT,
        _MANAGED_DEPLOYMENT,
        _set_annotation("example.com/note", "hi"),
        None,
    ),
    (
        "a tenant annotates the master",
        TENANT,
        _MASTER,
        _set_annotation("example.com/note", "hi"),
        None,
    ),
    ("a tenant re-applies a managed object unchanged", TENANT, _MANAGED_SERVICE, _no_change, None),
    (
        "a tenant edits its own ConfigMap",
        TENANT,
        _TENANT_CONFIG_MAP,
        _set("data.program.py", "mine()"),
        None,
    ),
    (
        "a tenant keeps stamped provenance while editing",
        TENANT,
        _STAMPED_CONFIG_MAP,
        _set("data.k", "v"),
        None,
    ),
    (
        "the garbage collector orphans a managed object",
        GARBAGE_COLLECTOR,
        _changed(
            _MANAGED_DEPLOYMENT, _set("metadata.ownerReferences", [{"kind": "X", "name": "x"}])
        ),
        _drop("metadata.ownerReferences"),
        None,
    ),
    (
        "the monitor rolls its Deployment",
        MONITOR,
        _MANAGED_DEPLOYMENT,
        _set_image("registry.example/model:v2"),
        None,
    ),
    (
        "the monitor republishes its program",
        MONITOR,
        _MANAGED_CONFIG_MAP,
        _set("data.program.py", "v2"),
        None,
    ),
    (
        "the monitor claims its epoch",
        MONITOR,
        _MANAGED_SECRET,
        _set_annotation(PROVENANCE_KEYS[2], "8"),
        None,
    ),
    (
        "the monitor adopts a legacy object",
        MONITOR,
        _TENANT_CONFIG_MAP,
        _set_label(LABEL_KEY, LABEL_VALUE),
        None,
    ),
]


@pytest.mark.parametrize(
    ("user", "old", "change", "expected"),
    [case[1:] for case in UPDATE_CASES],
    ids=[case[0] for case in UPDATE_CASES],
)
def test_updates_in_gco_inference(
    user: str,
    old: dict[str, Any],
    change: Callable[[dict[str, Any]], None],
    expected: str | None,
) -> None:
    _assert_verdict(_review(_changed(old, change), user=user, old=old), expected)


# ─── The endpoint keypair and the trust bundle ─────────────────────


class TestOwnedByOneController:
    _KEYPAIR = _secret(
        KEYPAIR,
        type="kubernetes.io/tls",
        data={"tls.crt": "AA==", "tls.key": "AA==", "ca.crt": "AA=="},
    )
    _BUNDLE = _config_map("gco-internal-ca", JOBS, data={"ca.crt": "-----BEGIN CERTIFICATE-----"})

    @pytest.mark.parametrize("user", [TENANT, MONITOR, APPLIER])
    def test_nobody_but_cert_manager_writes_the_endpoint_keypair(self, user: str) -> None:
        """The pre-creation gap: a planted ca.crt would be trusted by every PD proxy."""
        _assert_verdict(_review(self._KEYPAIR, user=user), KEYPAIR_OWNED)
        annotated = _changed(self._KEYPAIR, _set_annotation("example.com/note", "hi"))
        _assert_verdict(_review(annotated, user=user, old=self._KEYPAIR), KEYPAIR_OWNED)
        replaced = _changed(self._KEYPAIR, _set("data.ca.crt", "AQ=="))
        _assert_verdict(_review(replaced, user=user, old=self._KEYPAIR), KEYPAIR_OWNED)

    def test_cert_manager_issues_and_reissues_it(self) -> None:
        assert _review(self._KEYPAIR, user=CERT_MANAGER) == []
        reissued = _changed(self._KEYPAIR, _set("data.tls.crt", "AQ=="))
        assert _review(reissued, user=CERT_MANAGER, old=self._KEYPAIR) == []
        # A Secret of the same name elsewhere is not the keypair.
        assert _review(_secret(KEYPAIR, JOBS), user=TENANT) == []

    @pytest.mark.parametrize("user", [TENANT, APPLIER, CERT_MANAGER])
    def test_nobody_but_trust_manager_writes_the_jobs_trust_bundle(self, user: str) -> None:
        _assert_verdict(_review(self._BUNDLE, user=user), BUNDLE_OWNED)
        rewritten = _changed(self._BUNDLE, _set("data.ca.crt", "planted"))
        _assert_verdict(_review(rewritten, user=user, old=self._BUNDLE), BUNDLE_OWNED)

    def test_trust_manager_publishes_and_refreshes_it(self) -> None:
        assert _review(self._BUNDLE, user=TRUST_MANAGER) == []
        refreshed = _changed(self._BUNDLE, _set("data.ca.crt", "rotated"))
        assert _review(refreshed, user=TRUST_MANAGER, old=self._BUNDLE) == []
        # Nothing reads a ConfigMap of that name in gco-inference.
        assert _review(_config_map("gco-internal-ca"), user=TENANT) == []

    def test_gco_jobs_has_no_monitor_objects(self) -> None:
        """Only the bundle is fenced there: the label and annotations mean nothing."""
        for obj in (
            _config_map("labelled", JOBS, labels=MANAGED_LABELS),
            _config_map("stamped", JOBS, annotations=PROVENANCE),
            _config_map("ep-tls-proxy", JOBS),
            _secret("ep-admin", JOBS, labels=MANAGED_LABELS),
        ):
            assert _review(obj, user=TENANT) == []
            assert _review(_changed(obj, _set("data.k", "v")), user=TENANT, old=obj) == []


# ─── What is never evaluated ───────────────────────────────────────


@pytest.mark.parametrize(
    ("obj", "operation", "subresource"),
    [
        # DELETE: namespace deletion, garbage collection, and a tenant's
        # delete, which the monitor repairs by recreating the object.
        (_MANAGED_CONFIG_MAP, "DELETE", ""),
        (_secret(KEYPAIR), "DELETE", ""),
        # Controllers' status writes, and the scale subresource HPAs use.
        (_MANAGED_DEPLOYMENT, "UPDATE", "status"),
        (_MANAGED_DEPLOYMENT, "UPDATE", "scale"),
        (_MASTER, "UPDATE", "scale"),
        # Kinds the fence leaves alone.
        (_object("Pod", "look-alike", labels=MANAGED_LABELS), "CREATE", ""),
        (_object("Job", "netpol-target", labels={"app": "x"}), "CREATE", ""),
        (_object("HorizontalPodAutoscaler", "ep", labels=MANAGED_LABELS), "UPDATE", ""),
        (_object("ScaledObject", "ep", labels=MANAGED_LABELS), "UPDATE", ""),
        # Namespaces the fence leaves alone.
        (_config_map("ep-tls-proxy", "gco-system", labels=MANAGED_LABELS), "CREATE", ""),
        (_config_map("gco-internal-ca", "default"), "CREATE", ""),
    ],
    ids=[
        "delete-managed",
        "delete-keypair",
        "status",
        "scale",
        "scale-master",
        "pod",
        "job",
        "hpa",
        "scaledobject",
        "gco-system",
        "default",
    ],
)
def test_requests_the_policy_does_not_select(
    obj: dict[str, Any], operation: str, subresource: str
) -> None:
    assert _review(obj, user=TENANT, operation=operation, subresource=subresource) is None


def test_the_unfenced_kinds_a_tenant_can_write_are_the_documented_ones() -> None:
    """HPAs and ScaledObjects are the monitor's kinds a tenant role reaches unfenced.

    Every other kind the monitor manages is matched, and every matched kind is
    one the tenant roles can write, so the fence neither misses nor overreaches
    (the three roles' write rules are pinned equal by tests/test_kubectl_applier.py).
    """
    (role,) = [
        doc
        for doc in _documents(MANIFESTS_DIR / "07-kro-tenant-access.yaml")
        if doc["kind"] == "Role" and doc["metadata"]["namespace"] == INFERENCE
    ]
    writable = {
        resource
        for rule in role["rules"]
        if {"create", "update", "patch"} <= set(rule["verbs"])
        for resource in rule["resources"]
    }
    fenced = {
        resource
        for rule in _policy()["spec"]["matchConstraints"]["resourceRules"]
        for resource in rule["resources"]
    }
    assert fenced == _FENCED_RESOURCES
    assert fenced <= writable
    managed = {
        "deployments",
        "services",
        "horizontalpodautoscalers",
        "scaledobjects",
        "configmaps",
        "secrets",
        "statefulsets",
    }
    inventory = InferenceMonitor._endpoint_resource_inventory("ep")
    assert {
        "deployments",
        "services",
        "horizontal_pod_autoscalers",
        "scaled_objects",
        "config_maps",
    } <= set(vars(inventory))
    assert (managed - fenced) & writable == {"horizontalpodautoscalers", "scaledobjects"}
    raw = (MANIFESTS_DIR / FENCE).read_text(encoding="utf-8")
    assert "HorizontalPodAutoscalers and ScaledObjects are not fenced" in raw


# ─── Everything GCO ships or tells users to apply ──────────────────


def _fenced_objects(paths: list[Path]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every top-level object of a matched kind, in each namespace it may land in.

    An object without a namespace is submitted to one of the tenant
    namespaces, so it is checked in both.
    """
    for path in paths:
        for doc in _documents(path):
            if doc.get("kind") not in _RESOURCES:
                continue
            namespace = doc.get("metadata", {}).get("namespace")
            for target in [namespace] if namespace else [INFERENCE, JOBS]:
                placed = copy.deepcopy(doc)
                placed["metadata"]["namespace"] = target
                yield f"{path.relative_to(REPO_ROOT)}: {doc['kind']} {target}", placed


def test_every_object_the_applier_ships_is_admitted() -> None:
    checked = 0
    for where, obj in _fenced_objects(sorted(MANIFESTS_DIR.glob("*.yaml"))):
        verdict = _review(obj, user=APPLIER)
        assert verdict in (None, []), f"{where}: {verdict}"
        checked += verdict is not None
    # The per-namespace storage ConfigMaps land in both tenant namespaces.
    assert checked >= 10


def test_every_example_and_harness_object_is_admitted_for_an_operator() -> None:
    paths = sorted(EXAMPLES_DIR.glob("*.yaml")) + sorted(HARNESS_MANIFESTS_DIR.glob("*.yaml"))
    checked = 0
    for where, obj in _fenced_objects(paths):
        verdict = _review(obj, user=APPLIER)
        assert verdict in (None, []), f"{where}: {verdict}"
        checked += verdict is not None
    assert checked >= 7


def _labelled_objects(node: Any, path: str = "") -> Iterator[str]:
    """Every object (anything with a kind) whose own labels carry the monitor's label."""
    if isinstance(node, dict):
        labels = (node.get("metadata") or {}).get("labels") or {}
        if "kind" in node and labels.get(LABEL_KEY) == LABEL_VALUE:
            yield f"{path} ({node['kind']})"
        for key, value in node.items():
            yield from _labelled_objects(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _labelled_objects(value, f"{path}[{index}]")


def test_no_example_labels_an_object_as_the_monitors() -> None:
    """Pod templates keep the label, so the model pods keep their NetworkPolicies.

    On an object in gco-inference the label claims the monitor's provenance,
    which only the monitor may do; that includes objects nested in a
    ResourceGraphDefinition or a Composition, which kro or Crossplane creates.
    """
    offenders = [
        f"{path.name}{where}"
        for path in sorted(EXAMPLES_DIR.glob("*.yaml"))
        for doc in _documents(path)
        for where in _labelled_objects(doc)
    ]
    assert offenders == []
    for name in ("inference-vllm.yaml", "inference-sglang.yaml", "inference-triton.yaml"):
        (deployment,) = [
            doc for doc in _documents(EXAMPLES_DIR / name) if doc["kind"] == "Deployment"
        ]
        pod_labels = deployment["spec"]["template"]["metadata"]["labels"]
        assert pod_labels[LABEL_KEY] == LABEL_VALUE, name
        assert LABEL_KEY not in deployment["metadata"]["labels"], name


def test_the_kind_probes_expect_the_messages_evaluated_here() -> None:
    """integration:kind:examples-smoke greps for exactly these messages."""
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "integration-tests.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["integration-kind-examples-smoke"]["steps"]
    run = next(
        step["run"]
        for step in steps
        if step.get("name") == "Prove the tenant write fence in the real API server"
    )
    for fragment in (ONLY_THE_MONITOR, MANAGED_CONTENT, POD_PROGRAM, PROVENANCE_OWNED):
        assert fragment in run, fragment
    assert "only the cert-manager controller" in KEYPAIR_OWNED
    assert f"'{POLICY}'" in run
