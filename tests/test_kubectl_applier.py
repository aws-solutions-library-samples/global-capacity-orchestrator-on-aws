"""
Tests for the kubectl-applier Lambda (lambda/kubectl-applier-simple/handler.py).

Covers the manifest-application state machine that bootstraps the
cluster: the two-phase apply that defers `post-helm-*.yaml` files to
after Helm runs, skipping of placeholder manifests for optional
features, PV smart-recreate (skip when unchanged, delete+recreate
when volumeHandle changes), credential verification before any
mutation, and the AllowedKinds allowlist. The handler_module fixture
reloads the handler with sys.modules cleanup so each test runs
against a fresh import.
"""

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml


@pytest.fixture
def handler_module():
    """Import the kubectl-applier handler with mocked dependencies."""
    handler_path = str(Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple")
    sys.path.insert(0, handler_path)
    try:
        # Remove cached module if present
        sys.modules.pop("handler", None)
        import handler

        yield handler
    finally:
        sys.path.pop(0)
        sys.modules.pop("handler", None)


@pytest.fixture(autouse=True)
def _neutralize_legacy_sweep(request):
    """Empty the legacy-removal inventory for every test by default.

    ``apply_manifests`` base passes unconditionally sweep
    ``_LEGACY_REMOVED_RESOURCES``; with an empty inventory
    ``_delete_exact_resources`` returns before building a Kubernetes client,
    so the many base-pass tests that mock ``handler.client`` stay isolated.
    ``TestLegacyRemovedResources`` opts out to exercise the real inventory.
    """
    owner = type(request.instance).__name__ if request.instance is not None else ""
    if "handler_module" not in request.fixturenames or owner == "TestLegacyRemovedResources":
        yield
        return
    handler = request.getfixturevalue("handler_module")
    with patch.object(handler, "_LEGACY_REMOVED_RESOURCES", ()):
        yield


class TestPostHelmDeferral:
    """Tests for the post-helm- filename prefix convention."""

    def test_main_pass_skips_post_helm_files(self, handler_module, tmp_path):
        """Main pass (post_helm=False) skips files prefixed with post-helm-."""
        # Create a post-helm manifest
        (tmp_path / "post-helm-keda.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "keda.sh/v1alpha1",
                    "kind": "ScaledJob",
                    "metadata": {"name": "test", "namespace": "default"},
                }
            )
        )
        # Create a normal manifest
        (tmp_path / "00-ns.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": "test-ns"},
                }
            )
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_v1 = MagicMock()
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()

            result = handler_module.apply_manifests("test-cluster", "us-east-1", str(tmp_path), {})

        # Namespace should be applied, ScaledJob should be deferred
        assert result["AppliedCount"] == 1
        assert "post-helm-keda.yaml:deferred-to-post-helm" in result["Skipped"]

    def test_post_helm_pass_only_applies_post_helm_files(self, handler_module, tmp_path):
        """Post-helm pass (post_helm=True) only applies post-helm- files."""
        (tmp_path / "post-helm-keda.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "keda.sh/v1alpha1",
                    "kind": "ScaledJob",
                    "metadata": {"name": "test", "namespace": "default"},
                    "spec": {
                        "jobTargetRef": {"template": {"spec": {"containers": [{"name": "test"}]}}}
                    },
                }
            )
        )
        (tmp_path / "00-ns.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": "test-ns"},
                }
            )
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_v1 = MagicMock()
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_custom = MagicMock()
            mock_client.CustomObjectsApi.return_value = mock_custom

            result = handler_module.apply_manifests(
                "test-cluster", "us-east-1", str(tmp_path), {}, post_helm=True
            )

        # Only the ScaledJob should be applied, Namespace skipped
        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0


class TestPlaceholderSkipping:
    """Tests for skipping manifests with unreplaced template variables."""

    def test_skips_files_with_unreplaced_placeholders(self, handler_module, tmp_path):
        """Files with {{PLACEHOLDER}} values are skipped (feature not enabled)."""
        (tmp_path / "20-fsx.yaml").write_text(
            "apiVersion: v1\nkind: PersistentVolume\nmetadata:\n  name: test\n"
            "spec:\n  csi:\n    volumeHandle: '{{FSX_FILE_SYSTEM_ID}}'\n"
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch.object(
                handler_module,
                "_prune_disabled_feature",
                return_value={"pruned": [], "failed": []},
            ),
            patch("handler.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()

            result = handler_module.apply_manifests("test-cluster", "us-east-1", str(tmp_path), {})

        assert result["AppliedCount"] == 0
        assert "20-fsx.yaml:unreplaced-placeholders" in result["Skipped"]

    def test_applies_files_after_placeholder_replacement(self, handler_module, tmp_path):
        """Files with placeholders are applied after replacement."""
        (tmp_path / "00-ns.yaml").write_text(
            "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: '{{NS_NAME}}'\n"
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_v1 = MagicMock()
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()

            result = handler_module.apply_manifests(
                "test-cluster",
                "us-east-1",
                str(tmp_path),
                {"{{NS_NAME}}": "my-namespace"},
            )

        assert result["AppliedCount"] == 1

    def test_lowercase_double_brace_content_is_not_skipped(self, handler_module, tmp_path):
        """Grafana dashboard legend tokens ({{gpu}}, {{service}}) are NOT
        feature-gate placeholders and must survive substitution untouched.

        Regression guard for the observability dashboard ConfigMaps: the old
        blanket ``"{{" in content and "}}" in content`` check skipped any file
        containing double braces, which would have silently dropped every
        dashboard even when observability is enabled. Only UPPER_SNAKE tokens
        gate a file now.
        """
        (tmp_path / "post-helm-grafana-dashboards.yaml").write_text(
            "apiVersion: v1\n"
            "kind: ConfigMap\n"
            "metadata:\n"
            "  name: gco-dashboard-gpu\n"
            "  namespace: monitoring\n"
            "data:\n"
            '  d.json: \'{"targets":[{"legendFormat":"{{Hostname}} gpu{{gpu}}"}]}\'\n'
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()

            result = handler_module.apply_manifests(
                "test-cluster", "us-east-1", str(tmp_path), {}, post_helm=True
            )

        assert result["AppliedCount"] == 1
        assert "unreplaced-placeholders" not in result["Skipped"]

    def test_uppercase_gate_still_skips_even_beside_lowercase_tokens(
        self, handler_module, tmp_path
    ):
        """A file mixing an unresolved UPPER_SNAKE gate with lowercase legend
        tokens is still skipped — the gate placeholder wins.

        This is how the dashboards ConfigMap is turned off when observability
        is disabled: ``{{CLUSTER_OBSERVABILITY_ENABLED}}`` stays unresolved and
        gates the whole file, while the ``{{gpu}}`` legend token alone would
        not have.
        """
        (tmp_path / "post-helm-grafana-dashboards.yaml").write_text(
            "apiVersion: v1\n"
            "kind: ConfigMap\n"
            "metadata:\n"
            "  name: gco-dashboard-gpu\n"
            "  namespace: monitoring\n"
            "  annotations:\n"
            '    gco.io/cluster-observability-enabled: "{{CLUSTER_OBSERVABILITY_ENABLED}}"\n'
            "data:\n"
            '  d.json: \'{"targets":[{"legendFormat":"{{gpu}}"}]}\'\n'
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch.object(
                handler_module,
                "_prune_disabled_feature",
                return_value={"pruned": [], "failed": []},
            ),
            patch("handler.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()

            result = handler_module.apply_manifests(
                "test-cluster", "us-east-1", str(tmp_path), {}, post_helm=True
            )

        assert result["AppliedCount"] == 0
        assert "post-helm-grafana-dashboards.yaml:unreplaced-placeholders" in result["Skipped"]


class TestDisabledFeaturePruning:
    """Disabled optional features converge by pruning only owned resources."""

    def test_prunes_only_exact_queue_processor_scaled_job(self, handler_module):
        mock_resource = MagicMock()
        mock_dynamic = MagicMock()
        mock_dynamic.resources.get.return_value = mock_resource
        delete_options = MagicMock()

        with (
            patch.object(
                handler_module.dynamic,
                "DynamicClient",
                return_value=mock_dynamic,
            ),
            patch.object(handler_module.client, "ApiClient", return_value=MagicMock()),
            patch.object(
                handler_module.client,
                "V1DeleteOptions",
                return_value=delete_options,
            ),
        ):
            result = handler_module._prune_disabled_feature(
                "{{QUEUE_PROCESSOR_IMAGE}}", post_helm=True
            )

        mock_dynamic.resources.get.assert_called_once_with(
            api_version="keda.sh/v1alpha1", kind="ScaledJob"
        )
        mock_resource.delete.assert_called_once_with(
            name="sqs-queue-processor",
            namespace="gco-system",
            body=delete_options,
        )
        assert result == {
            "pruned": ["keda.sh/v1alpha1/ScaledJob/gco-system/sqs-queue-processor"],
            "failed": [],
        }

    def test_missing_resource_and_missing_crd_are_noops(self, handler_module):
        from kubernetes.client.rest import ApiException

        missing_resource = MagicMock()
        missing_resource.delete.side_effect = ApiException(status=404, reason="Not Found")
        missing_dynamic = MagicMock()
        missing_dynamic.resources.get.return_value = missing_resource

        with (
            patch.object(
                handler_module.dynamic,
                "DynamicClient",
                return_value=missing_dynamic,
            ),
            patch.object(handler_module.client, "ApiClient", return_value=MagicMock()),
            patch.object(handler_module.client, "V1DeleteOptions", return_value=MagicMock()),
        ):
            result = handler_module._prune_disabled_feature(
                "{{QUEUE_PROCESSOR_IMAGE}}", post_helm=True
            )

        assert result == {"pruned": [], "failed": []}

        missing_dynamic.resources.get.side_effect = handler_module.ResourceNotFoundError(
            "ScaledJob CRD is absent"
        )
        with (
            patch.object(
                handler_module.dynamic,
                "DynamicClient",
                return_value=missing_dynamic,
            ),
            patch.object(handler_module.client, "ApiClient", return_value=MagicMock()),
            patch.object(handler_module.client, "V1DeleteOptions", return_value=MagicMock()),
        ):
            result = handler_module._prune_disabled_feature(
                "{{QUEUE_PROCESSOR_IMAGE}}", post_helm=True
            )

        assert result == {"pruned": [], "failed": []}

    def test_non_404_prune_failure_is_reported(self, handler_module):
        from kubernetes.client.rest import ApiException

        mock_resource = MagicMock()
        mock_resource.delete.side_effect = ApiException(status=403, reason="Forbidden")
        mock_dynamic = MagicMock()
        mock_dynamic.resources.get.return_value = mock_resource

        with (
            patch.object(
                handler_module.dynamic,
                "DynamicClient",
                return_value=mock_dynamic,
            ),
            patch.object(handler_module.client, "ApiClient", return_value=MagicMock()),
            patch.object(handler_module.client, "V1DeleteOptions", return_value=MagicMock()),
        ):
            result = handler_module._prune_disabled_feature(
                "{{QUEUE_PROCESSOR_IMAGE}}", post_helm=True
            )

        assert result["pruned"] == []
        assert result["failed"] == [
            "keda.sh/v1alpha1/ScaledJob/gco-system/sqs-queue-processor:403:Forbidden"
        ]

    def test_reconciles_each_gate_once_and_surfaces_prune_results(self, handler_module, tmp_path):
        for filename in ("post-helm-queue-a.yaml", "post-helm-queue-b.yaml"):
            (tmp_path / filename).write_text(
                "apiVersion: v1\n"
                "kind: ConfigMap\n"
                "metadata:\n"
                "  name: disabled-queue\n"
                "  annotations:\n"
                '    image: "{{QUEUE_PROCESSOR_IMAGE}}"\n'
            )

        pruned = "keda.sh/v1alpha1/ScaledJob/gco-system/sqs-queue-processor"
        failure = f"{pruned}:403:Forbidden"
        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch.object(
                handler_module,
                "_prune_disabled_feature",
                return_value={"pruned": [pruned], "failed": [failure]},
            ) as mock_prune,
            patch("handler.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()

            result = handler_module.apply_manifests(
                "test-cluster",
                "us-east-1",
                str(tmp_path),
                {},
                post_helm=True,
            )

        mock_prune.assert_called_once_with("{{QUEUE_PROCESSOR_IMAGE}}", True)
        assert result["PrunedCount"] == 1
        assert result["Pruned"] == pruned
        assert result["PruneFailures"] == failure
        assert result["FailedCount"] == 1
        assert result["Failed"] == f"prune:{failure}"


class TestLegacyRemovedResources:
    """Objects shipped by earlier releases are swept from upgraded clusters."""

    def test_inventory_targets_the_removed_nvidia_device_plugin(self, handler_module):
        # EKS Auto Mode provides the NVIDIA device plugin built into the node;
        # the community DaemonSet GCO used to ship crash-loops there (NVML
        # ERROR_LIBRARY_NOT_FOUND) and must be deleted from upgraded clusters.
        assert (
            "apps/v1",
            "DaemonSet",
            "kube-system",
            "nvidia-device-plugin-daemonset",
        ) in handler_module._LEGACY_REMOVED_RESOURCES

    def test_no_legacy_entry_still_ships_as_a_manifest(self, handler_module):
        manifests_dir = (
            Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple" / "manifests"
        )
        shipped = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(manifests_dir.glob("*.yaml"))
        )
        for _, _, _, name in handler_module._LEGACY_REMOVED_RESOURCES:
            assert name not in shipped, f"{name} is swept as legacy but still shipped"

    def test_sweep_deletes_exact_resource(self, handler_module):
        mock_resource = MagicMock()
        mock_dynamic = MagicMock()
        mock_dynamic.resources.get.return_value = mock_resource
        delete_options = MagicMock()

        with (
            patch.object(handler_module.dynamic, "DynamicClient", return_value=mock_dynamic),
            patch.object(handler_module.client, "ApiClient", return_value=MagicMock()),
            patch.object(handler_module.client, "V1DeleteOptions", return_value=delete_options),
        ):
            result = handler_module._prune_legacy_removed_resources()

        mock_dynamic.resources.get.assert_called_once_with(api_version="apps/v1", kind="DaemonSet")
        mock_resource.delete.assert_called_once_with(
            name="nvidia-device-plugin-daemonset",
            namespace="kube-system",
            body=delete_options,
        )
        assert result == {
            "pruned": ["apps/v1/DaemonSet/kube-system/nvidia-device-plugin-daemonset"],
            "failed": [],
        }

    def test_sweep_missing_resource_is_noop(self, handler_module):
        from kubernetes.client.rest import ApiException

        missing_resource = MagicMock()
        missing_resource.delete.side_effect = ApiException(status=404, reason="Not Found")
        mock_dynamic = MagicMock()
        mock_dynamic.resources.get.return_value = missing_resource

        with (
            patch.object(handler_module.dynamic, "DynamicClient", return_value=mock_dynamic),
            patch.object(handler_module.client, "ApiClient", return_value=MagicMock()),
            patch.object(handler_module.client, "V1DeleteOptions", return_value=MagicMock()),
        ):
            result = handler_module._prune_legacy_removed_resources()

        assert result == {"pruned": [], "failed": []}

    def test_base_pass_runs_the_sweep_and_post_helm_does_not(self, handler_module, tmp_path):
        (tmp_path / "00-ns.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": "test-ns"},
                }
            )
        )
        swept = "apps/v1/DaemonSet/kube-system/nvidia-device-plugin-daemonset"
        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch.object(
                handler_module,
                "_prune_legacy_removed_resources",
                return_value={"pruned": [swept], "failed": []},
            ) as mock_sweep,
            patch("handler.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()

            base = handler_module.apply_manifests(
                "test-cluster", "us-east-1", str(tmp_path), {}, post_helm=False
            )
            post_helm = handler_module.apply_manifests(
                "test-cluster", "us-east-1", str(tmp_path), {}, post_helm=True
            )

        mock_sweep.assert_called_once_with()
        assert base["PrunedCount"] == 1
        assert base["Pruned"] == swept
        assert post_helm["PrunedCount"] == 0


class TestPrometheusOperatorCRDs:
    """ServiceMonitor / PodMonitor are applied as monitoring.coreos.com objects.

    Regression guard: these Prometheus Operator CRDs had no dedicated apply
    branch, so they fell through to the "unsupported kind" skip and were never
    created — silently breaking every cluster-observability scrape target.
    They are applied in the post-Helm pass because the kube-prometheus-stack
    chart registers their CRDs.
    """

    def test_servicemonitor_applied_as_namespaced_custom_object(self, handler_module, tmp_path):
        (tmp_path / "post-helm-servicemonitors.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "monitoring.coreos.com/v1",
                    "kind": "ServiceMonitor",
                    "metadata": {"name": "gco-kueue", "namespace": "monitoring"},
                    "spec": {"endpoints": [{"port": "metrics"}]},
                }
            )
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_custom = MagicMock()
            mock_client.CustomObjectsApi.return_value = mock_custom

            result = handler_module.apply_manifests(
                "c", "us-east-1", str(tmp_path), {}, post_helm=True
            )

        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0
        mock_custom.create_namespaced_custom_object.assert_called_once()
        args = mock_custom.create_namespaced_custom_object.call_args.args
        assert args[0] == "monitoring.coreos.com"
        assert args[1] == "v1"
        assert args[2] == "monitoring"
        assert args[3] == "servicemonitors"

    def test_podmonitor_applied_with_podmonitors_plural(self, handler_module, tmp_path):
        (tmp_path / "post-helm-podmonitors.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "monitoring.coreos.com/v1",
                    "kind": "PodMonitor",
                    "metadata": {"name": "gco-health-monitor", "namespace": "monitoring"},
                    "spec": {"podMetricsEndpoints": [{"port": "http"}]},
                }
            )
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_custom = MagicMock()
            mock_client.CustomObjectsApi.return_value = mock_custom

            result = handler_module.apply_manifests(
                "c", "us-east-1", str(tmp_path), {}, post_helm=True
            )

        assert result["AppliedCount"] == 1
        args = mock_custom.create_namespaced_custom_object.call_args.args
        assert args[3] == "podmonitors"

    def test_servicemonitor_patched_on_conflict(self, handler_module, tmp_path):
        """A 409 on create falls back to patch (idempotent re-apply)."""
        from kubernetes.client.rest import ApiException

        (tmp_path / "post-helm-servicemonitors.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "monitoring.coreos.com/v1",
                    "kind": "ServiceMonitor",
                    "metadata": {"name": "gco-kueue", "namespace": "monitoring"},
                    "spec": {"endpoints": [{"port": "metrics"}]},
                }
            )
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_custom = MagicMock()
            mock_custom.create_namespaced_custom_object.side_effect = ApiException(status=409)
            mock_client.CustomObjectsApi.return_value = mock_custom

            result = handler_module.apply_manifests(
                "c", "us-east-1", str(tmp_path), {}, post_helm=True
            )

        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0
        mock_custom.patch_namespaced_custom_object.assert_called_once()
        patch_args = mock_custom.patch_namespaced_custom_object.call_args.args
        assert patch_args[3] == "servicemonitors"
        assert patch_args[4] == "gco-kueue"


class TestCronJobApply:
    """batch/v1 CronJob is applied via BatchV1Api.

    Regression guard mirroring the ServiceMonitor/PodMonitor fix: the Grafana
    admin-password rotation CronJob (observability post-Helm pass) had no
    dedicated apply branch, so it fell through to the "unsupported kind" skip
    and was never created — its RBAC applied but the rotation CronJob did not.
    Caught in live us-east-1 verification.
    """

    def test_cronjob_applied_via_batch_v1(self, handler_module, tmp_path):
        (tmp_path / "post-helm-grafana-credential-rotation.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "batch/v1",
                    "kind": "CronJob",
                    "metadata": {
                        "name": "gco-grafana-admin-password-rotation",
                        "namespace": "monitoring",
                    },
                    "spec": {"schedule": "0 4 1 * *"},
                }
            )
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()
            mock_batch = MagicMock()
            mock_client.BatchV1Api.return_value = mock_batch

            result = handler_module.apply_manifests(
                "c", "us-east-1", str(tmp_path), {}, post_helm=True
            )

        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0
        assert "CronJob/gco-grafana-admin-password-rotation" not in result["Skipped"]
        mock_batch.create_namespaced_cron_job.assert_called_once()
        assert mock_batch.create_namespaced_cron_job.call_args.args[0] == "monitoring"

    def test_cronjob_patched_on_conflict(self, handler_module, tmp_path):
        """A 409 on create falls back to patch (idempotent re-apply)."""
        from kubernetes.client.rest import ApiException

        (tmp_path / "post-helm-grafana-credential-rotation.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "batch/v1",
                    "kind": "CronJob",
                    "metadata": {
                        "name": "gco-grafana-admin-password-rotation",
                        "namespace": "monitoring",
                    },
                    "spec": {"schedule": "0 4 1 * *"},
                }
            )
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()
            mock_batch = MagicMock()
            mock_batch.create_namespaced_cron_job.side_effect = ApiException(status=409)
            mock_client.BatchV1Api.return_value = mock_batch

            result = handler_module.apply_manifests(
                "c", "us-east-1", str(tmp_path), {}, post_helm=True
            )

        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0
        mock_batch.patch_namespaced_cron_job.assert_called_once()
        patch_args = mock_batch.patch_namespaced_cron_job.call_args.args
        assert patch_args[0] == "gco-grafana-admin-password-rotation"
        assert patch_args[1] == "monitoring"


class TestPersistentVolumeHandling:
    """Tests for PV smart recreate logic."""

    def test_pv_skip_when_volume_handle_unchanged(self, handler_module, tmp_path):
        """PV with same volumeHandle is skipped (no-op)."""
        from kubernetes.client.rest import ApiException

        pv_doc = {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {"name": "test-pv"},
            "spec": {
                "capacity": {"storage": "1200Gi"},
                "accessModes": ["ReadWriteMany"],
                "csi": {
                    "driver": "fsx.csi.aws.com",
                    "volumeHandle": "fs-abc123",
                },
            },
        }
        (tmp_path / "20-pv.yaml").write_text(yaml.dump(pv_doc))

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_v1 = MagicMock()
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()

            # create_persistent_volume raises 409 (already exists)
            mock_v1.create_persistent_volume.side_effect = ApiException(status=409)

            # read returns existing PV with same volumeHandle
            existing_pv = MagicMock()
            existing_pv.spec.csi.volume_handle = "fs-abc123"
            mock_v1.read_persistent_volume.return_value = existing_pv

            result = handler_module.apply_manifests("test-cluster", "us-east-1", str(tmp_path), {})

        # Should succeed (skip counts as applied)
        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0
        # Should NOT have called delete
        mock_v1.delete_persistent_volume.assert_not_called()

    def test_pv_recreate_when_volume_handle_changed(self, handler_module, tmp_path):
        """PV with different volumeHandle is deleted and recreated."""
        from kubernetes.client.rest import ApiException

        pv_doc = {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {"name": "test-pv"},
            "spec": {
                "capacity": {"storage": "1200Gi"},
                "accessModes": ["ReadWriteMany"],
                "csi": {
                    "driver": "fsx.csi.aws.com",
                    "volumeHandle": "fs-NEW456",
                },
            },
        }
        (tmp_path / "20-pv.yaml").write_text(yaml.dump(pv_doc))

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_v1 = MagicMock()
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()

            # First create raises 409
            # Second create (after delete) succeeds
            mock_v1.create_persistent_volume.side_effect = [
                ApiException(status=409),
                None,
            ]

            # Existing PV has OLD volumeHandle
            existing_pv = MagicMock()
            existing_pv.spec.csi.volume_handle = "fs-OLD123"
            mock_v1.read_persistent_volume.side_effect = [
                existing_pv,  # first read (check existing)
                ApiException(status=404),  # second read (wait loop — PV gone)
            ]

            result = handler_module.apply_manifests("test-cluster", "us-east-1", str(tmp_path), {})

        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0
        # Should have removed finalizer, deleted, and recreated
        mock_v1.patch_persistent_volume.assert_called_once()
        mock_v1.delete_persistent_volume.assert_called_once_with("test-pv")
        assert mock_v1.create_persistent_volume.call_count == 2


class TestPersistentVolumeClaimHandling:
    """Tests for PVC Lost-state recovery.

    When a PV is deleted and recreated (e.g. FSx file system replaced),
    the bound PVC goes into ``Lost`` state because its binding UID no
    longer matches. The handler must detect this and delete+recreate
    the PVC so it binds to the new PV.
    """

    def test_pvc_patch_when_healthy(self, handler_module, tmp_path):
        """Existing healthy PVC is patched (normal update path)."""
        from kubernetes.client.rest import ApiException

        pvc_doc = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "gco-fsx-storage", "namespace": "gco-jobs"},
            "spec": {
                "accessModes": ["ReadWriteMany"],
                "storageClassName": "fsx-sc",
                "resources": {"requests": {"storage": "1200Gi"}},
            },
        }
        (tmp_path / "21-pvc.yaml").write_text(yaml.dump(pvc_doc))

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_v1 = MagicMock()
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()

            # create raises 409 (already exists)
            mock_v1.create_namespaced_persistent_volume_claim.side_effect = ApiException(status=409)

            # existing PVC is Bound (healthy)
            existing_pvc = MagicMock()
            existing_pvc.status.phase = "Bound"
            mock_v1.read_namespaced_persistent_volume_claim.return_value = existing_pvc

            result = handler_module.apply_manifests("test-cluster", "us-east-1", str(tmp_path), {})

        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0
        # Should patch, not delete
        mock_v1.patch_namespaced_persistent_volume_claim.assert_called_once()
        mock_v1.delete_namespaced_persistent_volume_claim.assert_not_called()

    def test_pvc_recreate_when_lost(self, handler_module, tmp_path):
        """Lost PVC is deleted and recreated to bind to the new PV."""
        from kubernetes.client.rest import ApiException

        pvc_doc = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "gco-fsx-storage", "namespace": "gco-jobs"},
            "spec": {
                "accessModes": ["ReadWriteMany"],
                "storageClassName": "fsx-sc",
                "resources": {"requests": {"storage": "1200Gi"}},
            },
        }
        (tmp_path / "21-pvc.yaml").write_text(yaml.dump(pvc_doc))

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_v1 = MagicMock()
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()

            # First create raises 409 (already exists)
            # Second create (after delete) succeeds
            mock_v1.create_namespaced_persistent_volume_claim.side_effect = [
                ApiException(status=409),
                None,
            ]

            # Existing PVC is Lost
            existing_pvc = MagicMock()
            existing_pvc.status.phase = "Lost"
            mock_v1.read_namespaced_persistent_volume_claim.side_effect = [
                existing_pvc,  # first read (check status)
                ApiException(status=404),  # second read (wait loop — PVC gone)
            ]

            result = handler_module.apply_manifests("test-cluster", "us-east-1", str(tmp_path), {})

        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0
        # Should have deleted and recreated
        mock_v1.delete_namespaced_persistent_volume_claim.assert_called_once_with(
            "gco-fsx-storage", "gco-jobs"
        )
        assert mock_v1.create_namespaced_persistent_volume_claim.call_count == 2
        # Should NOT have patched
        mock_v1.patch_namespaced_persistent_volume_claim.assert_not_called()


class TestPostHelmPassNoRestarts:
    """Tests that the post-helm pass doesn't restart deployments or verify credentials."""

    def test_post_helm_pass_returns_minimal_response(self, handler_module, tmp_path):
        """Post-helm pass doesn't include RestartedDeployments or CredentialWarnings."""
        (tmp_path / "post-helm-test.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "test", "namespace": "default"},
                    "data": {"key": "value"},
                }
            )
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_v1 = MagicMock()
            mock_client.CoreV1Api.return_value = mock_v1
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()

            result = handler_module.apply_manifests(
                "test-cluster", "us-east-1", str(tmp_path), {}, post_helm=True
            )

        # Post-helm response should NOT have restart or credential fields
        assert "RestartedDeployments" not in result
        assert "CredentialWarnings" not in result
        assert result["AppliedCount"] == 1


class TestLambdaHandler:
    """Tests for the lambda_handler entry point."""

    def test_passes_post_helm_flag(self, handler_module):
        """lambda_handler passes PostHelm property to apply_manifests."""
        event = {
            "RequestType": "Update",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/id",
            "RequestId": "req-123",
            "LogicalResourceId": "KubectlApply",
            "PhysicalResourceId": "phys-123",
            "ResponseURL": "https://example.com",
            "ResourceProperties": {
                "ClusterName": "test-cluster",
                "Region": "us-east-1",
                "PostHelm": "true",
                "ImageReplacements": {},
            },
        }

        with (
            patch.object(handler_module, "apply_manifests") as mock_apply,
            patch.object(handler_module, "send_response"),
        ):
            mock_apply.return_value = {"AppliedCount": 0, "FailedCount": 0}
            handler_module.lambda_handler(event, MagicMock())

        # Verify post_helm=True was passed
        _, kwargs = mock_apply.call_args
        assert kwargs.get("post_helm") is True or mock_apply.call_args[0][4] is True

    def test_default_post_helm_is_false(self, handler_module):
        """lambda_handler defaults PostHelm to false."""
        event = {
            "RequestType": "Create",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/id",
            "RequestId": "req-123",
            "LogicalResourceId": "KubectlApply",
            "PhysicalResourceId": "phys-123",
            "ResponseURL": "https://example.com",
            "ResourceProperties": {
                "ClusterName": "test-cluster",
                "Region": "us-east-1",
                "ImageReplacements": {},
            },
        }

        with (
            patch.object(handler_module, "apply_manifests") as mock_apply,
            patch.object(handler_module, "send_response"),
        ):
            mock_apply.return_value = {
                "AppliedCount": 0,
                "FailedCount": 0,
                "SkippedCount": 0,
            }
            handler_module.lambda_handler(event, MagicMock())

        # Verify post_helm=False was passed
        _, kwargs = mock_apply.call_args
        assert kwargs.get("post_helm") is False or mock_apply.call_args[0][4] is False

    def test_failed_apply_sends_failed_cloudformation_response(self, handler_module):
        event = {
            "RequestType": "Update",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/id",
            "RequestId": "req-123",
            "LogicalResourceId": "KubectlApply",
            "PhysicalResourceId": "phys-123",
            "ResponseURL": "https://example.com",
            "ResourceProperties": {
                "ClusterName": "test-cluster",
                "Region": "us-east-1",
                "ImageReplacements": {},
            },
        }
        response = {
            "AppliedCount": 2,
            "FailedCount": 1,
            "Failed": "bad.yaml:Deployment/bad",
            "PruneFailures": "None",
        }

        with (
            patch.object(handler_module, "apply_manifests", return_value=response),
            patch.object(handler_module, "send_response") as mock_send,
        ):
            handler_module.lambda_handler(event, MagicMock())

        assert mock_send.call_count == 1
        assert mock_send.call_args.args[2] == handler_module.FAILED
        assert "failed_count=1" in mock_send.call_args.args[5]


class TestAddonRolloutRestarts:
    """
    Regression guards for the post-install IRSA role-ARN race.

    When EKS managed addons (aws-efs-csi-driver, aws-fsx-csi-driver,
    amazon-cloudwatch-observability) are created, their service accounts
    and controller pods land in parallel. We then call UpdateAddon with
    a serviceAccountRoleArn, which patches the SA's role-arn annotation
    but does NOT restart the pods. The existing pods keep their
    un-mutated pod spec (no AWS_ROLE_ARN, no projected token) and fall
    back to IMDS for credentials — which EKS Auto Mode blocks. The
    visible symptom is PVCs stuck Pending forever with
    "no EC2 IMDS role found".

    These tests guard against that regression by asserting the kubectl
    Lambda explicitly rollout-restarts the affected Deployments and
    DaemonSets at the end of the main apply pass.
    """

    def test_restart_deployments_skips_missing_with_404(self, handler_module):
        """404 on patch is treated as "not installed" — not an error."""
        from kubernetes.client.rest import ApiException

        mock_apps_v1 = MagicMock()
        # First deployment is missing (404), second is present.
        mock_apps_v1.patch_namespaced_deployment.side_effect = [
            ApiException(status=404, reason="Not Found"),
            MagicMock(),
        ]

        with patch.object(handler_module.client, "AppsV1Api", return_value=mock_apps_v1):
            result = handler_module.restart_deployments(
                "kube-system", ["fsx-csi-controller", "efs-csi-controller"]
            )

        # The 404 is skipped, not counted as a failure. Only the
        # successfully-patched deployment shows up in `restarted`.
        assert result["restarted"] == ["efs-csi-controller"]
        assert result["failed"] == []

    def test_restart_deployments_records_non_404_errors(self, handler_module):
        """Non-404 errors (403, 500, etc.) are still treated as failures."""
        from kubernetes.client.rest import ApiException

        mock_apps_v1 = MagicMock()
        mock_apps_v1.patch_namespaced_deployment.side_effect = ApiException(
            status=403, reason="Forbidden"
        )

        with patch.object(handler_module.client, "AppsV1Api", return_value=mock_apps_v1):
            result = handler_module.restart_deployments("kube-system", ["efs-csi-controller"])

        assert result["restarted"] == []
        assert result["failed"] == ["efs-csi-controller"]

    def test_restart_daemonsets_patches_daemonset_not_deployment(self, handler_module):
        """restart_daemonsets uses patch_namespaced_daemon_set, not _deployment."""
        mock_apps_v1 = MagicMock()

        with patch.object(handler_module.client, "AppsV1Api", return_value=mock_apps_v1):
            result = handler_module.restart_daemonsets("kube-system", ["efs-csi-node"])

        # Must call the DaemonSet API, not the Deployment API.
        mock_apps_v1.patch_namespaced_daemon_set.assert_called_once()
        mock_apps_v1.patch_namespaced_deployment.assert_not_called()
        assert result["restarted"] == ["efs-csi-node"]
        assert result["failed"] == []

    def test_restart_daemonsets_skips_missing_with_404(self, handler_module):
        """FSx daemonset is missing when FSx is disabled — must not fail."""
        from kubernetes.client.rest import ApiException

        mock_apps_v1 = MagicMock()
        mock_apps_v1.patch_namespaced_daemon_set.side_effect = ApiException(
            status=404, reason="Not Found"
        )

        with patch.object(handler_module.client, "AppsV1Api", return_value=mock_apps_v1):
            result = handler_module.restart_daemonsets("kube-system", ["fsx-csi-node"])

        assert result["restarted"] == []
        assert result["failed"] == []

    def test_restart_patches_include_kubectl_restart_annotation(self, handler_module):
        """The patch body must use the canonical `kubectl.kubernetes.io/restartedAt` annotation."""
        mock_apps_v1 = MagicMock()

        with patch.object(handler_module.client, "AppsV1Api", return_value=mock_apps_v1):
            handler_module.restart_deployments("kube-system", ["efs-csi-controller"])

        call_args = mock_apps_v1.patch_namespaced_deployment.call_args
        body = call_args.kwargs.get("body") or call_args.args[2]
        annotations = body["spec"]["template"]["metadata"]["annotations"]
        # The annotation name must match `kubectl rollout restart` exactly so
        # cluster operators can diff against their own `kubectl` output.
        assert "kubectl.kubernetes.io/restartedAt" in annotations
        # And the value must be a non-empty ISO timestamp.
        assert annotations["kubectl.kubernetes.io/restartedAt"]


class TestMainPassRestartsAddonControllers:
    """
    Assert the main (non-post-helm) apply pass restarts every addon
    controller and DaemonSet that needs to re-pick-up its IRSA role-ARN
    annotation.

    This is the production guardrail against the EFS/FSx/CloudWatch
    post-install IRSA race. If someone adds a new managed addon with a
    serviceAccountRoleArn patched post-install, they should either add
    its controller to this list or accept that it won't work on cold
    installs.
    """

    def test_main_pass_restarts_efs_and_fsx_controllers(self, handler_module, tmp_path):
        """efs-csi-controller and fsx-csi-controller are restarted in kube-system."""
        # Minimal manifest so apply_manifests completes.
        (tmp_path / "00-ns.yaml").write_text(
            yaml.dump({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "demo"}})
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
            patch.object(handler_module, "restart_deployments") as mock_restart_deploy,
            patch.object(handler_module, "restart_daemonsets") as mock_restart_ds,
            patch.object(handler_module, "_verify_workload_credentials", return_value=[]),
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()
            mock_restart_deploy.return_value = {"restarted": [], "failed": []}
            mock_restart_ds.return_value = {"restarted": [], "failed": []}

            handler_module.apply_manifests(
                "test-cluster", "us-east-1", str(tmp_path), {}, post_helm=False
            )

        # Collect every (namespace, names) pair restart_deployments was
        # called with. We don't care about argument order between gco-system
        # and kube-system — we only care both restarts happened.
        deploy_calls = {
            (call.args[0], tuple(call.args[1])) for call in mock_restart_deploy.call_args_list
        }
        assert (
            "gco-system",
            ("health-monitor", "manifest-processor", "inference-monitor", "inference-proxy"),
        ) in deploy_calls
        assert ("kube-system", ("efs-csi-controller", "fsx-csi-controller")) in deploy_calls

    def test_main_pass_restarts_csi_and_cloudwatch_daemonsets(self, handler_module, tmp_path):
        """efs-csi-node, fsx-csi-node, and cloudwatch-agent DaemonSets are restarted."""
        (tmp_path / "00-ns.yaml").write_text(
            yaml.dump({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "demo"}})
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
            patch.object(handler_module, "restart_deployments") as mock_restart_deploy,
            patch.object(handler_module, "restart_daemonsets") as mock_restart_ds,
            patch.object(handler_module, "_verify_workload_credentials", return_value=[]),
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()
            mock_restart_deploy.return_value = {"restarted": [], "failed": []}
            mock_restart_ds.return_value = {"restarted": [], "failed": []}

            handler_module.apply_manifests(
                "test-cluster", "us-east-1", str(tmp_path), {}, post_helm=False
            )

        ds_calls = {(call.args[0], tuple(call.args[1])) for call in mock_restart_ds.call_args_list}
        assert ("kube-system", ("efs-csi-node", "fsx-csi-node")) in ds_calls
        assert ("amazon-cloudwatch", ("cloudwatch-agent",)) in ds_calls

    def test_post_helm_pass_does_not_restart_addon_controllers(self, handler_module, tmp_path):
        """Post-helm pass is a pure apply — no restarts should fire."""
        (tmp_path / "post-helm-test.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "t", "namespace": "default"},
                }
            )
        )

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
            patch.object(handler_module, "restart_deployments") as mock_restart_deploy,
            patch.object(handler_module, "restart_daemonsets") as mock_restart_ds,
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()

            result = handler_module.apply_manifests(
                "test-cluster", "us-east-1", str(tmp_path), {}, post_helm=True
            )

        # Post-helm pass exits early, before any restart happens.
        mock_restart_deploy.assert_not_called()
        mock_restart_ds.assert_not_called()
        # And the response is the minimal shape without restart metadata.
        assert "RestartedDeployments" not in result


class TestHandleTask:
    """Tests for the Step Functions task entrypoint (Action-dispatched).

    The convergence state machine invokes the handler with an ``Action`` key
    (no CloudFormation ``RequestType``) for both the base and post-Helm apply
    passes. Unlike the custom-resource path — which reports SUCCESS even when
    individual manifests fail — the task path RAISES on any manifest failure so
    the state machine's Retry/Catch can react (the base pass fails the
    execution; the post-Helm pass is caught and the pipeline continues).
    """

    def test_action_event_dispatches_to_apply_and_returns_result(self, handler_module):
        """An ``Action`` event applies manifests and returns the result dict
        directly (no send_response / CloudFormation round-trip)."""
        event = {
            "Action": "apply_manifests",
            "ClusterName": "test-cluster",
            "Region": "us-east-1",
            "ImageReplacements": {"{{X}}": "y"},
            "PostHelm": "false",
        }
        with (
            patch.object(handler_module, "apply_manifests") as mock_apply,
            patch.object(handler_module, "send_response") as mock_send,
        ):
            mock_apply.return_value = {"AppliedCount": 3, "FailedCount": 0}
            result = handler_module.lambda_handler(event, MagicMock())

        assert result == {"AppliedCount": 3, "FailedCount": 0}
        # ImageReplacements forwarded; PostHelm "false" parsed to False.
        assert mock_apply.call_args[0][3] == {"{{X}}": "y"}
        assert mock_apply.call_args[0][4] is False
        # The task path never posts a CloudFormation response.
        mock_send.assert_not_called()

    def test_post_helm_true_string_parsed(self, handler_module):
        """``PostHelm`` arrives as the string "true" from the state machine."""
        event = {
            "Action": "apply_manifests",
            "ClusterName": "c",
            "Region": "us-east-1",
            "PostHelm": "true",
        }
        with patch.object(handler_module, "apply_manifests") as mock_apply:
            mock_apply.return_value = {"AppliedCount": 1, "FailedCount": 0}
            handler_module.handle_task(event)
        assert mock_apply.call_args[0][4] is True

    def test_raises_when_any_manifest_failed(self, handler_module):
        """A non-zero FailedCount must raise so the state machine can retry/catch."""
        event = {
            "Action": "apply_manifests",
            "ClusterName": "c",
            "Region": "us-east-1",
            "PostHelm": "false",
        }
        with patch.object(handler_module, "apply_manifests") as mock_apply:
            mock_apply.return_value = {
                "AppliedCount": 2,
                "FailedCount": 1,
                "Failed": "x.yaml:Foo/bar",
            }
            with pytest.raises(RuntimeError, match="kubectl apply failed"):
                handler_module.handle_task(event)

    def test_records_applied_status_on_success(self, handler_module):
        """On success the base pass records status 'applied' to SSM."""
        event = {
            "Action": "apply_manifests",
            "ClusterName": "c",
            "Region": "us-east-1",
            "PostHelm": "false",
        }
        with (
            patch.object(handler_module, "apply_manifests") as mock_apply,
            patch.object(handler_module, "_record_phase_status") as mock_status,
        ):
            mock_apply.return_value = {"AppliedCount": 5, "FailedCount": 0, "SkippedCount": 1}
            handler_module.handle_task(event)
        mock_status.assert_called_once()
        phase, status = mock_status.call_args[0][0], mock_status.call_args[0][1]
        assert phase == "base-manifests"
        assert status == "applied"

    def test_records_failed_status_before_raising(self, handler_module):
        """On failure the post-Helm pass records status 'failed' before raising."""
        event = {
            "Action": "apply_manifests",
            "ClusterName": "c",
            "Region": "us-east-1",
            "PostHelm": "true",
        }
        with (
            patch.object(handler_module, "apply_manifests") as mock_apply,
            patch.object(handler_module, "_record_phase_status") as mock_status,
        ):
            mock_apply.return_value = {"AppliedCount": 1, "FailedCount": 1, "Failed": "x"}
            with pytest.raises(RuntimeError):
                handler_module.handle_task(event)
        mock_status.assert_called_once()
        phase, status = mock_status.call_args[0][0], mock_status.call_args[0][1]
        assert phase == "post-helm-manifests"
        assert status == "failed"


class TestGatewayResourceDeletion:
    """Gateway teardown is ordered, bounded, idempotent, and task-dispatched."""

    @staticmethod
    def _not_found(handler_module):
        return handler_module.ApiException(status=404, reason="Not Found")

    def test_deletes_routes_first_and_gateway_class_last_waiting_for_absence(self, handler_module):
        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = [
            self._not_found(handler_module) for _ in range(4)
        ]
        custom_api.get_cluster_custom_object.side_effect = self._not_found(handler_module)
        delete_options = MagicMock()

        with (
            patch.object(handler_module, "configure_k8s_client") as configure,
            patch.object(handler_module.client, "CustomObjectsApi", return_value=custom_api),
            patch.object(handler_module.client, "V1DeleteOptions", return_value=delete_options),
        ):
            result = handler_module._delete_gateway_resources("gco-us-east-1", "us-east-1")

        configure.assert_called_once_with("gco-us-east-1", "us-east-1")
        assert result == {
            "status": "deleted",
            "DeletedCount": 5,
            "Deleted": [
                "HTTPRoute/gco-system/gco-routes",
                "Gateway/gco-system/gco-gateway",
                "LoadBalancerConfiguration/gco-system/gco-gateway-load-balancer",
                "TargetGroupConfiguration/gco-system/gco-default-target-group",
                "GatewayClass/gco-aws-alb",
            ],
        }
        assert [entry[0] for entry in custom_api.mock_calls] == [
            "delete_namespaced_custom_object",
            "get_namespaced_custom_object",
            "delete_namespaced_custom_object",
            "get_namespaced_custom_object",
            "delete_namespaced_custom_object",
            "get_namespaced_custom_object",
            "delete_namespaced_custom_object",
            "get_namespaced_custom_object",
            "delete_cluster_custom_object",
            "get_cluster_custom_object",
        ]
        assert custom_api.delete_namespaced_custom_object.call_args_list[0] == call(
            "gateway.networking.k8s.io",
            "v1",
            "gco-system",
            "httproutes",
            "gco-routes",
            body=delete_options,
        )
        assert custom_api.delete_cluster_custom_object.call_args == call(
            "gateway.networking.k8s.io",
            "v1",
            "gatewayclasses",
            "gco-aws-alb",
            body=delete_options,
        )

    def test_already_absent_resources_are_idempotent(self, handler_module):
        custom_api = MagicMock()
        custom_api.delete_namespaced_custom_object.side_effect = [
            self._not_found(handler_module) for _ in range(4)
        ]
        custom_api.delete_cluster_custom_object.side_effect = self._not_found(handler_module)

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch.object(handler_module.client, "CustomObjectsApi", return_value=custom_api),
            patch.object(handler_module.client, "V1DeleteOptions"),
        ):
            result = handler_module._delete_gateway_resources("cluster", "us-east-1")

        assert result["DeletedCount"] == 5
        assert all(item.endswith(":already-absent") for item in result["Deleted"])
        custom_api.get_namespaced_custom_object.assert_not_called()
        custom_api.get_cluster_custom_object.assert_not_called()

    def test_one_deadline_bounds_the_complete_resource_set(self, handler_module):
        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {"metadata": {}}

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch.object(handler_module.client, "CustomObjectsApi", return_value=custom_api),
            patch.object(handler_module.client, "V1DeleteOptions"),
            patch.object(
                handler_module.time,
                "monotonic",
                side_effect=[100.0, 100.0 + handler_module._GATEWAY_DELETE_WAIT_SECONDS],
            ),
            patch.object(handler_module.time, "sleep") as sleep,
            pytest.raises(RuntimeError, match="complete Gateway resource set"),
        ):
            handler_module._delete_gateway_resources("cluster", "us-east-1")

        assert custom_api.delete_namespaced_custom_object.call_count == 1
        sleep.assert_not_called()

    def test_task_action_dispatches_and_records_gateway_teardown(self, handler_module):
        deleted = {"status": "deleted", "DeletedCount": 5, "Deleted": ["five objects"]}
        with (
            patch.object(
                handler_module, "_delete_gateway_resources", return_value=deleted
            ) as delete_gateway,
            patch.object(handler_module, "_record_phase_status") as record,
        ):
            result = handler_module.handle_task(
                {
                    "Action": "delete_gateway_resources",
                    "ClusterName": "gco-us-east-1",
                    "Region": "us-east-1",
                }
            )

        assert result is deleted
        delete_gateway.assert_called_once_with("gco-us-east-1", "us-east-1")
        record.assert_called_once_with("gateway-teardown", "deleted", "deleted=5")


class TestHorizontalPodAutoscalerApply:
    """autoscaling/v2 HPAs are created and patched idempotently."""

    @staticmethod
    def _write_hpa(tmp_path):
        document = {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {
                "name": "inference-proxy-hpa",
                "namespace": "gco-system",
            },
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": "inference-proxy",
                },
                "minReplicas": 3,
                "maxReplicas": 10,
            },
        }
        (tmp_path / "33-inference-proxy-hpa.yaml").write_text(yaml.safe_dump(document))
        return document

    @staticmethod
    def _apply(handler_module, tmp_path, mock_client):
        mock_client.CoreV1Api.return_value = MagicMock()
        mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
        mock_client.NetworkingV1Api.return_value = MagicMock()
        mock_client.CustomObjectsApi.return_value = MagicMock()
        restart_result = {"restarted": [], "failed": []}
        with (
            patch.object(
                handler_module,
                "restart_deployments",
                return_value=restart_result,
            ),
            patch.object(
                handler_module,
                "restart_daemonsets",
                return_value=restart_result,
            ),
            patch.object(handler_module, "_verify_workload_credentials", return_value=[]),
        ):
            return handler_module.apply_manifests(
                "test-cluster",
                "us-east-1",
                str(tmp_path),
                {},
            )

    def test_deployment_update_omits_hpa_owned_replicas(self, handler_module, tmp_path):
        """Create seeds three replicas; reconciliation leaves HPA scale untouched."""
        from kubernetes.client.rest import ApiException

        document = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "inference-proxy",
                "namespace": "gco-system",
                "annotations": {"gco.aws/hpa-controls-replicas": "true"},
            },
            "spec": {
                "replicas": 3,
                "selector": {"matchLabels": {"app": "inference-proxy"}},
                "template": {
                    "metadata": {"labels": {"app": "inference-proxy"}},
                    "spec": {
                        "containers": [
                            {"name": "inference-proxy", "image": "example.invalid/proxy:test"}
                        ]
                    },
                },
            },
        }
        (tmp_path / "33-inference-proxy-deployment.yaml").write_text(yaml.safe_dump(document))

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_apps = mock_client.AppsV1Api.return_value
            mock_apps.create_namespaced_deployment.side_effect = ApiException(status=409)
            result = self._apply(handler_module, tmp_path, mock_client)

        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0
        mock_apps.create_namespaced_deployment.assert_called_once_with(
            "gco-system",
            body=document,
        )
        patch_body = mock_apps.patch_namespaced_deployment.call_args.kwargs["body"]
        assert "replicas" not in patch_body["spec"]
        assert patch_body["spec"]["template"] == document["spec"]["template"]
        assert document["spec"]["replicas"] == 3

    def test_deployment_update_retains_replicas_without_hpa_ownership(self, handler_module):
        """Ordinary Deployment updates continue to reconcile manifest replicas."""
        document = {
            "metadata": {"annotations": {"example.invalid/owner": "operator"}},
            "spec": {"replicas": 4},
        }

        patch_body = handler_module._deployment_patch_body(document)

        assert patch_body == document
        assert patch_body is not document
        assert patch_body["spec"] is not document["spec"]
        assert patch_body["spec"]["replicas"] == 4

    def test_hpa_applied_via_autoscaling_v2(self, handler_module, tmp_path):
        """The HPA does not fall through to the unsupported-kind branch."""
        document = self._write_hpa(tmp_path)
        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_autoscaling = MagicMock()
            mock_client.AutoscalingV2Api.return_value = mock_autoscaling
            result = self._apply(handler_module, tmp_path, mock_client)

        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0
        assert "HorizontalPodAutoscaler/inference-proxy-hpa" not in result["Skipped"]
        mock_autoscaling.create_namespaced_horizontal_pod_autoscaler.assert_called_once_with(
            "gco-system",
            body=document,
        )

    def test_hpa_patched_on_conflict(self, handler_module, tmp_path):
        """A 409 create response patches the existing HPA."""
        from kubernetes.client.rest import ApiException

        document = self._write_hpa(tmp_path)
        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_autoscaling = MagicMock()
            mock_autoscaling.create_namespaced_horizontal_pod_autoscaler.side_effect = ApiException(
                status=409
            )
            mock_client.AutoscalingV2Api.return_value = mock_autoscaling
            result = self._apply(handler_module, tmp_path, mock_client)

        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0
        mock_autoscaling.patch_namespaced_horizontal_pod_autoscaler.assert_called_once_with(
            "inference-proxy-hpa",
            "gco-system",
            body=document,
        )


class TestInferenceProxyAutoscalingManifest:
    """Static contract for the shared streaming proxy's HPA and drain budget."""

    def test_hpa_and_deployment_are_stream_safe(self):
        manifest_path = (
            Path(__file__).parent.parent
            / "lambda"
            / "kubectl-applier-simple"
            / "manifests"
            / "33-inference-proxy.yaml"
        )
        content = manifest_path.read_text().replace(
            "{{INFERENCE_PROXY_IMAGE}}",
            "example.invalid/inference-proxy:test",
        )
        documents = list(yaml.safe_load_all(content))
        deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
        hpa = next(doc for doc in documents if doc["kind"] == "HorizontalPodAutoscaler")
        pdb = next(doc for doc in documents if doc["kind"] == "PodDisruptionBudget")

        assert deployment["spec"]["replicas"] == 3
        assert deployment["metadata"]["annotations"] == {"gco.aws/hpa-controls-replicas": "true"}
        assert hpa["apiVersion"] == "autoscaling/v2"
        assert hpa["metadata"] == {
            "name": "inference-proxy-hpa",
            "namespace": "gco-system",
            "labels": {
                "app": "inference-proxy",
                "component": "inference-data-plane",
                "project": "gco",
            },
        }
        assert hpa["spec"]["scaleTargetRef"] == {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": "inference-proxy",
        }
        assert hpa["spec"]["minReplicas"] == 3
        assert hpa["spec"]["maxReplicas"] == 10
        assert hpa["spec"]["metrics"] == [
            {
                "type": "Resource",
                "resource": {
                    "name": "cpu",
                    "target": {"type": "Utilization", "averageUtilization": 70},
                },
            },
            {
                "type": "Resource",
                "resource": {
                    "name": "memory",
                    "target": {"type": "Utilization", "averageUtilization": 80},
                },
            },
        ]
        assert hpa["spec"]["behavior"]["scaleDown"] == {
            "stabilizationWindowSeconds": 900,
            "selectPolicy": "Min",
            "policies": [
                {"type": "Percent", "value": 25, "periodSeconds": 60},
                {"type": "Pods", "value": 1, "periodSeconds": 60},
            ],
        }
        pod_spec = deployment["spec"]["template"]["spec"]
        assert pod_spec["terminationGracePeriodSeconds"] == 930
        env = {item["name"]: item.get("value") for item in pod_spec["containers"][0]["env"]}
        assert env["GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS"] == "900"
        # python, not /bin/sh: the distroless service image ships no shell.
        assert pod_spec["containers"][0]["lifecycle"]["preStop"]["exec"]["command"] == [
            "python",
            "-c",
            "import time; time.sleep(10)",
        ]
        assert pdb["spec"]["minAvailable"] == 2
        assert pdb["spec"]["selector"]["matchLabels"] == {"app": "inference-proxy"}

    def test_gateway_target_group_deregistration_covers_stream_drain(self):
        """ALB draining lasts at least as long as Uvicorn's maximum stream drain."""
        gateway_path = (
            Path(__file__).parent.parent
            / "lambda"
            / "kubectl-applier-simple"
            / "manifests"
            / "post-helm-gateway.yaml"
        )
        documents = list(yaml.safe_load_all(gateway_path.read_text()))
        target_group = next(
            document
            for document in documents
            if document and document.get("kind") == "TargetGroupConfiguration"
        )
        attributes = {
            item["key"]: item["value"]
            for item in target_group["spec"]["defaultConfiguration"]["targetGroupAttributes"]
        }
        assert attributes["deregistration_delay.timeout_seconds"] == "900"

    def test_uvicorn_runtime_honors_graceful_shutdown_budget(self):
        """The container entrypoint forwards its configured drain window to Uvicorn."""
        from gco.services import inference_api

        assert inference_api.DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS == 900
        with (
            patch.dict(
                "os.environ",
                {"GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS": "901", "PORT": "8080"},
            ),
            patch("uvicorn.run") as mock_run,
        ):
            inference_api._run_server()

        assert mock_run.call_args.kwargs["timeout_graceful_shutdown"] == 901


class TestAuthoritativeManifestPlanner:
    """The raw-manifest planner is the only source of expected inventory."""

    def test_automatically_includes_sorted_yaml_and_yml_and_partitions_phases(
        self, handler_module, tmp_path
    ):
        (tmp_path / "20-second.yml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "second"},
                }
            )
        )
        (tmp_path / "10-first.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "first", "namespace": "demo"},
                }
            )
        )
        (tmp_path / "post-helm-third.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "third", "namespace": "monitoring"},
                }
            )
        )
        (tmp_path / "ignored.txt").write_text("not a manifest")

        plan = handler_module.plan_manifests(str(tmp_path), {})

        assert [item["name"] for item in plan["phases"]["base"]] == ["first", "second"]
        assert [item["sourceFile"] for item in plan["phases"]["post-helm"]] == [
            "post-helm-third.yaml"
        ]
        assert plan["phases"]["base"][0]["namespace"] == "demo"
        assert plan["phases"]["base"][1]["namespace"] == "default"
        assert plan["skipped"]["base"] == ["post-helm-third.yaml:deferred-to-post-helm"]

    def test_excludes_complete_file_for_unresolved_upper_snake_gate(self, handler_module, tmp_path):
        (tmp_path / "10-required.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: required\n"
        )
        (tmp_path / "20-optional.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: optional\n"
            "data:\n  enabled: '{{OPTIONAL_FEATURE_ENABLED}}'\n"
            "  dashboard-token: '{{gpu}}'\n"
        )

        plan = handler_module.plan_manifests(str(tmp_path), {})

        assert [item["name"] for item in plan["phases"]["base"]] == ["required"]
        assert plan["featureGates"]["base"] == ["{{OPTIONAL_FEATURE_ENABLED}}"]
        assert plan["skipped"]["base"] == ["20-optional.yaml:unreplaced-placeholders"]

    def test_planner_accepts_the_real_manifest_directory_fully_enabled(self, handler_module):
        """Every kind in the shipped manifests must pass planning.

        Regression: the 2026-09 live validation deploy failed because the
        Kueue default-queue kinds were added to the apply dispatch and the
        custom-object map but not to _SUPPORTED_MANIFEST_KINDS — planning
        rejected them at deploy time, hours into a live run. Plan the real
        directory with every feature gate resolved so a kind the planner
        does not know can never reach a live cluster first.
        """
        manifests_dir = (
            Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple" / "manifests"
        )
        token_re = re.compile(r"\{\{[A-Za-z0-9_]+\}\}")
        quantity_tokens = {"{{QUOTA_MAX_CPU}}", "{{QUOTA_MAX_MEMORY}}", "{{QUOTA_MAX_GPU}}"}
        integer_prefixes = ("{{QP_", "{{LIMIT_", "{{QUOTA_MAX_PODS}}")
        replacements: dict[str, str] = {
            "{{VPC_ENDPOINT_CIDR_BLOCKS}}": '- ipBlock:\n            cidr: "10.0.0.0/16"',
        }
        for manifest in sorted(manifests_dir.glob("*.yaml")):
            for token in token_re.findall(manifest.read_text(encoding="utf-8")):
                if token in replacements:
                    continue
                if token in quantity_tokens or token.startswith(integer_prefixes):
                    replacements[token] = "1"
                else:
                    replacements[token] = "stub-value"

        plan = handler_module.plan_manifests(str(manifests_dir), replacements)

        # Post-helm files legitimately appear in base's skip list as
        # deferred-to-post-helm; with every gate resolved, nothing may be
        # skipped for an unreplaced placeholder in either phase.
        unresolved_skips = [
            entry
            for phase in ("base", "post-helm")
            for entry in plan["skipped"][phase]
            if not entry.endswith(":deferred-to-post-helm")
        ]
        assert not unresolved_skips, (
            f"with every gate resolved, no file may be skipped: {unresolved_skips}"
        )
        planned_kinds = {
            item["kind"] for phase in ("base", "post-helm") for item in plan["phases"][phase]
        }
        assert {"ClusterQueue", "LocalQueue", "ResourceFlavor", "NetworkPolicy"} <= planned_kinds

    def test_every_supported_kind_has_an_apply_dispatch_branch(self, handler_module):
        """_SUPPORTED_MANIFEST_KINDS and the apply loop must agree exactly.

        Regression: the 2026-08-14 shakeout deploy admitted the shipped
        torch-distributed ClusterTrainingRuntime at planning time (the kind
        was in _SUPPORTED_MANIFEST_KINDS) but the apply loop had no dispatch
        branch for it, so every applier pass died on the defensive
        "Planner admitted unsupported kind" ValueError — hours into a live
        run. Extract every dispatch route from the handler source (literal
        ``kind == "X"`` branches, ``kind in ("X", "Y")`` tuples, and the
        gateway/queueing custom-object maps) and require set equality in
        both directions, so a kind added to planning without an apply
        branch (or a dead branch for an unplannable kind) fails here
        instead of mid-deploy.
        """
        source = (
            Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple" / "handler.py"
        ).read_text(encoding="utf-8")
        dispatched = set(re.findall(r'(?:el)?if kind == "([A-Za-z0-9]+)"', source))
        for group in re.findall(r"(?:el)?if kind in \(([^)]*)\)", source):
            dispatched.update(re.findall(r'"([A-Za-z0-9]+)"', group))
        # Table-driven branches: `elif kind in _GATEWAY_CUSTOM_OBJECTS or
        # kind in _QUEUEING_CUSTOM_OBJECTS`. Require the branch to exist so
        # the maps' keys genuinely reach the apply loop.
        assert re.search(
            r"kind in _GATEWAY_CUSTOM_OBJECTS or kind in _QUEUEING_CUSTOM_OBJECTS", source
        ), "table-driven custom-object dispatch branch disappeared; update this test"
        dispatched.update(handler_module._GATEWAY_CUSTOM_OBJECTS)
        dispatched.update(handler_module._QUEUEING_CUSTOM_OBJECTS)

        supported = set(handler_module._SUPPORTED_MANIFEST_KINDS)
        assert supported - dispatched == set(), (
            "kinds admitted by planning but with no apply dispatch branch "
            f"(would die on the defensive ValueError mid-deploy): {sorted(supported - dispatched)}"
        )
        assert dispatched - supported == set(), (
            "apply branches exist for kinds planning rejects (dead code or a "
            f"missing _SUPPORTED_MANIFEST_KINDS entry): {sorted(dispatched - supported)}"
        )

    def test_rejects_duplicate_identity_across_phases(self, handler_module, tmp_path):
        manifest = yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "duplicate", "namespace": "demo"},
            }
        )
        (tmp_path / "10-base.yaml").write_text(manifest)
        (tmp_path / "post-helm-duplicate.yaml").write_text(manifest)

        with pytest.raises(ValueError, match="duplicate.*first declared"):
            handler_module.plan_manifests(str(tmp_path), {})

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            (
                "apiVersion: example.io/v1\nkind: Mystery\nmetadata:\n  name: mystery\n",
                "unsupported kind 'Mystery'",
            ),
            ("apiVersion: v1\nkind: ConfigMap\nmetadata: {}\n", "metadata.name"),
            ("- apiVersion: v1\n  kind: ConfigMap\n", "document must be a mapping"),
            ("apiVersion: v1\nkind: [broken\n", "invalid YAML"),
        ],
    )
    def test_rejects_unsupported_or_malformed_documents(
        self, handler_module, tmp_path, content, message
    ):
        (tmp_path / "10-invalid.yaml").write_text(content)

        with pytest.raises(ValueError, match=message):
            handler_module.plan_manifests(str(tmp_path), {})

    def test_apply_returns_exact_planned_count_and_inventory(self, handler_module, tmp_path):
        documents = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": "monitoring"},
            }
            for name in ("one", "two")
        ]
        (tmp_path / "post-helm-auto.yaml").write_text(yaml.safe_dump_all(documents))

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            mock_client.CoreV1Api.return_value = MagicMock()
            mock_client.AppsV1Api.return_value = MagicMock()
            mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
            mock_client.NetworkingV1Api.return_value = MagicMock()
            mock_client.CustomObjectsApi.return_value = MagicMock()
            result = handler_module.apply_manifests(
                "cluster", "us-east-1", str(tmp_path), {}, post_helm=True
            )

        assert result["AppliedCount"] == result["ExpectedCount"] == 2
        assert [item["name"] for item in result["ExpectedResources"]] == ["one", "two"]
        assert all(item["phase"] == "post-helm" for item in result["ExpectedResources"])


def _validate_with_fake_dynamic(
    handler_module,
    manifests_dir,
    live_objects,
    *,
    endpoint_slice_items=None,
    deployment_token="deployment-token",
):
    """Run validation against an in-memory DynamicClient object inventory."""
    dynamic_client = MagicMock()
    discovered = {}

    def discover(*, api_version, kind):
        key = (api_version, kind)
        if key in discovered:
            return discovered[key]
        resource = MagicMock()

        def get_object(**kwargs):
            if kind == "EndpointSlice":
                return {"items": endpoint_slice_items or []}
            identity = (
                api_version,
                kind,
                kwargs.get("namespace", handler_module._CLUSTER_SCOPE),
                kwargs["name"],
            )
            if identity not in live_objects:
                raise handler_module.ApiException(status=404, reason="Not Found")
            return live_objects[identity]

        resource.get.side_effect = get_object
        discovered[key] = resource
        return resource

    dynamic_client.resources.get.side_effect = discover
    with (
        patch.object(handler_module, "configure_k8s_client"),
        patch.object(handler_module.dynamic, "DynamicClient", return_value=dynamic_client),
        patch.object(handler_module.client, "ApiClient", return_value=MagicMock()),
    ):
        result = handler_module.validate_manifests(
            "cluster",
            "us-east-1",
            str(manifests_dir),
            {},
            deployment_token,
        )
    return result, dynamic_client


class TestManifestReadinessValidation:
    """Exact DynamicClient retrieval and per-kind readiness contracts."""

    def test_all_readiness_kinds_and_service_endpoint_are_ready(self, handler_module, tmp_path):
        def document(api_version, kind, name, namespace=None, spec=None):
            metadata = {"name": name}
            if namespace:
                metadata["namespace"] = namespace
            result = {"apiVersion": api_version, "kind": kind, "metadata": metadata}
            if spec is not None:
                result["spec"] = spec
            return result

        base_documents = [
            document("apps/v1", "Deployment", "deployment", "demo", {"replicas": 2}),
            document("apps/v1", "StatefulSet", "stateful", "demo", {"replicas": 1}),
            document("apps/v1", "DaemonSet", "daemon", "demo"),
            document("batch/v1", "Job", "job", "demo"),
            document("v1", "Pod", "pod", "demo"),
            document("v1", "PersistentVolumeClaim", "claim", "demo"),
            document("v1", "PersistentVolume", "volume"),
            document(
                "apiextensions.k8s.io/v1",
                "CustomResourceDefinition",
                "widgets.example.io",
            ),
            document("apiregistration.k8s.io/v1", "APIService", "v1.example.io"),
            document("autoscaling/v2", "HorizontalPodAutoscaler", "hpa", "demo"),
            document("policy/v1", "PodDisruptionBudget", "pdb", "demo"),
            document("karpenter.sh/v1", "NodePool", "node-pool"),
            document("karpenter.k8s.aws/v1", "EC2NodeClass", "node-class"),
            document("keda.sh/v1alpha1", "ScaledJob", "scaled-job", "demo"),
            document("keda.sh/v1alpha1", "ScaledObject", "scaled-object", "demo"),
            document(
                "v1",
                "Service",
                "service",
                "demo",
                {"selector": {"app": "ready"}},
            ),
        ]
        post_document = document("v1", "ConfigMap", "post-static", "monitoring")
        (tmp_path / "10-all-ready.yaml").write_text(yaml.safe_dump_all(base_documents))
        (tmp_path / "post-helm-static.yml").write_text(yaml.safe_dump(post_document))

        cluster = handler_module._CLUSTER_SCOPE
        live_objects = {
            ("apps/v1", "Deployment", "demo", "deployment"): {
                "metadata": {"generation": 3},
                "spec": {"replicas": 2},
                "status": {
                    "observedGeneration": 3,
                    "replicas": 2,
                    "updatedReplicas": 2,
                    "readyReplicas": 2,
                    "availableReplicas": 2,
                },
            },
            ("apps/v1", "StatefulSet", "demo", "stateful"): {
                "metadata": {"generation": 2},
                "spec": {"replicas": 1},
                "status": {
                    "observedGeneration": 2,
                    "currentReplicas": 1,
                    "updatedReplicas": 1,
                    "readyReplicas": 1,
                },
            },
            ("apps/v1", "DaemonSet", "demo", "daemon"): {
                "metadata": {"generation": 1},
                "status": {
                    "observedGeneration": 1,
                    "desiredNumberScheduled": 0,
                    "currentNumberScheduled": 0,
                    "updatedNumberScheduled": 0,
                    "numberReady": 0,
                    "numberAvailable": 0,
                    "numberMisscheduled": 0,
                },
            },
            ("batch/v1", "Job", "demo", "job"): {
                "metadata": {},
                "status": {"conditions": [{"type": "Complete", "status": "True"}]},
            },
            ("v1", "Pod", "demo", "pod"): {
                "metadata": {},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
            ("v1", "PersistentVolumeClaim", "demo", "claim"): {
                "metadata": {},
                "status": {"phase": "Bound"},
            },
            ("v1", "PersistentVolume", cluster, "volume"): {
                "metadata": {},
                "status": {"phase": "Available"},
            },
            (
                "apiextensions.k8s.io/v1",
                "CustomResourceDefinition",
                cluster,
                "widgets.example.io",
            ): {
                "metadata": {},
                "status": {"conditions": [{"type": "Established", "status": "True"}]},
            },
            ("apiregistration.k8s.io/v1", "APIService", cluster, "v1.example.io"): {
                "metadata": {},
                "status": {"conditions": [{"type": "Available", "status": "True"}]},
            },
            ("autoscaling/v2", "HorizontalPodAutoscaler", "demo", "hpa"): {
                "metadata": {},
                "status": {
                    "conditions": [
                        {"type": "AbleToScale", "status": "True"},
                        {"type": "ScalingActive", "status": "True"},
                    ]
                },
            },
            ("policy/v1", "PodDisruptionBudget", "demo", "pdb"): {
                "metadata": {"generation": 1},
                "status": {
                    "observedGeneration": 1,
                    "currentHealthy": 2,
                    "desiredHealthy": 2,
                },
            },
            ("karpenter.sh/v1", "NodePool", cluster, "node-pool"): {
                "metadata": {},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
            ("karpenter.k8s.aws/v1", "EC2NodeClass", cluster, "node-class"): {
                "metadata": {},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
            ("keda.sh/v1alpha1", "ScaledJob", "demo", "scaled-job"): {
                "metadata": {},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
            ("keda.sh/v1alpha1", "ScaledObject", "demo", "scaled-object"): {
                "metadata": {},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
            ("v1", "Service", "demo", "service"): {"metadata": {}},
            ("v1", "ConfigMap", "monitoring", "post-static"): {"metadata": {}},
        }
        endpoint_slices = [
            {
                "endpoints": [
                    {"conditions": {"ready": False, "terminating": False}},
                    {"conditions": {"ready": True, "terminating": False}},
                ]
            }
        ]

        result, _ = _validate_with_fake_dynamic(
            handler_module,
            tmp_path,
            live_objects,
            endpoint_slice_items=endpoint_slices,
            deployment_token="deploy-123",
        )

        assert result["DeploymentToken"] == "deploy-123"
        assert result["ExpectedCount"] == result["ValidatedCount"] == 17
        assert result["PhaseCounts"] == {
            "base": {"ExpectedCount": 16, "ValidatedCount": 16},
            "post-helm": {"ExpectedCount": 1, "ValidatedCount": 1},
        }
        assert result["ExpectedResources"] == result["ValidatedResources"]

    def test_missing_exact_object_fails_with_identity(self, handler_module, tmp_path):
        (tmp_path / "10-required.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: absent\n  namespace: demo\n"
        )

        with pytest.raises(RuntimeError, match=r"ConfigMap/demo/absent.*API error 404"):
            _validate_with_fake_dynamic(handler_module, tmp_path, {})

    def test_unready_controller_reports_replica_evidence(self, handler_module, tmp_path):
        (tmp_path / "10-deployment.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {"name": "api", "namespace": "demo"},
                    "spec": {"replicas": 2},
                }
            )
        )
        live_objects = {
            ("apps/v1", "Deployment", "demo", "api"): {
                "metadata": {"generation": 4},
                "spec": {"replicas": 2},
                "status": {
                    "observedGeneration": 4,
                    "replicas": 2,
                    "updatedReplicas": 2,
                    "readyReplicas": 1,
                    "availableReplicas": 1,
                },
            }
        }

        with pytest.raises(RuntimeError, match=r"replicas not converged.*readyReplicas=1"):
            _validate_with_fake_dynamic(handler_module, tmp_path, live_objects)

    def test_selector_service_requires_ready_nonterminating_endpoint(
        self, handler_module, tmp_path
    ):
        (tmp_path / "10-service.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": "api", "namespace": "demo"},
                    "spec": {"selector": {"app": "api"}},
                }
            )
        )
        live_objects = {("v1", "Service", "demo", "api"): {"metadata": {}}}
        endpoint_slices = [{"endpoints": [{"conditions": {"ready": True, "terminating": True}}]}]

        with pytest.raises(RuntimeError, match="no ready, nonterminating EndpointSlice endpoint"):
            _validate_with_fake_dynamic(
                handler_module,
                tmp_path,
                live_objects,
                endpoint_slice_items=endpoint_slices,
            )

    def test_allow_empty_endpoints_annotation_accepts_an_endpointless_service(
        self, handler_module, tmp_path
    ):
        """Accelerator-backed Services are ready by existence on GPU-less clusters.

        Regression: the DCGM exporter Service selects a DaemonSet that only
        schedules onto GPU nodes, so a fresh deployment failed readiness with
        zero endpoints until the first GPU node existed.
        """
        (tmp_path / "10-service.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {
                        "name": "dcgm-exporter",
                        "namespace": "kube-system",
                        "annotations": {"gco.io/allow-empty-endpoints": "true"},
                    },
                    "spec": {"selector": {"app": "dcgm-exporter"}},
                }
            )
        )
        live_objects = {("v1", "Service", "kube-system", "dcgm-exporter"): {"metadata": {}}}

        result, _ = _validate_with_fake_dynamic(
            handler_module,
            tmp_path,
            live_objects,
            endpoint_slice_items=[],
        )

        assert result["ExpectedCount"] == result["ValidatedCount"] == 1

    def test_unresolved_optional_resources_are_not_expected(self, handler_module, tmp_path):
        (tmp_path / "10-required.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: required\n  namespace: demo\n"
        )
        (tmp_path / "20-optional.yaml").write_text(
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n"
            "  name: optional\n  namespace: demo\nspec:\n"
            "  template:\n    metadata:\n      annotations:\n"
            "        enabled: '{{OPTIONAL_CONTROLLER_ENABLED}}'\n"
        )
        live_objects = {("v1", "ConfigMap", "demo", "required"): {"metadata": {}}}

        result, dynamic_client = _validate_with_fake_dynamic(handler_module, tmp_path, live_objects)

        assert result["ExpectedCount"] == result["ValidatedCount"] == 1
        assert [item["name"] for item in result["ExpectedResources"]] == ["required"]
        discovered_kinds = {
            call.kwargs["kind"] for call in dynamic_client.resources.get.call_args_list
        }
        assert "Deployment" not in discovered_kinds

    @pytest.mark.parametrize(
        "resource",
        [
            {
                "metadata": {"generation": 1},
                "spec": {"replicas": 0},
                "status": {"observedGeneration": 1},
            },
            {
                "metadata": {"generation": 1},
                "spec": {"replicas": 0},
                "status": {"observedGeneration": 1},
            },
            {
                "metadata": {"generation": 1},
                "status": {"observedGeneration": 1, "desiredNumberScheduled": 0},
            },
        ],
        ids=["deployment", "statefulset", "daemonset"],
    )
    def test_zero_desired_controllers_accept_omitted_zero_counters(
        self, handler_module, resource, request
    ):
        kind = {
            "deployment": "Deployment",
            "statefulset": "StatefulSet",
            "daemonset": "DaemonSet",
        }[request.node.callspec.id]
        assert handler_module._resource_readiness_failure(kind, resource) is None

    def test_daemonset_rejects_misscheduled_pods(self, handler_module):
        resource = {
            "metadata": {"generation": 1},
            "status": {
                "observedGeneration": 1,
                "desiredNumberScheduled": 1,
                "currentNumberScheduled": 1,
                "updatedNumberScheduled": 1,
                "numberReady": 1,
                "numberAvailable": 1,
                "numberMisscheduled": 1,
            },
        }

        failure = handler_module._resource_readiness_failure("DaemonSet", resource)

        assert failure == "pods are misscheduled (numberMisscheduled=1)"


class TestManifestValidationTaskEvidence:
    """The task action preserves deployment-token and phase-status evidence."""

    def test_records_validated_status_and_returns_deployment_token(self, handler_module):
        event = {
            "Action": "validate_manifests",
            "ClusterName": "cluster",
            "Region": "us-east-1",
            "ImageReplacements": {"{{IMAGE}}": "image"},
            "DeploymentToken": "deploy-token-456",
        }
        validation_result = {
            "DeploymentToken": "deploy-token-456",
            "ExpectedCount": 3,
            "ValidatedCount": 3,
            "PhaseCounts": {},
            "ExpectedResources": [],
            "ValidatedResources": [],
        }
        with (
            patch.object(
                handler_module,
                "validate_manifests",
                return_value=validation_result,
            ) as mock_validate,
            patch.object(handler_module, "_record_phase_status") as mock_status,
        ):
            result = handler_module.handle_task(event)

        assert result["DeploymentToken"] == "deploy-token-456"
        assert mock_validate.call_args.args[3] == {"{{IMAGE}}": "image"}
        assert mock_validate.call_args.args[4] == "deploy-token-456"
        phase, status, message = mock_status.call_args.args
        assert (phase, status) == ("manifest-validation", "validated")
        assert "deploy-token-456" in message
        assert "validated=3 expected=3" in message

    def test_records_failed_status_before_validation_error_escapes(self, handler_module):
        event = {
            "Action": "validate_manifests",
            "ClusterName": "cluster",
            "Region": "us-east-1",
            "DeploymentToken": "failed-token",
        }
        with (
            patch.object(
                handler_module,
                "validate_manifests",
                side_effect=RuntimeError("Deployment/demo/api is not ready"),
            ),
            patch.object(handler_module, "_record_phase_status") as mock_status,
            pytest.raises(RuntimeError, match="not ready"),
        ):
            handler_module.handle_task(event)

        assert mock_status.call_args.args == (
            "manifest-validation",
            "failed",
            "token=failed-token Deployment/demo/api is not ready",
        )


def _parse_manifest_documents(manifest: Path) -> list[dict]:
    """Parse a repo manifest, substituting unresolved {{PLACEHOLDER}} tokens.

    The applier replaces template variables before parsing; mirror that so
    templated manifests stay parseable for these consistency checks.
    """
    import re as _re

    text = _re.sub(r"\{\{[A-Z0-9_]+\}\}", "placeholder", manifest.read_text(encoding="utf-8"))
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


class TestGatewayCustomObjectMapConsistency:
    """The applier's GVK map must agree with the manifests it applies.

    The 2026-08 Gateway API migration moved post-helm-gateway.yaml to
    gateway.k8s.aws/v1 while _GATEWAY_CUSTOM_OBJECTS still said v1beta1 —
    nothing failed until a live deploy's kubectl apply. This pins the two
    surfaces together: every gateway-family resource in the manifests
    directory must resolve to exactly the (group, version) the applier's
    map will use to create, patch, and tear it down.
    """

    def test_manifest_gateway_api_versions_match_the_applier_map(self, handler_module) -> None:
        manifests_dir = (
            Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple" / "manifests"
        )
        gateway_groups = {
            group for group, _v, _p, _s in handler_module._GATEWAY_CUSTOM_OBJECTS.values()
        }

        seen: list[tuple[str, str, str]] = []
        mismatches: list[str] = []
        for manifest in sorted(manifests_dir.glob("*.yaml")):
            for doc in _parse_manifest_documents(manifest):
                api_version = str(doc.get("apiVersion", ""))
                kind = str(doc.get("kind", ""))
                group, _, version = api_version.partition("/")
                if group not in gateway_groups:
                    continue
                seen.append((manifest.name, kind, api_version))
                mapped = handler_module._GATEWAY_CUSTOM_OBJECTS.get(kind)
                if mapped is None:
                    mismatches.append(
                        f"{manifest.name}: {kind} ({api_version}) has no "
                        "_GATEWAY_CUSTOM_OBJECTS entry"
                    )
                    continue
                mapped_group, mapped_version = mapped[0], mapped[1]
                if (group, version) != (mapped_group, mapped_version):
                    mismatches.append(
                        f"{manifest.name}: {kind} is {api_version} but "
                        f"_GATEWAY_CUSTOM_OBJECTS maps {mapped_group}/{mapped_version} — "
                        "the applier would create/patch/delete a different API version "
                        "than the manifest declares"
                    )

        assert seen, "expected gateway-family resources in the manifests directory"
        assert not mismatches, "\n".join(mismatches)

    def test_every_mapped_gateway_kind_appears_in_a_manifest(self, handler_module) -> None:
        # The inverse direction: a map entry nothing uses is dead weight that
        # can silently rot; force it to be pruned or exercised.
        manifests_dir = (
            Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple" / "manifests"
        )
        used_kinds = set()
        gateway_groups = {
            group for group, _v, _p, _s in handler_module._GATEWAY_CUSTOM_OBJECTS.values()
        }
        for manifest in sorted(manifests_dir.glob("*.yaml")):
            for doc in _parse_manifest_documents(manifest):
                group = str(doc.get("apiVersion", "")).partition("/")[0]
                if group in gateway_groups:
                    used_kinds.add(str(doc.get("kind", "")))

        unused = set(handler_module._GATEWAY_CUSTOM_OBJECTS) - used_kinds
        assert not unused, f"_GATEWAY_CUSTOM_OBJECTS maps kinds no manifest uses: {sorted(unused)}"


class TestQueueingCustomObjectMapConsistency:
    """_QUEUEING_CUSTOM_OBJECTS must agree with the manifests, both directions.

    Same regression class the gateway map guards against: an applier map
    whose (group, version) drifts from the manifests fails only on a live
    deploy's apply. The Kueue default-queue topology gets the identical pin.
    """

    def test_manifest_kueue_api_versions_match_the_applier_map(self, handler_module) -> None:
        manifests_dir = (
            Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple" / "manifests"
        )
        queueing_groups = {
            group for group, _v, _p, _s in handler_module._QUEUEING_CUSTOM_OBJECTS.values()
        }

        seen: list[tuple[str, str, str]] = []
        mismatches: list[str] = []
        for manifest in sorted(manifests_dir.glob("*.yaml")):
            for doc in _parse_manifest_documents(manifest):
                api_version = str(doc.get("apiVersion", ""))
                kind = str(doc.get("kind", ""))
                group, _, version = api_version.partition("/")
                if group not in queueing_groups:
                    continue
                seen.append((manifest.name, kind, api_version))
                mapped = handler_module._QUEUEING_CUSTOM_OBJECTS.get(kind)
                if mapped is None:
                    mismatches.append(
                        f"{manifest.name}: {kind} ({api_version}) has no "
                        "_QUEUEING_CUSTOM_OBJECTS entry"
                    )
                    continue
                if (group, version) != (mapped[0], mapped[1]):
                    mismatches.append(
                        f"{manifest.name}: {kind} is {api_version} but "
                        f"_QUEUEING_CUSTOM_OBJECTS maps {mapped[0]}/{mapped[1]}"
                    )
        assert seen, "expected kueue queue resources in the manifests directory"
        assert not mismatches, "\n".join(mismatches)

    def test_every_mapped_queueing_kind_appears_in_a_manifest(self, handler_module) -> None:
        manifests_dir = (
            Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple" / "manifests"
        )
        queueing_groups = {
            group for group, _v, _p, _s in handler_module._QUEUEING_CUSTOM_OBJECTS.values()
        }
        used_kinds = set()
        for manifest in sorted(manifests_dir.glob("*.yaml")):
            for doc in _parse_manifest_documents(manifest):
                group = str(doc.get("apiVersion", "")).partition("/")[0]
                if group in queueing_groups:
                    used_kinds.add(str(doc.get("kind", "")))
        unused = set(handler_module._QUEUEING_CUSTOM_OBJECTS) - used_kinds
        assert not unused, f"_QUEUEING_CUSTOM_OBJECTS maps kinds no manifest uses: {sorted(unused)}"

    def test_the_two_custom_object_maps_never_overlap(self, handler_module) -> None:
        # The apply dispatch consults the gateway map first; an overlapping
        # kind would silently take the gateway (group, version) and its
        # ALB-finalizer teardown semantics.
        overlap = set(handler_module._GATEWAY_CUSTOM_OBJECTS) & set(
            handler_module._QUEUEING_CUSTOM_OBJECTS
        )
        assert not overlap, f"custom-object maps overlap on kinds: {sorted(overlap)}"

    def test_kueue_prune_inventory_matches_the_gated_manifest(self, handler_module) -> None:
        """Every object in the gated queue manifest is pruned on disable."""
        manifests_dir = (
            Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple" / "manifests"
        )
        gated = manifests_dir / "post-helm-kueue-default-queues.yaml"
        expected = {
            (str(doc.get("apiVersion")), str(doc.get("kind")), str(doc["metadata"]["name"]))
            for doc in _parse_manifest_documents(gated)
        }
        inventory = handler_module._FEATURE_RESOURCE_INVENTORY[("{{KUEUE_ENABLED}}", True)]
        pruned = {(api_version, kind, name) for api_version, kind, _ns, name in inventory}
        assert pruned == expected

    def test_slurm_prune_inventory_matches_the_gated_manifest(self, handler_module) -> None:
        manifests_dir = (
            Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple" / "manifests"
        )
        gated = manifests_dir / "post-helm-slurm-network.yaml"
        expected = {
            (str(doc.get("apiVersion")), str(doc.get("kind")), str(doc["metadata"]["name"]))
            for doc in _parse_manifest_documents(gated)
        }
        inventory = handler_module._FEATURE_RESOURCE_INVENTORY[("{{SLURM_ENABLED}}", True)]
        pruned = {(api_version, kind, name) for api_version, kind, _ns, name in inventory}
        assert pruned == expected

    def test_vector_store_prune_inventory_matches_the_gated_manifest(self, handler_module) -> None:
        """Every ConfigMap in the gated vector-store manifest is pruned on disable.

        Namespace-inclusive comparison: the same ConfigMap name exists in
        three namespaces, so a 3-tuple check could pass with a missing or
        misplaced namespace entry.
        """
        manifests_dir = (
            Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple" / "manifests"
        )
        gated = manifests_dir / "26-storage-vector-store.yaml"
        expected = {
            (
                str(doc.get("apiVersion")),
                str(doc.get("kind")),
                str(doc["metadata"]["namespace"]),
                str(doc["metadata"]["name"]),
            )
            for doc in _parse_manifest_documents(gated)
        }
        inventory = handler_module._FEATURE_RESOURCE_INVENTORY[
            ("{{VECTOR_STORE_TABLE_NAME}}", False)
        ]
        pruned = {(api_version, kind, str(ns), name) for api_version, kind, ns, name in inventory}
        assert pruned == expected
