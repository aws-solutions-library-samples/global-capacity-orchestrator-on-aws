"""
Tests for the RBAC manifest at lambda/kubectl-applier-simple/manifests/02-rbac.yaml.

Asserts least-privilege separation across the per-service service
accounts: gco-health-monitor-role is cluster-wide but read-only (only
get/list/watch, no write verbs), manifest processor is scoped to the
gco-jobs namespace, inference monitor is scoped to gco-inference
(where it may get and update only its pre-created election Lease),
each Role/ClusterRole is bound only to its own SA, no role mixes
read+write on cluster-wide secrets, the old gco-cluster-role is gone,
and dedicated SAs live in gco-system. Fixtures load and classify the
YAML docs so each test stays focused on one property.
"""

import ast
import importlib
from pathlib import Path

import pytest
import yaml
from kubernetes import client as k8s_client

RBAC_MANIFEST_PATH = Path("lambda/kubectl-applier-simple/manifests/02-rbac.yaml")

READ_ONLY_VERBS = {"get", "list", "watch"}
WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection"}

ROLE_ANNOTATION = "eks.amazonaws.com/role-arn"
SHARED_ROLE_ANNOTATION_VALUE = "{{SERVICE_ACCOUNT_ROLE_ARN}}"
INFERENCE_MONITOR_ROLE_ANNOTATION_VALUE = "{{INFERENCE_MONITOR_ROLE_ARN}}"
MANIFEST_PROCESSOR_ROLE_ANNOTATION_VALUE = "{{MANIFEST_PROCESSOR_ROLE_ARN}}"
INFERENCE_PROXY_ROLE_ANNOTATION_VALUE = "{{INFERENCE_PROXY_ROLE_ARN}}"
HEALTH_ROLE_ANNOTATION_VALUE = "{{HEALTH_MONITOR_ROLE_ARN}}"


@pytest.fixture(scope="module")
def rbac_docs():
    """Load all YAML documents from the RBAC manifest."""
    content = RBAC_MANIFEST_PATH.read_text()
    docs = list(yaml.safe_load_all(content))
    # Filter out None docs (from empty YAML separators)
    return [d for d in docs if d is not None]


@pytest.fixture(scope="module")
def roles(rbac_docs):
    """Extract all Role and ClusterRole documents."""
    return [d for d in rbac_docs if d.get("kind") in ("Role", "ClusterRole")]


@pytest.fixture(scope="module")
def bindings(rbac_docs):
    """Extract all RoleBinding and ClusterRoleBinding documents."""
    return [d for d in rbac_docs if d.get("kind") in ("RoleBinding", "ClusterRoleBinding")]


@pytest.fixture(scope="module")
def service_accounts(rbac_docs):
    """Extract all ServiceAccount documents."""
    return [d for d in rbac_docs if d.get("kind") == "ServiceAccount"]


def _find_doc(docs, kind, name):
    """Find a document by kind and metadata.name."""
    for d in docs:
        if d.get("kind") == kind and d["metadata"]["name"] == name:
            return d
    return None


def _get_all_verbs(role_doc):
    """Collect all verbs from a role's rules."""
    verbs = set()
    for rule in role_doc.get("rules", []):
        verbs.update(rule.get("verbs", []))
    return verbs


def _get_all_resources(role_doc):
    """Collect all resources from a role's rules as (apiGroup, resource) tuples."""
    resources = set()
    for rule in role_doc.get("rules", []):
        api_groups = rule.get("apiGroups", [""])
        for group in api_groups:
            for resource in rule.get("resources", []):
                resources.add((group, resource))
    return resources


# ─── Health Monitor Role ────────────────────────────────────────────


class TestHealthMonitorRole:
    """health-monitor has read-only cluster-wide access."""

    def test_health_monitor_role_exists(self, rbac_docs):
        role = _find_doc(rbac_docs, "ClusterRole", "gco-health-monitor-role")
        assert role is not None, "gco-health-monitor-role ClusterRole must exist"

    def test_health_monitor_is_cluster_role(self, rbac_docs):
        role = _find_doc(rbac_docs, "ClusterRole", "gco-health-monitor-role")
        assert role["kind"] == "ClusterRole"

    def test_health_monitor_has_only_read_verbs(self, rbac_docs):
        """Health monitor should only have get, list, watch — no write verbs."""
        role = _find_doc(rbac_docs, "ClusterRole", "gco-health-monitor-role")
        all_verbs = _get_all_verbs(role)
        assert all_verbs.issubset(READ_ONLY_VERBS), (
            f"Health monitor role has non-read verbs: {all_verbs - READ_ONLY_VERBS}"
        )

    def test_health_monitor_grants_exactly_what_its_process_calls(self, rbac_docs):
        """Every grant is a call the health-monitor pod makes, and every call is granted.

        The pod runs gco.services.health_api, which hosts the health monitor,
        the webhook dispatcher and their Lease elections. Their Kubernetes API
        calls are read from the source, so a new call fails this test until
        its grant is added, and a grant no call needs fails it too.
        """
        calls = _kubernetes_api_calls(_process_modules("gco.services.health_api"))
        assert set(calls) == set(HEALTH_MONITOR_CALL_GRANTS), (
            "update HEALTH_MONITOR_CALL_GRANTS and 02-rbac.yaml for these calls: "
            f"{sorted(set(calls) ^ set(HEALTH_MONITOR_CALL_GRANTS), key=str)}"
        )
        needed = {grant for call in calls for grant in HEALTH_MONITOR_CALL_GRANTS[call]}
        cluster_role = _find_doc(rbac_docs, "ClusterRole", "gco-health-monitor-role")
        namespaced = _find_doc(rbac_docs, "Role", "gco-health-monitor-self-healing")
        keep_warm_records = _find_doc(rbac_docs, "Role", "gco-health-monitor-keep-warm-records")
        assert keep_warm_records["metadata"]["namespace"] == "gco-jobs"
        granted = (
            {("cluster", *grant) for grant in _grants(cluster_role)}
            | {("gco-system", *grant) for grant in _grants(namespaced)}
            | {("gco-jobs", *grant) for grant in _grants(keep_warm_records)}
        )
        assert granted == needed

    def test_health_monitor_self_healing_is_exactly_scoped(self, rbac_docs):
        """Gateway repair and election access stay in gco-system and named resources."""
        role = _find_doc(rbac_docs, "Role", "gco-health-monitor-self-healing")
        assert role is not None
        assert role["metadata"]["namespace"] == "gco-system"

        rules = {
            (tuple(rule["apiGroups"]), tuple(rule["resources"])): rule for rule in role["rules"]
        }
        gateway_rule = rules[(("gateway.networking.k8s.io",), ("gateways",))]
        assert gateway_rule["verbs"] == ["get"]
        assert gateway_rule["resourceNames"] == ["gco-gateway"]

        lease_rule = rules[(("coordination.k8s.io",), ("leases",))]
        assert set(lease_rule["verbs"]) == {"get", "update"}
        # One named Lease per elected duty: ALB-hostname sync, webhook
        # delivery and the keep-warm idle records. All are pre-created below
        # so no `create` grant is needed.
        assert lease_rule["resourceNames"] == [
            "gco-health-monitor-alb-sync",
            "gco-health-monitor-webhooks",
            "gco-health-monitor-keep-warm",
        ]
        assert "create" not in lease_rule["verbs"]

        for lease_name in lease_rule["resourceNames"]:
            lease = _find_doc(rbac_docs, "Lease", lease_name)
            assert lease is not None, lease_name
            assert lease["metadata"]["namespace"] == "gco-system"
            assert lease["spec"]["leaseDurationSeconds"] == 90

        binding = _find_doc(rbac_docs, "RoleBinding", "gco-health-monitor-self-healing")
        assert binding["roleRef"]["name"] == "gco-health-monitor-self-healing"
        assert binding["subjects"] == [
            {
                "kind": "ServiceAccount",
                "name": "gco-health-monitor-sa",
                "namespace": "gco-system",
            }
        ]


# ─── Manifest Processor Role ───────────────────────────────────────


class TestManifestProcessorRole:
    """manifest-processor is scoped to gco-jobs."""

    def test_manifest_processor_role_exists(self, rbac_docs):
        role = _find_doc(rbac_docs, "Role", "gco-manifest-processor-role")
        assert role is not None, "gco-manifest-processor-role Role must exist"

    def test_manifest_processor_is_namespace_scoped(self, rbac_docs):
        """Manifest processor role must be a Role (not ClusterRole) in gco-jobs."""
        role = _find_doc(rbac_docs, "Role", "gco-manifest-processor-role")
        assert role["kind"] == "Role"
        assert role["metadata"]["namespace"] == "gco-jobs"

    def test_manifest_processor_has_write_verbs(self, rbac_docs):
        """Manifest processor needs create and delete for workload management."""
        role = _find_doc(rbac_docs, "Role", "gco-manifest-processor-role")
        all_verbs = _get_all_verbs(role)
        assert "create" in all_verbs
        assert "delete" in all_verbs

    def test_manifest_processor_covers_workload_resources(self, rbac_docs):
        """Manifest processor should manage jobs, deployments, services, etc."""
        role = _find_doc(rbac_docs, "Role", "gco-manifest-processor-role")
        resources = _get_all_resources(role)
        resource_names = {r[1] for r in resources}
        expected = {"jobs", "pods", "services", "configmaps", "deployments"}
        assert expected.issubset(resource_names), f"Missing resources: {expected - resource_names}"

    def test_manifest_processor_can_read_pod_logs(self, rbac_docs):
        """pods/log must be in the manifest-processor Role so GET /api/v1/jobs/.../logs works.

        Regression: the logs endpoint called core_v1.read_namespaced_pod_log(),
        which requires a dedicated pods/log subresource RBAC rule separate from
        the pods rule. Without it users get 502 "Kubernetes API error: 403 Forbidden".
        """
        role = _find_doc(rbac_docs, "Role", "gco-manifest-processor-role")
        resources = _get_all_resources(role)
        resource_names = {r[1] for r in resources}
        assert "pods/log" in resource_names, (
            "manifest-processor Role must grant pods/log for logs endpoint"
        )

    def test_manifest_processor_can_read_events(self, rbac_docs):
        """events must be in the manifest-processor Role so the events endpoint works.

        Regression: GET /api/v1/jobs/.../events calls list_namespaced_event, which
        requires the events resource. Without it the endpoint returns 403.
        """
        role = _find_doc(rbac_docs, "Role", "gco-manifest-processor-role")
        resources = _get_all_resources(role)
        resource_names = {r[1] for r in resources}
        assert "events" in resource_names, (
            "manifest-processor Role must grant events for events endpoint"
        )

    def test_manifest_processor_can_read_resource_governance(self, rbac_docs):
        """resourcequotas + limitranges must be readable for GET /api/v1/policy.

        Regression: the 2026-08-26 release-validation run had /api/v1/policy
        return ``cluster_enforcement.gco-jobs = {"status": "unavailable",
        "reason": "403 Forbidden"}``. Three layers govern admission — the
        front-door per-manifest caps, the LimitRange per-container ceiling, and
        the namespace ResourceQuota aggregate — and without these two resources
        the endpoint can only report the first. It reports a caller a job is
        admissible that pod creation then rejects, and the endpoint degrades
        rather than failing, so the gap is silent in the response body.
        """
        role = _find_doc(rbac_docs, "Role", "gco-manifest-processor-role")
        resource_names = {item[1] for item in _get_all_resources(role)}
        missing = {"resourcequotas", "limitranges"} - resource_names
        assert not missing, (
            "manifest-processor Role must grant read on the resource-governance "
            f"objects GET /api/v1/policy reports; missing: {sorted(missing)}"
        )

    def test_resource_governance_access_is_read_only(self, rbac_docs):
        """Nothing at runtime may widen its own guardrail.

        The ceilings come from 04-resource-quotas.yaml, substituted from
        cdk.json at deploy time. A service that could patch its own
        ResourceQuota could lift the cap it is being held to, so the policy
        endpoint's access is deliberately get/list only.
        """
        role = _find_doc(rbac_docs, "Role", "gco-manifest-processor-role")
        governance_rules = [
            rule
            for rule in role["rules"]
            if {"resourcequotas", "limitranges"} & set(rule.get("resources", []))
        ]
        assert governance_rules, "expected a rule covering the governance objects"
        for rule in governance_rules:
            assert set(rule["verbs"]) <= READ_ONLY_VERBS, (
                "resourcequotas/limitranges must be read-only for the manifest "
                f"processor; found verbs {sorted(rule['verbs'])}"
            )
            # Kept on their own rule rather than folded into the pods/services
            # write rule, which would silently grant write on them too.
            assert not set(rule["resources"]) - {"resourcequotas", "limitranges"}, (
                "keep the governance read on its own rule so a future verb "
                "added for another resource cannot widen it"
            )

    def test_manifest_processor_manages_trainjobs(self, rbac_docs):
        """trainer.kubeflow.org/trainjobs needs full lifecycle verbs.

        Regression: the 2026-08-14 shakeout queued a TrainJob through SQS
        and every apply failed with "Create failed: Forbidden" — TrainJob
        is the first CRD submitted through the SQS path (Ray/Volcano
        examples submit direct), so no CRD group had ever been granted to
        the gco-jobs write role. The CLI's generic-manifest REST fallback
        (TrainJob status/logs/delete) rides the same ServiceAccount.
        """
        role = _find_doc(rbac_docs, "Role", "gco-manifest-processor-role")
        trainer_rules = [
            rule for rule in role["rules"] if "trainer.kubeflow.org" in rule.get("apiGroups", [])
        ]
        assert trainer_rules, "gco-jobs write role must grant trainer.kubeflow.org"
        (rule,) = trainer_rules
        assert rule["resources"] == ["trainjobs"]
        assert set(rule["verbs"]) == {
            "create",
            "get",
            "list",
            "watch",
            "patch",
            "update",
            "delete",
        }

    def test_manifest_processor_has_patch_verb_on_workload_resources(self, rbac_docs):
        """patch must be granted on every resource the dynamic client might update.

        Regression: manifest_processor._update_resource() sends an application/
        merge-patch+json body via the dynamic client. Without the patch verb K8s
        returns 403 and the "updated" path fails on resubmission. Queue-processor
        uses the same SA and hits the same path.
        """
        role = _find_doc(rbac_docs, "Role", "gco-manifest-processor-role")
        # Rules the manifest-processor writes to (excluding pods/log and events
        # which are read-only subresources/observability resources).
        write_resource_sets = [
            {"pods", "services", "configmaps"},
            {"deployments", "statefulsets", "daemonsets"},
            {"jobs", "cronjobs"},
        ]
        for rule in role.get("rules", []):
            rule_resources = set(rule.get("resources", []))
            for write_set in write_resource_sets:
                if rule_resources & write_set:
                    assert "patch" in rule.get("verbs", []), (
                        f"Rule for {rule_resources} must include patch verb"
                    )

    def test_manifest_processor_cluster_read_exists(self, rbac_docs):
        """A separate ClusterRole for namespace/CRD reads should exist."""
        role = _find_doc(rbac_docs, "ClusterRole", "gco-manifest-processor-cluster-read")
        assert role is not None, "gco-manifest-processor-cluster-read ClusterRole must exist"

    def test_manifest_processor_cluster_read_is_read_only(self, rbac_docs):
        """The cluster-read role should only have read verbs."""
        role = _find_doc(rbac_docs, "ClusterRole", "gco-manifest-processor-cluster-read")
        all_verbs = _get_all_verbs(role)
        assert all_verbs.issubset(READ_ONLY_VERBS), (
            f"Cluster-read role has non-read verbs: {all_verbs - READ_ONLY_VERBS}"
        )

    def test_manifest_processor_cluster_read_includes_metrics(self, rbac_docs):
        """metrics.k8s.io must be readable for the /metrics job endpoint.

        Regression: GET /api/v1/jobs/.../metrics calls custom_objects.
        get_namespaced_custom_object(group='metrics.k8s.io', ...). Without this
        rule the endpoint returns 403.
        """
        role = _find_doc(rbac_docs, "ClusterRole", "gco-manifest-processor-cluster-read")
        api_groups_seen = set()
        for rule in role.get("rules", []):
            api_groups_seen.update(rule.get("apiGroups", []))
        assert "metrics.k8s.io" in api_groups_seen, (
            "cluster-read role must allow metrics.k8s.io for /metrics endpoint"
        )

    def test_manifest_processor_cluster_read_includes_core_nodes(self, rbac_docs):
        """Core nodes must be readable so the job read surface reports hardware.

        GET /api/v1/jobs/.../{name} and .../pods call core_v1.read_node() to
        resolve the node.kubernetes.io/instance-type and
        karpenter.sh/capacity-type labels of the node a pod landed on. Without
        this rule the read is refused and the instance type comes back None —
        which is the deliberate degraded behavior, but it makes the feature
        permanently inert. Nodes are cluster-scoped, so the rule cannot live in
        the namespaced gco-manifest-processor Role.
        """
        role = _find_doc(rbac_docs, "ClusterRole", "gco-manifest-processor-cluster-read")
        core_node_verbs: set[str] = set()
        for rule in role.get("rules", []):
            if "" in rule.get("apiGroups", []) and "nodes" in rule.get("resources", []):
                core_node_verbs.update(rule.get("verbs", []))
        assert "get" in core_node_verbs, (
            "cluster-read role must grant core nodes 'get' so the job read "
            "surface can report the instance type a pod landed on"
        )

    def test_manifest_processor_cluster_read_lists_cluster_scoped_events(self, rbac_docs):
        """Events outside gco-jobs must be listable so node events reach the job read.

        GET /api/v1/jobs/.../events (gco.services.api_shared._collect_job_events)
        lists the events of the Node and NodeClaim each of a Job's pods ran on
        — where Karpenter / EKS Auto Mode publish SpotInterrupted,
        InstanceTerminating and DisruptionTerminating. Kubernetes records the
        events of cluster-scoped objects in the `default` namespace, which the
        namespaced gco-manifest-processor Role (events in gco-jobs only) cannot
        reach. Without this rule every such read is refused and the endpoint
        degrades to the Job and pod events alone, recording the refusal in
        `scope.errors` — deliberate, but it leaves the interruption cause
        permanently invisible.
        """
        role = _find_doc(rbac_docs, "ClusterRole", "gco-manifest-processor-cluster-read")
        event_verbs: set[str] = set()
        for rule in role.get("rules", []):
            if "" in rule.get("apiGroups", []) and "events" in rule.get("resources", []):
                event_verbs.update(rule.get("verbs", []))
        # The read is a field-selected list; nothing gets one event by name.
        assert event_verbs == {"list"}, (
            "cluster-read role must grant core events 'list' so the "
            "job events read can return Node and NodeClaim events"
        )

    def test_manifest_processor_cluster_read_lists_nodeclaims(self, rbac_docs):
        """Karpenter NodeClaims must be listable to attribute node events to a pod.

        _collect_job_events resolves each node a Job's pods ran on to its
        NodeClaim by listing karpenter.sh/v1 NodeClaims and matching
        status.nodeName (the CRD declares no selectable fields). Read-only:
        the processor never creates or disrupts nodes.
        """
        role = _find_doc(rbac_docs, "ClusterRole", "gco-manifest-processor-cluster-read")
        nodeclaim_verbs: set[str] = set()
        for rule in role.get("rules", []):
            if "karpenter.sh" in rule.get("apiGroups", []) and "nodeclaims" in rule.get(
                "resources", []
            ):
                nodeclaim_verbs.update(rule.get("verbs", []))
        assert nodeclaim_verbs == {"list"}, (
            "cluster-read role must grant karpenter.sh nodeclaims 'list'"
        )
        assert nodeclaim_verbs <= READ_ONLY_VERBS, (
            f"nodeclaims must stay read-only, got {nodeclaim_verbs - READ_ONLY_VERBS}"
        )

    def test_manifest_processor_cluster_read_gets_only_the_sandbox_runtime_classes(self, rbac_docs):
        """Both submission paths read the Nitro Sandbox RuntimeClasses, and only those.

        gco.job_admission.check_sandbox_runtime_class refuses a sandboxed
        manifest on a cluster without its RuntimeClass. Without this rule the
        read is refused and the check fails open, so a Job would again be
        accepted with pods that can never be created. RuntimeClasses are
        cluster-scoped, and ``resourceNames`` keeps the grant to the two
        classes the check asks about.
        """
        from gco.job_admission import SANDBOX_RUNTIME_CLASSES

        role = _find_doc(rbac_docs, "ClusterRole", "gco-manifest-processor-cluster-read")
        rules = [
            rule for rule in role.get("rules", []) if "node.k8s.io" in rule.get("apiGroups", [])
        ]
        assert rules == [
            {
                "apiGroups": ["node.k8s.io"],
                "resources": ["runtimeclasses"],
                "resourceNames": sorted(SANDBOX_RUNTIME_CLASSES),
                "verbs": ["get"],
            }
        ]

    def test_manifest_processor_cluster_read_gets_only_the_gpu_cap_policy(self, rbac_docs):
        """GET /api/v1/policy reads the per-container GPU cap from its admission policy.

        A LimitRange cannot hold the cap (any GPU entry there gives every
        gco-jobs pod a toleration of the GPU node taint), so a
        ValidatingAdmissionPolicy enforces it and carries it in an
        annotation. Without this rule the read is refused and gco-jobs reports
        ``unavailable``. Policies are cluster-scoped, and ``resourceNames``
        keeps the grant to that one policy: the others (the CA issuance and
        tenant write fences) are not the service's to read.
        """
        from gco.resource_governance import CONTAINER_GPU_LIMIT_POLICY

        role = _find_doc(rbac_docs, "ClusterRole", "gco-manifest-processor-cluster-read")
        rules = [
            rule
            for rule in role.get("rules", [])
            if "admissionregistration.k8s.io" in rule.get("apiGroups", [])
        ]
        assert rules == [
            {
                "apiGroups": ["admissionregistration.k8s.io"],
                "resources": ["validatingadmissionpolicies"],
                "resourceNames": [CONTAINER_GPU_LIMIT_POLICY],
                "verbs": ["get"],
            }
        ]


# ─── Submittable kinds ↔ RBAC lockstep ──────────────────────────────


class TestSubmittableKindsRbacLockstep:
    """Every kind users can submit must be writable by the gco-jobs role.

    Generalizes the TrainJob regression above: DEFAULT_ALLOWED_KINDS (the
    shared REST/SQS submission policy in gco/services/manifest_processor.py)
    is the authoritative list of kinds the queue processor creates as
    gco-manifest-processor-sa, so each one needs the full lifecycle verb set
    in gco-manifest-processor-role — create for the initial apply, patch for
    resubmission, get/list/watch for status, delete for teardown. A kind
    added to the allowlist without a matching RBAC rule only fails at
    runtime, as a bare "Forbidden" out of the SQS path (caught live
    2026-08-14, when TrainJob became the first CRD to ride that path). The
    integration:kind:examples-smoke CI job proves the same contract against
    a real apiserver via explicit SubjectAccessReview (SAR) objects — the
    authorization.k8s.io API that answers "may this user <verb> this
    resource?"; this test is the fast local half of that pair.
    """

    REQUIRED_VERBS = {"create", "get", "list", "watch", "patch", "update", "delete"}

    @staticmethod
    def _group_and_plural(kind: str) -> tuple[str, str]:
        """Derive the RBAC (apiGroup, resource-plural) for an allowed kind.

        Groups come from RESOURCE_API_VERSIONS (the exact-GVK allowlist the
        submission endpoints enforce), so the test can never drift from the
        code path it guards. Every current kind pluralizes as lowercase+s;
        a kind that doesn't (e.g. NetworkPolicy -> networkpolicies) will
        fail the coverage assertion loudly and get an explicit entry then.
        """
        from gco.services.manifest_processor import RESOURCE_API_VERSIONS

        api_versions = RESOURCE_API_VERSIONS[kind]
        groups = {av.rsplit("/", 1)[0] if "/" in av else "" for av in api_versions}
        assert len(groups) == 1, f"{kind} maps to multiple API groups: {groups}"
        return groups.pop(), f"{kind.lower()}s"

    def test_every_allowed_kind_has_an_exact_api_version_pin(self):
        """The kind allowlist and the exact-GVK map must cover each other."""
        from gco.services.manifest_processor import (
            DEFAULT_ALLOWED_KINDS,
            RESOURCE_API_VERSIONS,
        )

        assert set(DEFAULT_ALLOWED_KINDS) == set(RESOURCE_API_VERSIONS), (
            "DEFAULT_ALLOWED_KINDS and RESOURCE_API_VERSIONS must stay in "
            "lockstep — a kind present in only one of them is either "
            "unsubmittable or unvalidatable"
        )

    def test_every_allowed_kind_is_writable_by_the_gco_jobs_role(self, rbac_docs):
        """kinds ⊆ RBAC: each submittable kind carries the full verb set."""
        from gco.services.manifest_processor import DEFAULT_ALLOWED_KINDS

        role = _find_doc(rbac_docs, "Role", "gco-manifest-processor-role")
        assert role is not None

        missing: dict[str, set[str]] = {}
        for kind in DEFAULT_ALLOWED_KINDS:
            group, plural = self._group_and_plural(kind)
            granted: set[str] = set()
            for rule in role.get("rules", []):
                rule_groups = rule.get("apiGroups", [""])
                rule_resources = rule.get("resources", [])
                if ("*" in rule_groups or group in rule_groups) and (
                    "*" in rule_resources or plural in rule_resources
                ):
                    granted.update(rule.get("verbs", []))
            if "*" in granted:
                continue
            absent = self.REQUIRED_VERBS - granted
            if absent:
                missing[f"{group or 'core'}/{plural}"] = absent

        assert not missing, (
            "gco-manifest-processor-role is missing verbs for submittable "
            f"kinds (SQS applies will fail Forbidden at runtime): {missing}"
        )


# ─── Inference Monitor Role ─────────────────────────────────────────


class TestInferenceMonitorRole:
    """inference-monitor is scoped to gco-inference."""

    def test_inference_monitor_role_exists(self, rbac_docs):
        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        assert role is not None, "gco-inference-monitor-role Role must exist"

    def test_inference_monitor_is_namespace_scoped(self, rbac_docs):
        """Inference monitor role must be a Role in gco-inference."""
        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        assert role["kind"] == "Role"
        assert role["metadata"]["namespace"] == "gco-inference"

    def test_inference_monitor_covers_expected_resources(self, rbac_docs):
        """Inference monitor has every active and terminal-proof resource."""
        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        resources = _get_all_resources(role)
        expected = {
            ("apps", "deployments"),
            ("apps", "replicasets"),
            ("", "services"),
            ("", "pods"),
            ("", "endpoints"),
            ("discovery.k8s.io", "endpointslices"),
            ("autoscaling", "horizontalpodautoscalers"),
            ("keda.sh", "scaledobjects"),
            ("networking.k8s.io", "ingresses"),
            ("coordination.k8s.io", "leases"),
        }
        assert expected.issubset(resources), f"Missing resources: {expected - resources}"

    def test_inference_monitor_has_no_gateway_api_grant(self, rbac_docs):
        """The monitor calls no Gateway API: no release created a per-endpoint HTTPRoute.

        The shared ``gco-system/gco-routes`` HTTPRoute is the applier's, and
        the monitor's cleanup of HTTPRoutes no release created was removed,
        so a leftover grant would be unused write access.
        """
        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        gateway_rules = [
            rule
            for rule in role["rules"]
            if "gateway.networking.k8s.io" in rule.get("apiGroups", [])
        ]
        assert gateway_rules == []

    @pytest.mark.parametrize(
        ("api_group", "resource"),
        [
            ("apps", "replicasets"),
            ("", "pods"),
            ("", "endpoints"),
            ("discovery.k8s.io", "endpointslices"),
        ],
    )
    def test_generated_children_are_strictly_read_only(self, rbac_docs, api_group, resource):
        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        matching = [
            rule
            for rule in role.get("rules", [])
            if api_group in rule.get("apiGroups", []) and resource in rule.get("resources", [])
        ]
        assert len(matching) == 1
        assert set(matching[0]["verbs"]) == {"get", "list", "watch"}

    def test_inference_monitor_has_patch_verb(self, rbac_docs):
        """patch must be granted on every active resource inference-monitor updates."""
        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        patched_resources = {"deployments", "horizontalpodautoscalers"}
        for rule in role.get("rules", []):
            rule_resources = set(rule.get("resources", []))
            if rule_resources & patched_resources:
                assert "patch" in rule.get("verbs", []), (
                    f"inference-monitor rule for {rule_resources} must include patch"
                )

    def test_inference_monitor_legacy_ingresses_have_exact_cleanup_verbs(self, rbac_docs):
        """Legacy Ingresses allow only deterministic get/provenance-patch/delete."""
        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        rules = [
            rule
            for rule in role["rules"]
            if rule.get("apiGroups") == ["networking.k8s.io"]
            and rule.get("resources") == ["ingresses"]
        ]
        assert len(rules) == 1
        assert rules[0]["verbs"] == ["get", "patch", "delete"]
        assert not ({"create", "list", "update", "watch"} & set(rules[0]["verbs"]))

    def test_inference_monitor_covers_mooncake_resources(self, rbac_docs):
        """Mooncake disaggregated/store endpoints need statefulsets, configmaps, secrets.

        Regression: the role originally granted only deployments/services/
        ingresses/HPAs/leases, so creating the shared mooncake-master
        StatefulSet failed with HTTP 403 and disaggregated endpoints were stuck
        in 'creating'. The monitor also writes the transport ConfigMap and reads
        the proxy admin-key Secret, so both must be grantable too.
        """
        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        resources = _get_all_resources(role)
        resource_names = {r[1] for r in resources}
        for needed in (
            "statefulsets",
            "statefulsets/status",
            "configmaps",
            "secrets",
            "networkpolicies",
        ):
            assert needed in resource_names, (
                f"inference-monitor Role must grant {needed!r} for Mooncake endpoints"
            )

    def test_inference_monitor_secrets_have_generated_key_lifecycle_verbs(self, rbac_docs):
        """Generated Secrets may be created, migrated, read, and deleted only."""
        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        secrets_verbs = set()
        for rule in role.get("rules", []):
            if "secrets" in rule.get("resources", []):
                secrets_verbs.update(rule.get("verbs", []))
        assert secrets_verbs == {"get", "create", "patch", "delete"}
        assert not ({"list", "watch", "update"} & secrets_verbs)

    def test_inference_monitor_statefulsets_support_full_lifecycle(self, rbac_docs):
        """The master StatefulSet is created and its status read each loop, so
        statefulsets must carry create + get (the full write set shares the rule
        with deployments)."""
        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        ss_verbs = set()
        for rule in role.get("rules", []):
            if "statefulsets" in rule.get("resources", []):
                ss_verbs.update(rule.get("verbs", []))
        assert {"create", "get"}.issubset(ss_verbs), (
            f"inference-monitor statefulsets must allow at least create+get, got {ss_verbs}"
        )

    def test_inference_monitor_may_get_and_update_only_its_pre_created_lease(self, rbac_docs):
        """The election Lease is pre-created, so the Role names it and grants no create.

        The applier patches this body onto a live Lease, so it names no
        holder, and it declares the duration the monitor writes at the
        interval its Deployment sets.
        """
        import inspect

        from gco.services.inference_monitor import (
            LEADER_LEASE_NAME,
            LEASE_DURATION_INTERVALS,
            InferenceMonitor,
        )

        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        lease_rules = [rule for rule in role["rules"] if "leases" in rule.get("resources", [])]
        assert lease_rules == [
            {
                "apiGroups": ["coordination.k8s.io"],
                "resources": ["leases"],
                "resourceNames": [LEADER_LEASE_NAME],
                "verbs": ["get", "update"],
            }
        ]
        lease = _find_doc(rbac_docs, "Lease", LEADER_LEASE_NAME)
        assert lease is not None, f"02-rbac.yaml must pre-create the {LEADER_LEASE_NAME} Lease"
        assert lease["metadata"]["namespace"] == role["metadata"]["namespace"]
        assert lease["metadata"]["labels"] == {"app": "inference-monitor", "project": "gco"}
        interval = int(_inference_monitor_env("RECONCILE_INTERVAL_SECONDS"))
        default = inspect.signature(InferenceMonitor).parameters["reconcile_interval"].default
        assert interval == default
        assert lease["spec"] == {"leaseDurationSeconds": interval * LEASE_DURATION_INTERVALS}
        assert lease["spec"]["leaseDurationSeconds"] == 45

    def test_inference_monitor_lease_calls_are_a_read_and_a_replace(self):
        """get and update cover every Lease call the monitor process makes."""
        calls = _kubernetes_api_calls(_process_modules("gco.services.inference_monitor"))
        lease_calls = {method for method, _group, _plural in calls if "lease" in method}
        assert lease_calls == {"read_namespaced_lease", "replace_namespaced_lease"}


# ─── Role Bindings ──────────────────────────────────────────────────


class TestRoleBindings:
    """each role is bound to its corresponding SA only."""

    def test_health_monitor_binding_targets_correct_sa(self, rbac_docs):
        binding = _find_doc(rbac_docs, "ClusterRoleBinding", "gco-health-monitor-binding")
        assert binding is not None
        subjects = binding["subjects"]
        assert len(subjects) == 1
        assert subjects[0]["name"] == "gco-health-monitor-sa"
        assert subjects[0]["namespace"] == "gco-system"

    def test_manifest_processor_binding_targets_correct_sa(self, rbac_docs):
        binding = _find_doc(rbac_docs, "RoleBinding", "gco-manifest-processor-binding")
        assert binding is not None
        subjects = binding["subjects"]
        assert len(subjects) == 1
        assert subjects[0]["name"] == "gco-manifest-processor-sa"
        assert subjects[0]["namespace"] == "gco-system"

    def test_manifest_processor_cluster_read_binding_targets_correct_sa(self, rbac_docs):
        binding = _find_doc(
            rbac_docs, "ClusterRoleBinding", "gco-manifest-processor-cluster-read-binding"
        )
        assert binding is not None
        subjects = binding["subjects"]
        assert len(subjects) == 1
        assert subjects[0]["name"] == "gco-manifest-processor-sa"
        assert subjects[0]["namespace"] == "gco-system"

    def test_inference_monitor_binding_targets_correct_sa(self, rbac_docs):
        binding = _find_doc(rbac_docs, "RoleBinding", "gco-inference-monitor-binding")
        assert binding is not None
        subjects = binding["subjects"]
        assert len(subjects) == 1
        assert subjects[0]["name"] == "gco-inference-monitor-sa"
        assert subjects[0]["namespace"] == "gco-system"

    def test_inference_proxy_has_no_kubernetes_role_binding(self, bindings):
        """The data-plane proxy uses AWS APIs and HTTP, never the Kubernetes API."""
        bound_service_accounts = {
            subject["name"]
            for binding in bindings
            for subject in binding.get("subjects", [])
            if subject.get("kind") == "ServiceAccount"
        }
        assert "gco-inference-proxy-sa" not in bound_service_accounts

    def test_each_binding_references_correct_role(self, rbac_docs):
        """Each binding's roleRef should point to the matching role name."""
        expected_bindings = {
            "gco-health-monitor-binding": "gco-health-monitor-role",
            "gco-manifest-processor-binding": "gco-manifest-processor-role",
            "gco-manifest-processor-cluster-read-binding": "gco-manifest-processor-cluster-read",
            "gco-inference-monitor-binding": "gco-inference-monitor-role",
        }
        for binding_name, role_name in expected_bindings.items():
            binding = None
            for d in rbac_docs:
                if (
                    d.get("kind") in ("RoleBinding", "ClusterRoleBinding")
                    and d["metadata"]["name"] == binding_name
                ):
                    binding = d
                    break
            assert binding is not None, f"Binding {binding_name} not found"
            assert binding["roleRef"]["name"] == role_name, (
                f"Binding {binding_name} references {binding['roleRef']['name']}, "
                f"expected {role_name}"
            )


# ─── No Read+Write on Cluster-Wide Secrets ──────────────────────────


class TestNoClusterWideSecretReadWrite:
    """Verify no single role has both read and write on cluster-wide secrets."""

    def test_no_cluster_role_has_read_and_write_on_secrets(self, rbac_docs):
        """No ClusterRole should grant both read and write access to secrets."""
        cluster_roles = [d for d in rbac_docs if d.get("kind") == "ClusterRole"]
        for role in cluster_roles:
            role_name = role["metadata"]["name"]
            secrets_verbs = set()
            for rule in role.get("rules", []):
                resources = rule.get("resources", [])
                if "secrets" in resources or "*" in resources:
                    secrets_verbs.update(rule.get("verbs", []))
            has_read = bool(secrets_verbs & READ_ONLY_VERBS)
            has_write = bool(secrets_verbs & WRITE_VERBS)
            assert not (has_read and has_write), (
                f"ClusterRole '{role_name}' has both read ({secrets_verbs & READ_ONLY_VERBS}) "
                f"and write ({secrets_verbs & WRITE_VERBS}) on secrets"
            )


# ─── Old ClusterRole Removed ────────────────────────────────────────


class TestOldClusterRoleRemoved:
    """the old gco-cluster-role no longer exists."""

    def test_no_gco_cluster_role(self, rbac_docs):
        """The old over-privileged gco-cluster-role must not be present."""
        role = _find_doc(rbac_docs, "ClusterRole", "gco-cluster-role")
        assert role is None, "Old gco-cluster-role ClusterRole should be removed"

    def test_no_gco_cluster_role_binding(self, rbac_docs):
        """The old gco-cluster-role-binding must not be present."""
        binding = _find_doc(rbac_docs, "ClusterRoleBinding", "gco-cluster-role-binding")
        assert binding is None, "Old gco-cluster-role-binding should be removed"


# ─── Dedicated ServiceAccounts ──────────────────────────────────────


class TestDedicatedServiceAccounts:
    """Verify dedicated SAs exist in gco-system namespace."""

    EXPECTED_SAS = [
        "gco-health-monitor-sa",
        "gco-manifest-processor-sa",
        "gco-inference-monitor-sa",
        "gco-inference-proxy-sa",
    ]

    def test_all_dedicated_sas_exist(self, service_accounts):
        sa_names = {sa["metadata"]["name"] for sa in service_accounts}
        for expected in self.EXPECTED_SAS:
            assert expected in sa_names, f"ServiceAccount {expected} not found"

    def test_all_sas_in_gco_system_namespace(self, service_accounts):
        for sa in service_accounts:
            if sa["metadata"]["name"] in self.EXPECTED_SAS:
                assert sa["metadata"]["namespace"] == "gco-system", (
                    f"SA {sa['metadata']['name']} should be in gco-system, "
                    f"got {sa['metadata']['namespace']}"
                )

    def test_sas_have_project_label(self, service_accounts):
        for sa in service_accounts:
            if sa["metadata"]["name"] in self.EXPECTED_SAS:
                labels = sa["metadata"].get("labels", {})
                assert labels.get("project") == "gco", (
                    f"SA {sa['metadata']['name']} missing project=gco label"
                )

    @pytest.mark.parametrize(
        ("service_account_name", "expected_annotation"),
        [
            ("gco-health-monitor-sa", HEALTH_ROLE_ANNOTATION_VALUE),
            ("gco-manifest-processor-sa", MANIFEST_PROCESSOR_ROLE_ANNOTATION_VALUE),
            ("gco-inference-monitor-sa", INFERENCE_MONITOR_ROLE_ANNOTATION_VALUE),
            ("gco-inference-proxy-sa", INFERENCE_PROXY_ROLE_ANNOTATION_VALUE),
        ],
    )
    def test_service_accounts_use_matching_role_annotations(
        self, service_accounts, service_account_name, expected_annotation
    ):
        service_account = next(
            sa for sa in service_accounts if sa["metadata"]["name"] == service_account_name
        )
        annotations = service_account["metadata"].get("annotations") or {}
        assert annotations[ROLE_ANNOTATION] == expected_annotation

    def test_inference_proxy_disables_default_token_automount(self, service_accounts):
        proxy_sa = next(
            sa for sa in service_accounts if sa["metadata"]["name"] == "gco-inference-proxy-sa"
        )
        assert proxy_sa["automountServiceAccountToken"] is False


# ── IRSA trust policy regression tests ─────────────────────────────────────
#
# Bug history: every IRSA annotation in the kubectl-applier manifests must
# select a CDK role whose trust policy includes that ServiceAccount. The
# health monitor intentionally uses a dedicated role so its narrowly-scoped
# SSM write cannot be assumed by general platform/job workloads, and the
# inference monitor has its own so job and model pods (which share
# gco-service-account) cannot write the inference endpoints table.
#
# These tests pin every trust domain and prevent an annotation from drifting
# away from its matching `service_account_names` list.


# SAs that the CDK stack declares as trusted for each IRSA role. Keep in
# sync with gco/stacks/regional_stack.py::_create_service_account_role.
CDK_TRUSTED_SAS_BY_ANNOTATION = {
    SHARED_ROLE_ANNOTATION_VALUE: frozenset({"gco-service-account"}),
    INFERENCE_MONITOR_ROLE_ANNOTATION_VALUE: frozenset({"gco-inference-monitor-sa"}),
    MANIFEST_PROCESSOR_ROLE_ANNOTATION_VALUE: frozenset({"gco-manifest-processor-sa"}),
    INFERENCE_PROXY_ROLE_ANNOTATION_VALUE: frozenset({"gco-inference-proxy-sa"}),
    HEALTH_ROLE_ANNOTATION_VALUE: frozenset({"gco-health-monitor-sa"}),
}
CDK_TRUSTED_SAS = frozenset().union(*CDK_TRUSTED_SAS_BY_ANNOTATION.values())

MANIFEST_DIR = Path("lambda/kubectl-applier-simple/manifests")


def _load_all_manifest_sas():
    """Yield every ServiceAccount document from the kubectl-applier manifests."""
    for yaml_path in sorted(MANIFEST_DIR.glob("*.yaml")):
        try:
            docs = list(yaml.safe_load_all(yaml_path.read_text()))
        except yaml.YAMLError:
            continue
        for doc in docs:
            if doc is None:
                continue
            if doc.get("kind") == "ServiceAccount":
                yield yaml_path.name, doc


class TestIRSATrustPolicyCoverage:
    """Verify each role-annotated SA is trusted by the matching CDK role."""

    def test_every_annotated_sa_is_trusted_by_matching_cdk_role(self):
        """An annotation must select a known role that trusts that exact SA."""
        missing = []
        for filename, doc in _load_all_manifest_sas():
            annotations = doc["metadata"].get("annotations") or {}
            annotation_value = annotations.get(ROLE_ANNOTATION)
            if not annotation_value:
                continue
            trusted = CDK_TRUSTED_SAS_BY_ANNOTATION.get(annotation_value)
            name = doc["metadata"]["name"]
            if trusted is None or name not in trusted:
                missing.append(f"{filename}: {name} -> {annotation_value}")

        assert not missing, (
            "ServiceAccounts select an IRSA role that does not trust them. Missing:\n  - "
            + "\n  - ".join(sorted(missing))
        )

    def test_cdk_trusted_sas_are_declared_in_manifests(self):
        """Every SA the CDK trusts must actually exist somewhere in the
        manifests, so we don't accumulate stale trust entries."""
        declared = {doc["metadata"]["name"] for _, doc in _load_all_manifest_sas()}
        missing = sorted(CDK_TRUSTED_SAS - declared)
        assert not missing, (
            "CDK trusts these SAs but no kubectl-applier manifest defines them: "
            f"{missing}. Either remove them from regional_stack.py or add the "
            "ServiceAccount manifest."
        )


class TestCdkStackTrustPolicyContract:
    """Load regional_stack.py source and verify the list of trusted SAs
    matches the constant above. This catches any drift introduced by
    editing one side without the other."""

    def test_cdk_source_lists_all_trusted_sas(self):
        source = Path("gco/stacks/regional_stack.py").read_text()
        for sa in CDK_TRUSTED_SAS:
            assert f'"{sa}"' in source, (
                f"Expected {sa!r} to appear as a trusted SA string literal in "
                "regional_stack.py. If you renamed or removed it, update "
                "CDK_TRUSTED_SAS in this test file as well."
            )


# ── Namespace-scoped service invariant tests ────────────────────────────
#
# Bug history: the inference-monitor service called core_v1.read_namespace()
# at startup to check whether the gco-inference namespace existed, but Task
# 10's RBAC restructuring moved the service to a namespace-scoped Role that
# doesn't grant cluster-level namespace access. Result: the monitor
# crash-looped every 10s with HTTP 403 and never reconciled any inference
# endpoint.
#
# These tests codify the invariant that namespace-scoped services must not
# call cluster-scoped Kubernetes APIs.


class TestNamespaceScopedServiceDoesNotUseClusterAPIs:
    """The inference-monitor has a namespace-scoped Role (not ClusterRole)
    and therefore must not call cluster-scoped Kubernetes APIs."""

    FORBIDDEN_CALLS = (
        "core_v1.read_namespace(",
        "core_v1.create_namespace(",
        "core_v1.list_namespace(",
        "core_v1.patch_namespace(",
        "core_v1.delete_namespace(",
    )

    def test_inference_monitor_does_not_call_cluster_namespace_apis(self):
        source = Path("gco/services/inference_monitor.py").read_text()
        offenders = [call for call in self.FORBIDDEN_CALLS if call in source]
        assert not offenders, (
            "inference_monitor.py must not call cluster-scoped namespace APIs "
            "because the gco-inference-monitor-role is a namespace-scoped Role "
            "(02-rbac.yaml). These calls will fail with HTTP 403 at runtime and "
            f"crash-loop the monitor: {offenders}"
        )


# ─── RBAC derived from the code ────────────────────────────────────

#: The RBAC each Kubernetes API call of the health-monitor process needs, as
#: (scope, apiGroup, resource, verb). Custom-object calls are keyed with the
#: group and plural they pass.
HEALTH_MONITOR_CALL_GRANTS = {
    ("list_node", None, None): {("cluster", "", "nodes", "list")},
    ("list_pod_for_all_namespaces", None, None): {("cluster", "", "pods", "list")},
    # The webhook dispatcher lists, then watches from the list's resourceVersion.
    ("list_job_for_all_namespaces", None, None): {
        ("cluster", "batch", "jobs", "list"),
        ("cluster", "batch", "jobs", "watch"),
    },
    ("list_cluster_custom_object", "metrics.k8s.io", "nodes"): {
        ("cluster", "metrics.k8s.io", "nodes", "list")
    },
    ("get_namespaced_custom_object", "gateway.networking.k8s.io", "gateways"): {
        ("gco-system", "gateway.networking.k8s.io", "gateways", "get")
    },
    ("read_namespaced_lease", None, None): {("gco-system", "coordination.k8s.io", "leases", "get")},
    ("replace_namespaced_lease", None, None): {
        ("gco-system", "coordination.k8s.io", "leases", "update")
    },
    # Keep-warm idle records: read the Job, then patch its metadata, in the
    # job namespace the Role gco-health-monitor-keep-warm-records covers.
    ("read_namespaced_job", None, None): {("gco-jobs", "batch", "jobs", "get")},
    ("patch_namespaced_job", None, None): {("gco-jobs", "batch", "jobs", "patch")},
}

_API_CLASSES = (
    k8s_client.AdmissionregistrationV1Api,
    k8s_client.AppsV1Api,
    k8s_client.BatchV1Api,
    k8s_client.CoordinationV1Api,
    k8s_client.CoreV1Api,
    k8s_client.CustomObjectsApi,
    k8s_client.NetworkingV1Api,
    k8s_client.NodeV1Api,
    k8s_client.RbacAuthorizationV1Api,
    k8s_client.AutoscalingV2Api,
)
_API_METHODS = frozenset(
    name
    for api in _API_CLASSES
    for name in dir(api)
    if name.split("_", 1)[0] in {"list", "read", "get", "create", "patch", "replace", "delete"}
)


def _module_file(module: str) -> Path | None:
    path = Path(*module.split("."))
    for candidate in (path.with_suffix(".py"), path / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _process_modules(
    entry: str, *, imported_for_helpers: frozenset[str] = frozenset()
) -> list[str]:
    """``entry`` and every gco module it imports, transitively, as dotted names.

    ``imported_for_helpers`` names modules a process imports only for helpers
    while their Kubernetes calls run in another process; they are not followed.
    """
    seen: set[str] = set()
    pending = [entry]
    while pending:
        module = pending.pop()
        path = _module_file(module)
        if module in seen or module in imported_for_helpers or path is None:
            continue
        seen.add(module)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("gco"):
                pending.append(str(node.module))
                pending.extend(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names if alias.name.startswith("gco"))
    return sorted(seen)


def _keyword_value(call: ast.Call, name: str, module: object) -> str | None:
    """A keyword argument's value: a literal, or a module-level constant."""
    for keyword in call.keywords:
        if keyword.arg != name:
            continue
        if isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
        if isinstance(keyword.value, ast.Name):
            return str(getattr(module, keyword.value.id))
    return None


def _kubernetes_api_calls(modules) -> set[tuple[str, str | None, str | None]]:
    """``(method, group, plural)`` for every Kubernetes client method the modules reference.

    A method passed by reference (``asyncio.to_thread(api.list_node, ...)``,
    ``watch.stream(api.list_job_for_all_namespaces, ...)``) counts like a call.
    """
    found: set[tuple[str, str | None, str | None]] = set()
    for name in modules:
        tree = ast.parse(Path(str(_module_file(name))).read_text(encoding="utf-8"))
        # The names are the service modules this test lists, never external input.
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        module = importlib.import_module(name)
        custom_calls = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Attribute) and target.attr.endswith("_custom_object"):
                    custom_calls[id(target)] = node
                # asyncio.to_thread(api.method, group=..., plural=...)
                for argument in node.args[:1]:
                    if isinstance(argument, ast.Attribute) and argument.attr.endswith(
                        "_custom_object"
                    ):
                        custom_calls[id(argument)] = node
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _API_METHODS:
                if node.attr.endswith("_custom_object"):
                    call = custom_calls[id(node)]
                    found.add(
                        (
                            node.attr,
                            _keyword_value(call, "group", module),
                            _keyword_value(call, "plural", module),
                        )
                    )
                else:
                    found.add((node.attr, None, None))
    return found


def _inference_monitor_env(name: str) -> str:
    """The value the shipped inference-monitor Deployment gives environment variable ``name``."""
    import re

    path = RBAC_MANIFEST_PATH.parent / "32-inference-monitor.yaml"
    text = re.sub(r"\{\{[A-Z0-9_]+\}\}", "placeholder", path.read_text(encoding="utf-8"))
    values = [
        str(variable["value"])
        for doc in yaml.safe_load_all(text)
        if isinstance(doc, dict) and doc.get("kind") == "Deployment"
        for container in doc["spec"]["template"]["spec"]["containers"]
        for variable in container.get("env", [])
        if variable["name"] == name
    ]
    assert len(values) == 1, f"32-inference-monitor.yaml should set {name} once: {values}"
    return values[0]


def _grants(role: dict) -> set[tuple[str, str, str]]:
    """Every (apiGroup, resource, verb) a Role or ClusterRole grants."""
    return {
        (group, resource, verb)
        for rule in role.get("rules", [])
        for group in rule.get("apiGroups", [""])
        for resource in rule.get("resources", [])
        for verb in rule.get("verbs", [])
    }


#: The RBAC each Kubernetes API call of the manifest-processor service account
#: needs. Two processes run as it: the REST service (``python -m
#: gco.services.manifest_api``, its routes and the central queue worker) and
#: the SQS queue processor. "cluster" grants come from
#: gco-manifest-processor-cluster-read, "gco-jobs" grants from the gco-jobs
#: Roles (gco-manifest-processor-role, and the Slurm token Role when Slurm is on).
MANIFEST_PROCESSOR_CALL_GRANTS = {
    # /readyz: one namespace list proves the API server answers.
    ("list_namespace", None, None): {("cluster", "", "namespaces", "list")},
    # Job and pod detail: the node a pod landed on.
    ("read_node", None, None): {("cluster", "", "nodes", "get")},
    # Job events in the job's namespace, Node and NodeClaim events in default.
    ("list_namespaced_event", None, None): {
        ("gco-jobs", "", "events", "list"),
        ("cluster", "", "events", "list"),
    },
    ("list_cluster_custom_object", "karpenter.sh", "nodeclaims"): {
        ("cluster", "karpenter.sh", "nodeclaims", "list")
    },
    ("get_namespaced_custom_object", "metrics.k8s.io", "pods"): {
        ("cluster", "metrics.k8s.io", "pods", "get")
    },
    ("read_runtime_class", None, None): {("cluster", "node.k8s.io", "runtimeclasses", "get")},
    # GET /api/v1/policy: the per-container GPU cap, from its admission policy.
    ("read_validating_admission_policy", None, None): {
        ("cluster", "admissionregistration.k8s.io", "validatingadmissionpolicies", "get")
    },
    ("list_namespaced_job", None, None): {("gco-jobs", "batch", "jobs", "list")},
    ("read_namespaced_job", None, None): {("gco-jobs", "batch", "jobs", "get")},
    ("delete_namespaced_job", None, None): {("gco-jobs", "batch", "jobs", "delete")},
    ("list_namespaced_pod", None, None): {("gco-jobs", "", "pods", "list")},
    ("read_namespaced_pod", None, None): {("gco-jobs", "", "pods", "get")},
    ("read_namespaced_pod_log", None, None): {("gco-jobs", "", "pods/log", "get")},
    ("list_namespaced_limit_range", None, None): {("gco-jobs", "", "limitranges", "list")},
    ("list_namespaced_resource_quota", None, None): {("gco-jobs", "", "resourcequotas", "list")},
    # The Slurm REST client's JWT (post-helm-slurm-api-access.yaml).
    ("read_namespaced_secret", None, None): {("gco-jobs", "", "secrets", "get")},
}

#: The REST service imports the webhook dispatcher for its URL validation only;
#: the dispatcher and its Lease run in the health-monitor pod.
_MANIFEST_API_HELPER_IMPORTS = frozenset({"gco.services.webhook_dispatcher"})


def _manifest_processor_calls() -> set[tuple[str, str | None, str | None]]:
    rest = _process_modules(
        "gco.services.manifest_api", imported_for_helpers=_MANIFEST_API_HELPER_IMPORTS
    )
    return _kubernetes_api_calls(rest) | _kubernetes_api_calls(
        _process_modules("gco.services.queue_processor")
    )


def _manifest_processor_gco_jobs_roles() -> list[dict]:
    """The gco-jobs Roles bound to gco-manifest-processor-sa, Slurm's included."""
    import re

    manifests = RBAC_MANIFEST_PATH.parent
    roles = []
    for path in (RBAC_MANIFEST_PATH, manifests / "post-helm-slurm-api-access.yaml"):
        text = re.sub(r"\{\{[A-Z0-9_]+\}\}", "placeholder", path.read_text(encoding="utf-8"))
        docs = [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]
        bound = {
            doc["roleRef"]["name"]
            for doc in docs
            if doc["kind"] == "RoleBinding"
            and doc["metadata"]["namespace"] == "gco-jobs"
            and any(s.get("name") == "gco-manifest-processor-sa" for s in doc["subjects"])
        }
        roles.extend(
            doc
            for doc in docs
            if doc["kind"] == "Role"
            and doc["metadata"]["namespace"] == "gco-jobs"
            and doc["metadata"]["name"] in bound
        )
    return roles


class TestManifestProcessorRbacMatchesItsCalls:
    """The manifest-processor service account holds what its two processes call."""

    def test_every_call_has_a_known_grant(self):
        calls = _manifest_processor_calls()
        assert calls == set(MANIFEST_PROCESSOR_CALL_GRANTS), (
            "update MANIFEST_PROCESSOR_CALL_GRANTS and the RBAC for these calls: "
            f"{sorted(calls ^ set(MANIFEST_PROCESSOR_CALL_GRANTS), key=str)}"
        )

    def test_the_cluster_read_role_grants_exactly_the_cluster_scoped_calls(self, rbac_docs):
        """No wildcard CRD groups: nothing the service runs reads Kueue, Ray or Volcano."""
        needed = {
            grant[1:]
            for call in _manifest_processor_calls()
            for grant in MANIFEST_PROCESSOR_CALL_GRANTS[call]
            if grant[0] == "cluster"
        }
        role = _find_doc(rbac_docs, "ClusterRole", "gco-manifest-processor-cluster-read")
        assert _grants(role) == needed

    def test_the_gco_jobs_roles_cover_every_namespaced_call(self):
        needed = {
            grant[1:]
            for call in _manifest_processor_calls()
            for grant in MANIFEST_PROCESSOR_CALL_GRANTS[call]
            if grant[0] == "gco-jobs"
        }
        granted = set().union(*(_grants(role) for role in _manifest_processor_gco_jobs_roles()))
        assert needed <= granted, sorted(needed - granted)

    def test_the_gco_jobs_role_names_only_resources_something_uses(self, rbac_docs):
        """Each resource is a default submittable kind or the target of a typed call.

        Submissions reach the dynamic client for the kinds in
        job_validation_policy.allowed_kinds, by default
        gco.job_admission.DEFAULT_ALLOWED_KINDS, which also pins their
        apiVersions for the status and delete endpoints. PersistentVolumeClaim
        is neither, so the Role grants no write on it.
        """
        from gco.job_admission import DEFAULT_ALLOWED_KINDS, RESOURCE_API_VERSIONS

        assert set(RESOURCE_API_VERSIONS) == set(DEFAULT_ALLOWED_KINDS)
        submittable = {kind.lower() + "s" for kind in DEFAULT_ALLOWED_KINDS}
        typed = {
            grant[2]
            for grants in MANIFEST_PROCESSOR_CALL_GRANTS.values()
            for grant in grants
            if grant[0] == "gco-jobs"
        }
        role = _find_doc(rbac_docs, "Role", "gco-manifest-processor-role")
        resources = {resource for _group, resource, _verb in _grants(role)}
        assert resources <= submittable | typed, sorted(resources - submittable - typed)
        assert submittable <= resources, sorted(submittable - resources)
