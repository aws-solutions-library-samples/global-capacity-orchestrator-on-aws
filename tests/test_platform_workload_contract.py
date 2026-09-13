"""
One hosting contract for every GCO platform workload, pinned to the manifests.

The five ``gco-system`` Deployments (``30``–``34``) and the queue-processor
``ScaledJob`` share one pod shape — documented in
``lambda/kubectl-applier-simple/manifests/README.md`` ("Platform Workload
Contract"). Each property here exists because its absence has a concrete
failure mode: a container without limits can starve a node, a Deployment
without a PDB loses both replicas to one node drain, a probed port without an
ingress rule crash-loops under default-deny, an HPA target whose Deployment
still asserts ``replicas`` is reset on every re-apply. A new service, or an
edit to an old one, cannot quietly drop part of the contract.

The tests parse the shipped manifests with the same typed placeholder
rendering the regional stack performs (integers for replica counts and HPA
targets, quantities for limits, strings elsewhere).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

MANIFESTS_DIR = (
    Path(__file__).resolve().parent.parent / "lambda" / "kubectl-applier-simple" / "manifests"
)
HANDLER_DIR = MANIFESTS_DIR.parent

DEPLOYMENT_FILES = (
    "30-health-monitor.yaml",
    "31-manifest-processor.yaml",
    "32-inference-monitor.yaml",
    "33-inference-proxy.yaml",
    "34-cost-monitor.yaml",
)
SCALED_JOB_FILE = "post-helm-sqs-consumer.yaml"
TLS_SIDECAR = "api-tls-proxy"
IRSA_TOKEN_PATH = "/var/run/secrets/eks.amazonaws.com/serviceaccount/token"  # noqa: S105 - a mount path, not a secret

#: Typed placeholder rendering: bare integers where Kubernetes wants integers,
#: quantities where it wants quantities. Everything else becomes a string.
INTEGER_TOKENS = {
    "{{MP_REPLICAS}}": "3",
    "{{MP_HPA_MAX_REPLICAS}}": "6",
    "{{MP_HPA_CPU_TARGET_UTILIZATION}}": "70",
    "{{INFERENCE_PROXY_MIN_REPLICAS}}": "3",
    "{{INFERENCE_PROXY_MAX_REPLICAS}}": "10",
    "{{INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION}}": "70",
}
QUANTITY_TOKENS = {
    "{{MP_CPU_LIMIT}}": "1000m",
    "{{MP_MEMORY_LIMIT}}": "2Gi",
    "{{INFERENCE_PROXY_TLS_CPU_REQUEST}}": "100m",
}
POD_SECURITY_CONTEXT = {
    "runAsNonRoot": True,
    "runAsUser": 1000,
    "runAsGroup": 1000,
    "fsGroup": 1000,
    "seccompProfile": {"type": "RuntimeDefault"},
}
CONTAINER_SECURITY_CONTEXT = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "capabilities": {"drop": ["ALL"]},
}
PROBES = ("startupProbe", "livenessProbe", "readinessProbe")
STARTUP_BUDGET_SECONDS = 120


def _render(filename: str) -> str:
    text = (MANIFESTS_DIR / filename).read_text(encoding="utf-8")
    for token, value in {**INTEGER_TOKENS, **QUANTITY_TOKENS}.items():
        text = text.replace(token, value)
    return re.sub(r"\{\{[A-Z0-9_]+\}\}", "placeholder", text)


def _documents(filename: str) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(_render(filename)) if isinstance(doc, dict)]


def _raw(filename: str) -> str:
    return (MANIFESTS_DIR / filename).read_text(encoding="utf-8")


def _deployment(filename: str) -> dict[str, Any]:
    (deployment,) = [doc for doc in _documents(filename) if doc["kind"] == "Deployment"]
    return deployment


def _pod_spec(deployment: dict[str, Any]) -> dict[str, Any]:
    return deployment["spec"]["template"]["spec"]


def _matches(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    wanted = (selector or {}).get("matchLabels") or {}
    return all(labels.get(key) == value for key, value in wanted.items())


@pytest.fixture(scope="module")
def handler_module():
    """Import the kubectl-applier handler (pure constants, no cluster access)."""
    sys.path.insert(0, str(HANDLER_DIR))
    try:
        sys.modules.pop("handler", None)
        import handler

        yield handler
    finally:
        sys.path.pop(0)
        sys.modules.pop("handler", None)


# ─── Rollout ───────────────────────────────────────────────────────


class TestRollout:
    @pytest.mark.parametrize("filename", DEPLOYMENT_FILES)
    def test_history_and_min_ready_are_bounded(self, filename):
        spec = _deployment(filename)["spec"]
        assert spec["revisionHistoryLimit"] == 3
        assert spec["minReadySeconds"] == 10

    @pytest.mark.parametrize("filename", DEPLOYMENT_FILES)
    def test_strategy_matches_the_replica_count(self, filename):
        """Zero-unavailability rollouts, except the single-writer cost monitor.

        A one-replica Deployment cannot roll without either two writers
        (surge) or an outage (Recreate); Recreate is the deliberate choice
        and the pod is marked do-not-disrupt so consolidation never evicts
        the singleton either.
        """
        deployment = _deployment(filename)
        replicas = deployment["spec"]["replicas"]
        assert type(replicas) is int, f"{filename}: replicas must render as an integer"
        strategy = deployment["spec"]["strategy"]
        annotations = deployment["spec"]["template"]["metadata"]["annotations"]
        if replicas == 1:
            assert strategy == {"type": "Recreate"}
            assert annotations.get("karpenter.sh/do-not-disrupt") == "true"
        else:
            assert strategy == {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
            }

    @pytest.mark.parametrize("filename", DEPLOYMENT_FILES)
    def test_every_deploy_rolls_exactly_once(self, filename):
        """The deployment-timestamp annotation is the one rollout trigger."""
        deployment = _deployment(filename)
        assert (
            "gco.aws/deployment-timestamp"
            in deployment["spec"]["template"]["metadata"]["annotations"]
        )
        assert 'gco.aws/deployment-timestamp: "{{DEPLOYMENT_TIMESTAMP}}"' in _raw(filename)
        assert "kubectl.kubernetes.io/restartedAt" not in _raw(filename)


# ─── Placement and disruption ─────────────────────────────────────


class TestPlacementAndDisruption:
    @pytest.mark.parametrize("filename", DEPLOYMENT_FILES)
    def test_multi_replica_workloads_spread_and_carry_a_one_at_a_time_budget(self, filename):
        deployment = _deployment(filename)
        app = deployment["spec"]["selector"]["matchLabels"]["app"]
        pod_spec = _pod_spec(deployment)
        budgets = [doc for doc in _documents(filename) if doc["kind"] == "PodDisruptionBudget"]
        if deployment["spec"]["replicas"] == 1:
            assert budgets == [], f"{filename}: a PDB on a singleton would block node drains"
            assert "topologySpreadConstraints" not in pod_spec
            return

        spread = pod_spec["topologySpreadConstraints"]
        assert [(c["topologyKey"], c["whenUnsatisfiable"], c["maxSkew"]) for c in spread] == [
            ("topology.kubernetes.io/zone", "ScheduleAnyway", 1),
            ("kubernetes.io/hostname", "ScheduleAnyway", 1),
        ]
        for constraint in spread:
            assert constraint["labelSelector"] == {"matchLabels": {"app": app}}
        preferred = pod_spec["affinity"]["podAntiAffinity"][
            "preferredDuringSchedulingIgnoredDuringExecution"
        ]
        assert {term["podAffinityTerm"]["topologyKey"] for term in preferred} == {
            "kubernetes.io/hostname",
            "topology.kubernetes.io/zone",
        }
        for term in preferred:
            assert term["podAffinityTerm"]["labelSelector"] == {"matchLabels": {"app": app}}

        (budget,) = budgets
        # maxUnavailable, never minAvailable: one eviction at a time however
        # far an HPA or an operator has scaled the Deployment.
        assert budget["spec"] == {
            "maxUnavailable": 1,
            "selector": deployment["spec"]["selector"],
        }
        assert budget["metadata"]["namespace"] == deployment["metadata"]["namespace"]

    @pytest.mark.parametrize("filename", DEPLOYMENT_FILES)
    def test_platform_pods_preempt_user_workloads(self, filename):
        assert _pod_spec(_deployment(filename))["priorityClassName"] == "gco-platform-critical"


# ─── Pod and container shape ──────────────────────────────────────


class TestPodShape:
    @pytest.mark.parametrize("filename", DEPLOYMENT_FILES)
    def test_pod_level_hardening(self, filename):
        pod_spec = _pod_spec(_deployment(filename))
        assert pod_spec["securityContext"] == POD_SECURITY_CONTEXT
        assert pod_spec["automountServiceAccountToken"] is False
        assert pod_spec["enableServiceLinks"] is False
        assert type(pod_spec["terminationGracePeriodSeconds"]) is int
        assert pod_spec["terminationGracePeriodSeconds"] >= 30

    @pytest.mark.parametrize("filename", DEPLOYMENT_FILES)
    def test_every_container_is_bounded_hardened_and_cached(self, filename):
        for container in _pod_spec(_deployment(filename))["containers"]:
            where = f"{filename}/{container['name']}"
            assert container["imagePullPolicy"] == "IfNotPresent", where
            assert container["securityContext"] == CONTAINER_SECURITY_CONTEXT, where
            resources = container["resources"]
            for section in ("requests", "limits"):
                for resource in ("cpu", "memory"):
                    value = resources[section][resource]
                    assert isinstance(value, str) and value, f"{where}: {section}.{resource}"

    @pytest.mark.parametrize("filename", DEPLOYMENT_FILES)
    def test_every_container_has_three_honest_probes(self, filename):
        """Startup budget of at least two minutes; exec probes fork python, not a shell."""
        for container in _pod_spec(_deployment(filename))["containers"]:
            where = f"{filename}/{container['name']}"
            for probe_name in PROBES:
                probe = container[probe_name]
                handlers = [key for key in ("exec", "httpGet", "tcpSocket") if key in probe]
                assert len(handlers) == 1, f"{where}: {probe_name} needs exactly one handler"
                assert probe["timeoutSeconds"] >= 3, f"{where}: {probe_name} timeout"
                assert probe["failureThreshold"] >= 3, f"{where}: {probe_name} failureThreshold"
                if "exec" in probe:
                    # The distroless images ship no shell: python is the only
                    # executable, and the loopback address keeps the probe off
                    # the network entirely. The interpreter starts lean (-I -S)
                    # and speaks HTTP over a bare socket: a probe is a process
                    # competing with the server for the container's CPU, and a
                    # live run saw urllib-importing probes time out and restart
                    # a healthy container on a contended node.
                    command = probe["exec"]["command"]
                    assert command[:4] == ["python", "-I", "-S", "-c"], (
                        f"{where}: {probe_name} {command}"
                    )
                    assert "127.0.0.1" in command[-1], f"{where}: {probe_name} must probe loopback"
                    assert "import socket" in command[-1], f"{where}: {probe_name} {command}"
                    assert "urllib" not in command[-1], f"{where}: {probe_name} imports urllib"
            startup = container["startupProbe"]
            budget = startup["periodSeconds"] * startup["failureThreshold"]
            assert budget >= STARTUP_BUDGET_SECONDS, f"{where}: startup budget {budget}s"

    @pytest.mark.parametrize("filename", (*DEPLOYMENT_FILES, SCALED_JOB_FILE))
    def test_every_scratch_volume_is_bounded(self, filename):
        docs = _documents(filename)
        pod_specs = [_pod_spec(doc) for doc in docs if doc["kind"] == "Deployment"] + [
            doc["spec"]["jobTargetRef"]["template"]["spec"]
            for doc in docs
            if doc["kind"] == "ScaledJob"
        ]
        assert pod_specs, filename
        for pod_spec in pod_specs:
            for volume in pod_spec.get("volumes", []):
                if "emptyDir" in volume:
                    assert volume["emptyDir"].get("sizeLimit"), f"{filename}: {volume['name']}"
                assert "hostPath" not in volume, f"{filename}: {volume['name']}"


# ─── Identity ──────────────────────────────────────────────────────


class TestIdentity:
    def test_handler_inventory_matches_the_manifests(self, handler_module):
        """PLATFORM_DEPLOYMENTS is the post-apply credential check's scope."""
        shipped = {
            (
                deployment["metadata"]["namespace"],
                deployment["metadata"]["name"],
                _pod_spec(deployment)["serviceAccountName"],
            )
            for deployment in map(_deployment, DEPLOYMENT_FILES)
        }
        assert set(handler_module.PLATFORM_DEPLOYMENTS) == shipped
        assert len(handler_module.PLATFORM_DEPLOYMENTS) == len(shipped)
        workload_accounts = {
            (doc["metadata"]["namespace"], doc["metadata"]["name"])
            for doc in _documents("01-serviceaccounts.yaml")
            if doc["kind"] == "ServiceAccount"
        }
        assert set(handler_module.WORKLOAD_SERVICE_ACCOUNTS) == workload_accounts

    @pytest.mark.parametrize("filename", DEPLOYMENT_FILES)
    def test_only_the_application_container_holds_aws_credentials(self, filename):
        deployment = _deployment(filename)
        pod_spec = _pod_spec(deployment)
        annotations = deployment["spec"]["template"]["metadata"]["annotations"]
        volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
        (token_source,) = volumes["aws-iam-token"]["projected"]["sources"]
        assert token_source["serviceAccountToken"]["audience"] == "sts.amazonaws.com"

        for container in pod_spec["containers"]:
            env = {item["name"]: item.get("value") for item in container.get("env", [])}
            mounts = {mount["name"]: mount for mount in container.get("volumeMounts", [])}
            if container["name"] == TLS_SIDECAR:
                assert "AWS_ROLE_ARN" not in env, filename
                assert "aws-iam-token" not in mounts, filename
                assert annotations["eks.amazonaws.com/skip-containers"] == TLS_SIDECAR, filename
            else:
                assert "AWS_ROLE_ARN" in env, f"{filename}/{container['name']}"
                assert env["AWS_WEB_IDENTITY_TOKEN_FILE"] == IRSA_TOKEN_PATH
                assert mounts["aws-iam-token"]["mountPath"] == str(Path(IRSA_TOKEN_PATH).parent)
                assert mounts["aws-iam-token"]["readOnly"] is True

    @pytest.mark.parametrize("filename", DEPLOYMENT_FILES)
    def test_kubernetes_api_token_is_projected_exactly_for_rbac_bound_accounts(self, filename):
        """automount is off; accounts with a binding get an explicit short-lived token."""
        deployment = _deployment(filename)
        pod_spec = _pod_spec(deployment)
        bound = {
            subject["name"]
            for doc in _documents("02-rbac.yaml")
            if doc["kind"] in ("RoleBinding", "ClusterRoleBinding")
            for subject in doc.get("subjects", [])
            if subject.get("kind") == "ServiceAccount" and subject.get("namespace") == "gco-system"
        }
        has_token = "kubernetes-api-token" in {volume["name"] for volume in pod_spec["volumes"]}
        assert has_token == (pod_spec["serviceAccountName"] in bound), (
            f"{filename}: {pod_spec['serviceAccountName']} bound={bound}"
        )
        if has_token:
            (volume,) = [v for v in pod_spec["volumes"] if v["name"] == "kubernetes-api-token"]
            kinds = [next(iter(source)) for source in volume["projected"]["sources"]]
            assert kinds == ["serviceAccountToken", "configMap", "downwardAPI"]
            assert (
                volume["projected"]["sources"][0]["serviceAccountToken"]["expirationSeconds"]
                <= 3600
            )


# ─── Services, autoscaling, network ───────────────────────────────


class TestServiceWiring:
    @pytest.mark.parametrize("filename", DEPLOYMENT_FILES)
    def test_services_target_named_container_ports(self, filename):
        docs = _documents(filename)
        deployment = _deployment(filename)
        ports = {
            port["name"]
            for container in _pod_spec(deployment)["containers"]
            for port in container.get("ports", [])
        }
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        for service in (doc for doc in docs if doc["kind"] == "Service"):
            assert service["spec"]["type"] == "ClusterIP", filename
            assert all(labels.get(k) == v for k, v in service["spec"]["selector"].items()), filename
            for port in service["spec"]["ports"]:
                assert port["targetPort"] in ports, f"{filename}: {port['targetPort']}"

    def test_hpas_and_replica_ownership_annotations_pair_up(self):
        """Every HPA target hands replica ownership to the HPA, and only those do.

        Otherwise the applier re-asserts ``replicas`` on every deploy and
        resets the HPA's scale decision. The manifest-processor pairing is a
        placeholder on both sides because that HPA is feature-gated.
        """
        deployments: dict[str, tuple[str, dict[str, Any]]] = {}
        hpas: list[tuple[str, dict[str, Any]]] = []
        for path in sorted(MANIFESTS_DIR.glob("*.yaml")):
            for doc in _documents(path.name):
                if doc["kind"] == "Deployment" and doc["metadata"]["namespace"] == "gco-system":
                    deployments[doc["metadata"]["name"]] = (path.name, doc)
                elif doc["kind"] == "HorizontalPodAutoscaler":
                    hpas.append((path.name, doc))

        targeted = set()
        for hpa_file, hpa in hpas:
            ref = hpa["spec"]["scaleTargetRef"]
            assert ref["kind"] == "Deployment" and ref["apiVersion"] == "apps/v1", hpa_file
            deployment_file, deployment = deployments[ref["name"]]
            targeted.add(ref["name"])
            owns = deployment["metadata"]["annotations"]["gco.aws/hpa-controls-replicas"]
            assert owns in ("true", "placeholder"), f"{deployment_file}: {owns}"
            # The HPA floor is the Deployment's create-time replica count, so
            # enabling autoscaling never shrinks the HA baseline.
            assert hpa["spec"]["minReplicas"] == deployment["spec"]["replicas"], hpa_file
            assert hpa["spec"]["maxReplicas"] >= hpa["spec"]["minReplicas"], hpa_file
            assert hpa["spec"]["behavior"]["scaleDown"]["stabilizationWindowSeconds"] >= 300, (
                hpa_file
            )
            for metric in hpa["spec"]["metrics"]:
                assert metric["type"] == "ContainerResource", hpa_file
                container_names = {c["name"] for c in _pod_spec(deployment)["containers"]}
                assert metric["containerResource"]["container"] in container_names, hpa_file

        for name, (deployment_file, deployment) in deployments.items():
            annotations = deployment["metadata"].get("annotations") or {}
            has_annotation = "gco.aws/hpa-controls-replicas" in annotations
            assert has_annotation == (name in targeted), f"{deployment_file}: {name}"

        # The gated pair: the same token family on both sides of the gate.
        assert '"{{MP_HPA_CONTROLS_REPLICAS}}"' in _raw("31-manifest-processor.yaml")
        assert "{{MP_HPA_ENABLED}}" in _raw("35-manifest-processor-hpa.yaml")
        assert "minReplicas: {{MP_REPLICAS}}" in _raw("35-manifest-processor-hpa.yaml")
        assert "replicas: {{MP_REPLICAS}}" in _raw("31-manifest-processor.yaml")

    @pytest.mark.parametrize("filename", DEPLOYMENT_FILES)
    def test_every_platform_workload_is_admitted_by_an_ingress_rule(self, filename):
        """Under default-deny a workload no rule admits is unreachable."""
        deployment = _deployment(filename)
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        admitting = []
        for path in sorted(MANIFESTS_DIR.glob("*.yaml")):
            for doc in _documents(path.name):
                if doc["kind"] != "NetworkPolicy" or doc["metadata"]["namespace"] != "gco-system":
                    continue
                if "Ingress" not in doc["spec"].get("policyTypes", []):
                    continue
                if _matches(doc["spec"].get("podSelector"), labels) and doc["spec"].get("ingress"):
                    admitting.append(doc["metadata"]["name"])
        assert admitting, f"{filename}: no NetworkPolicy admits ingress to {labels}"
        # Every admitted port is one a container in the pod actually listens on.
        listening = {
            port["containerPort"]
            for container in _pod_spec(deployment)["containers"]
            for port in container.get("ports", [])
        }
        for path in sorted(MANIFESTS_DIR.glob("*.yaml")):
            for doc in _documents(path.name):
                if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] in admitting:
                    for rule in doc["spec"]["ingress"]:
                        for port in rule.get("ports", []):
                            assert port["port"] in listening, f"{doc['metadata']['name']}: {port}"


# ─── The queue processor's template ───────────────────────────────


class TestQueueProcessorTemplate:
    @pytest.fixture(scope="class")
    def scaled_job(self) -> dict[str, Any]:
        (scaled_job,) = [doc for doc in _documents(SCALED_JOB_FILE) if doc["kind"] == "ScaledJob"]
        return scaled_job

    def test_batch_pod_shares_the_platform_hardening(self, scaled_job, handler_module):
        template = scaled_job["spec"]["jobTargetRef"]["template"]
        pod_spec = template["spec"]
        assert template["metadata"]["labels"]["project"] == "gco"
        assert pod_spec["restartPolicy"] == "Never"
        assert pod_spec["securityContext"] == POD_SECURITY_CONTEXT
        assert pod_spec["automountServiceAccountToken"] is False
        assert pod_spec["enableServiceLinks"] is False
        assert pod_spec["priorityClassName"] == "gco-platform-critical"
        # Runs as the manifest processor: same role, same RBAC, same
        # credential verification scope.
        assert pod_spec["serviceAccountName"] in {
            sa for _ns, _name, sa in handler_module.PLATFORM_DEPLOYMENTS
        }
        (container,) = pod_spec["containers"]
        assert container["imagePullPolicy"] == "IfNotPresent"
        assert container["securityContext"] == CONTAINER_SECURITY_CONTEXT
        for section in ("requests", "limits"):
            assert set(container["resources"][section]) >= {"cpu", "memory"}
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["AWS_WEB_IDENTITY_TOKEN_FILE"] == IRSA_TOKEN_PATH
        assert scaled_job["spec"]["jobTargetRef"]["activeDeadlineSeconds"] > 0
        assert scaled_job["spec"]["jobTargetRef"]["backoffLimit"] >= 1


# ─── Documentation ─────────────────────────────────────────────────


def test_readme_contract_table_names_every_property_tested_here():
    """The manifests README's contract table and this file describe one contract."""
    readme = (MANIFESTS_DIR / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Platform Workload Contract", 1)[1].split("\n## ", 1)[0]
    assert "tests/test_platform_workload_contract.py" in section
    for needle in (
        "maxUnavailable: 1",
        "topologySpreadConstraints",
        "IfNotPresent",
        "startupProbe",
        "readOnlyRootFilesystem",
        "automountServiceAccountToken: false",
        "revisionHistoryLimit: 3",
        "gco.aws/deployment-timestamp",
        "hpa-controls-replicas",
        "sizeLimit",
        "skip-containers",
    ):
        assert needle in section, needle
