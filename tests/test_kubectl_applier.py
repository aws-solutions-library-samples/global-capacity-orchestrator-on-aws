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

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        assert pod_spec["containers"][0]["lifecycle"]["preStop"]["exec"]["command"] == [
            "/bin/sh",
            "-c",
            "sleep 10",
        ]
        assert pdb["spec"]["minAvailable"] == 2
        assert pdb["spec"]["selector"]["matchLabels"] == {"app": "inference-proxy"}

    def test_ingress_deregistration_covers_stream_drain(self):
        """ALB draining lasts at least as long as Uvicorn's maximum stream drain."""
        ingress_path = (
            Path(__file__).parent.parent
            / "lambda"
            / "kubectl-applier-simple"
            / "manifests"
            / "11-ingress.yaml"
        )
        ingress = yaml.safe_load(ingress_path.read_text())
        annotations = ingress["metadata"]["annotations"]
        assert annotations["alb.ingress.kubernetes.io/target-group-attributes"] == (
            "deregistration_delay.timeout_seconds=900"
        )

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
