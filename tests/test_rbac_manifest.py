"""
Tests for the RBAC manifest at lambda/kubectl-applier-simple/manifests/02-rbac.yaml.

Asserts least-privilege separation across the per-service service
accounts: gco-health-monitor-role is cluster-wide but read-only (only
get/list/watch, no write verbs), manifest processor is scoped to the
gco-jobs namespace, inference monitor is scoped to gco-inference,
each Role/ClusterRole is bound only to its own SA, no role mixes
read+write on cluster-wide secrets, the old gco-cluster-role is gone,
and dedicated SAs live in gco-system. Fixtures load and classify the
YAML docs so each test stays focused on one property.
"""

from pathlib import Path

import pytest
import yaml

RBAC_MANIFEST_PATH = Path("lambda/kubectl-applier-simple/manifests/02-rbac.yaml")

READ_ONLY_VERBS = {"get", "list", "watch"}
WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection"}


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

    def test_health_monitor_covers_expected_resources(self, rbac_docs):
        """Health monitor should cover pods, nodes, deployments, services, events, jobs, metrics."""
        role = _find_doc(rbac_docs, "ClusterRole", "gco-health-monitor-role")
        resources = _get_all_resources(role)
        resource_names = {r[1] for r in resources}
        expected = {"pods", "nodes", "deployments", "services", "events", "jobs"}
        assert expected.issubset(resource_names), f"Missing resources: {expected - resource_names}"

    def test_health_monitor_self_healing_is_exactly_scoped(self, rbac_docs):
        """Ingress repair and election access stay in gco-system and named resources."""
        role = _find_doc(rbac_docs, "Role", "gco-health-monitor-self-healing")
        assert role is not None
        assert role["metadata"]["namespace"] == "gco-system"

        rules = {
            (tuple(rule["apiGroups"]), tuple(rule["resources"])): rule for rule in role["rules"]
        }
        ingress_rule = rules[(("networking.k8s.io",), ("ingresses",))]
        assert ingress_rule["verbs"] == ["get"]
        assert ingress_rule["resourceNames"] == ["gco-ingress"]

        lease_rule = rules[(("coordination.k8s.io",), ("leases",))]
        assert set(lease_rule["verbs"]) == {"get", "update"}
        assert lease_rule["resourceNames"] == ["gco-health-monitor-alb-sync"]
        assert "create" not in lease_rule["verbs"]

        lease = _find_doc(rbac_docs, "Lease", "gco-health-monitor-alb-sync")
        assert lease is not None
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
            {"pods", "services", "configmaps", "persistentvolumeclaims"},
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
        """Inference monitor should manage deployments, services, ingresses, HPAs, leases."""
        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        resources = _get_all_resources(role)
        resource_names = {r[1] for r in resources}
        expected = {"deployments", "services", "ingresses", "horizontalpodautoscalers", "leases"}
        assert expected.issubset(resource_names), f"Missing resources: {expected - resource_names}"

    def test_inference_monitor_has_patch_verb(self, rbac_docs):
        """patch must be granted on every resource inference-monitor updates.

        Regression: inference_monitor calls apps_v1.patch_namespaced_deployment,
        networking_v1.patch_namespaced_ingress, and autoscaling_v2.
        patch_namespaced_horizontal_pod_autoscaler. Without patch these 403.
        """
        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        patched_resources = {"deployments", "ingresses", "horizontalpodautoscalers"}
        for rule in role.get("rules", []):
            rule_resources = set(rule.get("resources", []))
            if rule_resources & patched_resources:
                assert "patch" in rule.get("verbs", []), (
                    f"inference-monitor rule for {rule_resources} must include patch"
                )

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

    def test_inference_monitor_secrets_limited_to_get_and_create(self, rbac_docs):
        """The monitor reads named admin-key Secrets and creates auto-managed ones.

        It reads a user-supplied proxy admin-key Secret by reference and, when an
        endpoint names none, creates a per-endpoint ``{name}-admin`` Secret with a
        generated key. It never updates or deletes Secrets, so access is limited
        to ``get`` + ``create`` — keeping the blast radius minimal.
        """
        role = _find_doc(rbac_docs, "Role", "gco-inference-monitor-role")
        secrets_verbs = set()
        for rule in role.get("rules", []):
            if "secrets" in rule.get("resources", []):
                secrets_verbs.update(rule.get("verbs", []))
        assert secrets_verbs == {"get", "create"}, (
            f"inference-monitor secrets access must be {{'get', 'create'}}, got {secrets_verbs}"
        )

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

    def test_health_monitor_uses_dedicated_role_annotation(self, service_accounts):
        health_sa = next(
            sa for sa in service_accounts if sa["metadata"]["name"] == "gco-health-monitor-sa"
        )
        annotations = health_sa["metadata"].get("annotations") or {}
        assert annotations[ROLE_ANNOTATION] == HEALTH_ROLE_ANNOTATION_VALUE


# ── IRSA trust policy regression tests ─────────────────────────────────────
#
# Bug history: every IRSA annotation in the kubectl-applier manifests must
# select a CDK role whose trust policy includes that ServiceAccount. The
# health monitor intentionally uses a dedicated role so its narrowly-scoped
# SSM write cannot be assumed by general platform/job workloads.
#
# These tests pin both trust domains and prevent either annotation from
# drifting away from its matching `service_account_names` list.


ROLE_ANNOTATION = "eks.amazonaws.com/role-arn"
SHARED_ROLE_ANNOTATION_VALUE = "{{SERVICE_ACCOUNT_ROLE_ARN}}"
HEALTH_ROLE_ANNOTATION_VALUE = "{{HEALTH_MONITOR_ROLE_ARN}}"

# SAs that the CDK stack declares as trusted for each IRSA role. Keep in
# sync with gco/stacks/regional_stack.py::_create_service_account_role.
CDK_TRUSTED_SAS_BY_ANNOTATION = {
    SHARED_ROLE_ANNOTATION_VALUE: frozenset(
        {
            "gco-service-account",
            "gco-manifest-processor-sa",
            "gco-inference-monitor-sa",
        }
    ),
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
