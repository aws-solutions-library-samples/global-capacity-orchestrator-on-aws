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

import base64
import json
import logging
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml


def _fully_enabled_replacements(manifests_dir: Path) -> dict[str, str]:
    """Stub every feature-gate placeholder in the real manifests directory.

    Shared by the planner tests that must run against the shipped manifests
    with every optional feature resolved: any remaining ``{{TOKEN}}`` would
    gate its whole file out of the plan and silently exempt it from the
    planner's invariants.
    """
    token_re = re.compile(r"\{\{[A-Za-z0-9_]+\}\}")
    quantity_tokens = {
        "{{INFERENCE_PROXY_TLS_CPU_REQUEST}}",
        "{{QUOTA_MAX_CPU}}",
        "{{QUOTA_MAX_MEMORY}}",
        "{{QUOTA_MAX_GPU}}",
    }
    integer_tokens = {"{{INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION}}"}
    integer_prefixes = ("{{QP_", "{{LIMIT_", "{{QUOTA_MAX_PODS}}")
    replacements: dict[str, str] = {
        "{{VPC_ENDPOINT_CIDR_BLOCKS}}": '- ipBlock:\n            cidr: "10.0.0.0/16"',
    }
    for manifest in sorted(manifests_dir.glob("*.yaml")):
        for token in token_re.findall(manifest.read_text(encoding="utf-8")):
            if token in replacements:
                continue
            if (
                token in quantity_tokens
                or token in integer_tokens
                or token.startswith(integer_prefixes)
            ):
                replacements[token] = "1"
            else:
                replacements[token] = "stub-value"
    return replacements


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


class TestCertManagerCRDs:
    """Issuer and Certificate resources are applied after cert-manager installs its CRDs."""

    @pytest.mark.parametrize(
        ("kind", "plural"),
        (("Issuer", "issuers"), ("Certificate", "certificates")),
    )
    def test_tls_resource_applied_as_namespaced_custom_object(
        self,
        handler_module,
        tmp_path,
        kind: str,
        plural: str,
    ):
        (tmp_path / f"post-helm-{plural}.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": kind,
                    "metadata": {"name": f"test-{plural}", "namespace": "gco-system"},
                    "spec": {"selfSigned": {}} if kind == "Issuer" else {"secretName": "tls"},
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
        args = mock_custom.create_namespaced_custom_object.call_args.args
        assert args[:4] == ("cert-manager.io", "v1", "gco-system", plural)


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


class TestPriorityClassApply:
    """scheduling.k8s.io/v1 PriorityClass is applied via SchedulingV1Api.

    The planning/dispatch lockstep test proves the branch exists; these pin
    its behavior: cluster-scoped create (no namespace argument), patch on 409
    so the mutable fields (description, labels, globalDefault) converge on
    re-apply, and a loud per-resource failure when the patch is rejected —
    an attempted change to the immutable ``value`` must never silently keep
    the old priority.
    """

    @staticmethod
    def _write_manifest(tmp_path):
        (tmp_path / "05-priority-classes.yaml").write_text(
            yaml.dump(
                {
                    "apiVersion": "scheduling.k8s.io/v1",
                    "kind": "PriorityClass",
                    "metadata": {"name": "gco-platform-critical"},
                    "value": 1000000,
                    "globalDefault": False,
                    "preemptionPolicy": "PreemptLowerPriority",
                    "description": "GCO platform services",
                }
            )
        )

    @staticmethod
    def _client_mocks(mock_client):
        mock_client.CoreV1Api.return_value = MagicMock()
        mock_client.AppsV1Api.return_value = MagicMock()
        mock_client.RbacAuthorizationV1Api.return_value = MagicMock()
        mock_client.NetworkingV1Api.return_value = MagicMock()
        mock_client.CustomObjectsApi.return_value = MagicMock()
        scheduling = MagicMock()
        mock_client.SchedulingV1Api.return_value = scheduling
        return scheduling

    def test_priority_class_created_cluster_scoped(self, handler_module, tmp_path):
        self._write_manifest(tmp_path)
        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            scheduling = self._client_mocks(mock_client)
            result = handler_module.apply_manifests("c", "us-east-1", str(tmp_path), {})
        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0
        scheduling.create_priority_class.assert_called_once()
        create_kwargs = scheduling.create_priority_class.call_args.kwargs
        assert create_kwargs["body"]["metadata"]["name"] == "gco-platform-critical"
        # Cluster-scoped: no namespace rides the call.
        assert "namespace" not in create_kwargs
        scheduling.patch_priority_class.assert_not_called()

    def test_priority_class_patched_on_conflict(self, handler_module, tmp_path):
        """A 409 on create falls back to patch (idempotent re-apply)."""
        from kubernetes.client.rest import ApiException

        self._write_manifest(tmp_path)
        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            scheduling = self._client_mocks(mock_client)
            scheduling.create_priority_class.side_effect = ApiException(status=409)
            result = handler_module.apply_manifests("c", "us-east-1", str(tmp_path), {})
        assert result["AppliedCount"] == 1
        assert result["FailedCount"] == 0
        scheduling.patch_priority_class.assert_called_once()
        patch_args = scheduling.patch_priority_class.call_args
        assert patch_args.args[0] == "gco-platform-critical"
        assert patch_args.kwargs["body"]["value"] == 1000000

    def test_rejected_patch_fails_loudly(self, handler_module, tmp_path):
        """An immutable-field change (422 on patch) is a per-resource failure.

        The manifest header promises a changed ``value`` fails loudly instead
        of silently keeping the old priority; the apply loop's per-resource
        isolation must record the failure, not swallow it.
        """
        from kubernetes.client.rest import ApiException

        self._write_manifest(tmp_path)
        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            scheduling = self._client_mocks(mock_client)
            scheduling.create_priority_class.side_effect = ApiException(status=409)
            scheduling.patch_priority_class.side_effect = ApiException(status=422)
            result = handler_module.apply_manifests("c", "us-east-1", str(tmp_path), {})
        assert result["FailedCount"] == 1
        assert result["AppliedCount"] == 0

    def test_non_conflict_create_error_fails_loudly(self, handler_module, tmp_path):
        """Only 409 routes to patch; any other create failure is recorded."""
        from kubernetes.client.rest import ApiException

        self._write_manifest(tmp_path)
        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch("handler.client") as mock_client,
        ):
            scheduling = self._client_mocks(mock_client)
            scheduling.create_priority_class.side_effect = ApiException(status=403)
            result = handler_module.apply_manifests("c", "us-east-1", str(tmp_path), {})
        assert result["FailedCount"] == 1
        scheduling.patch_priority_class.assert_not_called()


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

    def test_main_pass_avoids_duplicate_gco_rollout_and_restarts_csi_controllers(
        self, handler_module, tmp_path
    ):
        """GCO rolls via manifest annotation; CSI controllers still restart for IRSA."""
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

        deploy_calls = {
            (call.args[0], tuple(call.args[1])) for call in mock_restart_deploy.call_args_list
        }
        assert not any(namespace == "gco-system" for namespace, _names in deploy_calls)
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
            self._not_found(handler_module) for _ in range(7)
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
            "DeletedCount": 8,
            "Deleted": [
                "HTTPRoute/gco-system/gco-routes",
                "Gateway/gco-system/gco-gateway",
                "LoadBalancerConfiguration/gco-system/gco-gateway-load-balancer",
                "TargetGroupConfiguration/gco-system/gco-health-monitor-target-group",
                "TargetGroupConfiguration/gco-system/gco-manifest-processor-target-group",
                "TargetGroupConfiguration/gco-system/gco-inference-proxy-target-group",
                "TargetGroupConfiguration/gco-system/gco-default-target-group",
                "GatewayClass/gco-aws-alb",
            ],
        }
        expected_namespaced_calls = [
            "delete_namespaced_custom_object",
            "get_namespaced_custom_object",
        ] * 7
        assert [entry[0] for entry in custom_api.mock_calls] == [
            *expected_namespaced_calls,
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
            self._not_found(handler_module) for _ in range(7)
        ]
        custom_api.delete_cluster_custom_object.side_effect = self._not_found(handler_module)

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch.object(handler_module.client, "CustomObjectsApi", return_value=custom_api),
            patch.object(handler_module.client, "V1DeleteOptions"),
        ):
            result = handler_module._delete_gateway_resources("cluster", "us-east-1")

        assert result["DeletedCount"] == 8
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
        deleted = {"status": "deleted", "DeletedCount": 8, "Deleted": ["eight objects"]}
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
        record.assert_called_once_with("gateway-teardown", "deleted", "deleted=8")


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
        content = (
            manifest_path.read_text()
            .replace(
                "{{INFERENCE_PROXY_IMAGE}}",
                "example.invalid/inference-proxy:test",
            )
            .replace("{{INFERENCE_PROXY_TLS_CPU_REQUEST}}", "100m")
            .replace("{{INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION}}", "70")
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
                "type": "ContainerResource",
                "containerResource": {
                    "name": "cpu",
                    "container": "inference-proxy",
                    "target": {"type": "Utilization", "averageUtilization": 70},
                },
            },
            {
                "type": "ContainerResource",
                "containerResource": {
                    "name": "memory",
                    "container": "inference-proxy",
                    "target": {"type": "Utilization", "averageUtilization": 80},
                },
            },
            {
                "type": "ContainerResource",
                "containerResource": {
                    "name": "cpu",
                    "container": "api-tls-proxy",
                    "target": {"type": "Utilization", "averageUtilization": 70},
                },
            },
        ]
        assert hpa["spec"]["behavior"] == {
            "scaleUp": {
                "stabilizationWindowSeconds": 0,
                "selectPolicy": "Max",
                "policies": [
                    {"type": "Percent", "value": 100, "periodSeconds": 60},
                    {"type": "Pods", "value": 4, "periodSeconds": 60},
                ],
            },
            "scaleDown": {
                "stabilizationWindowSeconds": 900,
                "selectPolicy": "Min",
                "policies": [
                    {"type": "Percent", "value": 25, "periodSeconds": 60},
                    {"type": "Pods", "value": 1, "periodSeconds": 60},
                ],
            },
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
        containers = {container["name"]: container for container in pod_spec["containers"]}
        assert containers["inference-proxy"]["resources"] == {
            "requests": {"cpu": "250m", "memory": "256Mi"},
            "limits": {"cpu": "1000m", "memory": "1Gi"},
        }
        assert containers["api-tls-proxy"]["resources"] == {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "250m", "memory": "256Mi"},
        }
        assert (
            containers["api-tls-proxy"]["lifecycle"]["preStop"]
            == containers["inference-proxy"]["lifecycle"]["preStop"]
        )
        # One disruption at a time however far the HPA has scaled the proxy;
        # minAvailable: 2 would have let 8 of 10 replicas be evicted at once.
        assert pdb["spec"] == {
            "maxUnavailable": 1,
            "selector": {"matchLabels": {"app": "inference-proxy"}},
        }

    @pytest.mark.parametrize(
        "missing_token",
        [
            "{{INFERENCE_PROXY_TLS_CPU_REQUEST}}",
            "{{INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION}}",
        ],
    )
    def test_missing_tls_autoscaling_replacement_skips_complete_manifest(
        self, handler_module, tmp_path, missing_token
    ):
        """One unresolved required token gates every document in the file."""
        source = (
            Path(__file__).parent.parent
            / "lambda"
            / "kubectl-applier-simple"
            / "manifests"
            / "33-inference-proxy.yaml"
        ).read_text(encoding="utf-8")
        (tmp_path / "33-inference-proxy.yaml").write_text(source, encoding="utf-8")
        token_re = re.compile(r"\{\{[A-Z0-9_]+\}\}")
        replacements = dict.fromkeys(token_re.findall(source), "test-value")
        replacements["{{INFERENCE_PROXY_TLS_CPU_REQUEST}}"] = "100m"
        replacements["{{INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION}}"] = "70"
        del replacements[missing_token]

        plan = handler_module.plan_manifests(str(tmp_path), replacements)

        assert plan["phases"]["base"] == []
        assert plan["skipped"]["base"] == ["33-inference-proxy.yaml:unreplaced-placeholders"]
        assert plan["featureGates"]["base"] == [missing_token]

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
        replacements = _fully_enabled_replacements(manifests_dir)

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

    # ── Cross-phase ServiceAccount/token-projection invariant ────────────
    #
    # Shared synthetic fixtures: a hardened ServiceAccount (base), an RBAC
    # binding declaring it talks to the Kubernetes API (base), and a
    # post-Helm workload running as it — the exact shape of the SQS
    # queue-processor pairing whose split-release deploy caused the outage.

    @staticmethod
    def _write_hardened_sa_fixture(tmp_path, *, rbac_bound=True, project_token=False):
        (tmp_path / "01-sa.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {"name": "gco-test-sa", "namespace": "gco-system"},
                    "automountServiceAccountToken": False,
                }
            )
        )
        if rbac_bound:
            (tmp_path / "02-rbac.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "rbac.authorization.k8s.io/v1",
                        "kind": "RoleBinding",
                        "metadata": {"name": "gco-test-binding", "namespace": "gco-system"},
                        "roleRef": {
                            "apiGroup": "rbac.authorization.k8s.io",
                            "kind": "Role",
                            "name": "gco-test-role",
                        },
                        "subjects": [{"kind": "ServiceAccount", "name": "gco-test-sa"}],
                    }
                )
            )
        pod_spec: dict = {
            "serviceAccountName": "gco-test-sa",
            "containers": [{"name": "worker", "image": "python:3.14"}],
        }
        if project_token:
            pod_spec["volumes"] = [
                {
                    "name": "kubernetes-api-token",
                    "projected": {
                        "sources": [
                            {"serviceAccountToken": {"expirationSeconds": 3600, "path": "token"}}
                        ]
                    },
                }
            ]
            pod_spec["containers"][0]["volumeMounts"] = [
                {
                    "name": "kubernetes-api-token",
                    "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
                    "readOnly": True,
                }
            ]
        (tmp_path / "post-helm-worker.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {"name": "worker", "namespace": "gco-system"},
                    "spec": {"template": {"spec": pod_spec}},
                }
            )
        )

    def test_real_manifests_satisfy_the_token_projection_invariant(self, handler_module):
        """The shipped manifests keep every hardened, API-bound SA projected.

        Runs the planner over the real directory with every feature gate
        resolved, so the sqs-queue-processor ScaledJob and both platform
        Deployments are actually planned and subject to the invariant.
        """
        manifests_dir = (
            Path(__file__).parent.parent / "lambda" / "kubectl-applier-simple" / "manifests"
        )
        plan = handler_module.plan_manifests(
            str(manifests_dir), _fully_enabled_replacements(manifests_dir)
        )
        planned_names = {
            (item["kind"], item["name"])
            for phase in ("base", "post-helm")
            for item in plan["phases"][phase]
        }
        # The pairing that caused the outage must be planned (not gated out)
        # for this test to mean anything.
        assert ("ScaledJob", "sqs-queue-processor") in planned_names
        assert ("ServiceAccount", "gco-manifest-processor-sa") in planned_names

    def test_hardened_rbac_bound_sa_without_projection_fails_planning(
        self, handler_module, tmp_path
    ):
        """The outage shape is a planning error naming both objects."""
        self._write_hardened_sa_fixture(tmp_path, rbac_bound=True, project_token=False)

        with pytest.raises(ValueError) as error:
            handler_module.plan_manifests(str(tmp_path), {})

        message = str(error.value)
        assert "Deployment/gco-system/worker" in message
        assert "post-helm-worker.yaml" in message
        assert "'gco-test-sa'" in message
        assert "01-sa.yaml" in message
        assert "/var/run/secrets/kubernetes.io/serviceaccount" in message

    def test_hardened_rbac_bound_sa_with_projection_passes(self, handler_module, tmp_path):
        self._write_hardened_sa_fixture(tmp_path, rbac_bound=True, project_token=True)

        plan = handler_module.plan_manifests(str(tmp_path), {})

        assert [item["name"] for item in plan["phases"]["post-helm"]] == ["worker"]

    def test_hardened_unbound_sa_without_projection_is_exempt(self, handler_module, tmp_path):
        """No RBAC binding means the SA holds no Kubernetes API role.

        Matches the shipped inference-proxy and cost-monitor pods: automount
        is disabled, the pods hold only AWS credentials, and requiring a
        Kubernetes token projection for them would be wrong.
        """
        self._write_hardened_sa_fixture(tmp_path, rbac_bound=False, project_token=False)

        plan = handler_module.plan_manifests(str(tmp_path), {})

        assert [item["name"] for item in plan["phases"]["post-helm"]] == ["worker"]

    def test_unhardened_sa_is_exempt_even_when_rbac_bound(self, handler_module, tmp_path):
        """Automount left on (or unset) needs no projection: pods get tokens."""
        self._write_hardened_sa_fixture(tmp_path, rbac_bound=True, project_token=False)
        (tmp_path / "01-sa.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {"name": "gco-test-sa", "namespace": "gco-system"},
                }
            )
        )

        plan = handler_module.plan_manifests(str(tmp_path), {})

        assert [item["name"] for item in plan["phases"]["post-helm"]] == ["worker"]

    def test_projection_without_matching_mount_still_fails(self, handler_module, tmp_path):
        """A projected volume that no container mounts is not a credential."""
        self._write_hardened_sa_fixture(tmp_path, rbac_bound=True, project_token=True)
        worker = yaml.safe_load((tmp_path / "post-helm-worker.yaml").read_text())
        del worker["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        (tmp_path / "post-helm-worker.yaml").write_text(yaml.safe_dump(worker))

        with pytest.raises(ValueError) as error:
            handler_module.plan_manifests(str(tmp_path), {})

        assert "Deployment/gco-system/worker" in str(error.value)

    def test_cluster_role_binding_subject_requires_explicit_namespace(
        self, handler_module, tmp_path
    ):
        """ClusterRoleBinding subjects bind only via their stated namespace."""
        self._write_hardened_sa_fixture(tmp_path, rbac_bound=False, project_token=False)
        (tmp_path / "02-rbac.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "ClusterRoleBinding",
                    "metadata": {"name": "gco-test-cluster-binding"},
                    "roleRef": {
                        "apiGroup": "rbac.authorization.k8s.io",
                        "kind": "ClusterRole",
                        "name": "gco-test-role",
                    },
                    "subjects": [
                        {
                            "kind": "ServiceAccount",
                            "name": "gco-test-sa",
                            "namespace": "gco-system",
                        }
                    ],
                }
            )
        )

        with pytest.raises(ValueError) as error:
            handler_module.plan_manifests(str(tmp_path), {})

        assert "Deployment/gco-system/worker" in str(error.value)

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
        # Table-driven branches use the three custom-object maps. Require each
        # map name to appear in the dispatch condition so adding a map cannot
        # leave its kinds admitted by planning but unreachable at apply time.
        for map_name in (
            "_GATEWAY_CUSTOM_OBJECTS",
            "_QUEUEING_CUSTOM_OBJECTS",
            "_CERT_MANAGER_CUSTOM_OBJECTS",
        ):
            assert f"kind in {map_name}" in source, (
                f"table-driven custom-object dispatch omitted {map_name}; update the apply loop"
            )
        dispatched.update(handler_module._GATEWAY_CUSTOM_OBJECTS)
        dispatched.update(handler_module._QUEUEING_CUSTOM_OBJECTS)
        dispatched.update(handler_module._CERT_MANAGER_CUSTOM_OBJECTS)

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

    @pytest.mark.parametrize("kind", ["Issuer", "Certificate"])
    def test_cert_manager_resource_requires_current_ready_condition(self, handler_module, kind):
        pending = {"metadata": {"generation": 2}, "status": {"conditions": []}}
        stale = {
            "metadata": {"generation": 2},
            "status": {
                "conditions": [{"type": "Ready", "status": "True", "observedGeneration": 1}]
            },
        }
        ready = {
            "metadata": {"generation": 2},
            "status": {
                "conditions": [{"type": "Ready", "status": "True", "observedGeneration": 2}]
            },
        }

        assert handler_module._resource_readiness_failure(kind, pending) == (
            f"{kind} condition Ready is missing"
        )
        stale_failure = handler_module._resource_readiness_failure(kind, stale)
        assert stale_failure is not None
        assert "is stale" in stale_failure
        assert handler_module._resource_readiness_failure(kind, ready) is None

    def test_certificate_readiness_requires_a_nonempty_generated_secret(self, handler_module):
        dynamic_client = MagicMock()
        secret_api = MagicMock()
        dynamic_client.resources.get.return_value = secret_api
        planned = {"namespace": "gco-system"}
        certificate = {"spec": {"secretName": "api-tls"}}

        secret_api.get.return_value = {"data": {"tls.crt": "", "tls.key": "a2V5"}}
        failure = handler_module._certificate_secret_failure(
            dynamic_client,
            {},
            planned,
            certificate,
        )
        assert failure == "Certificate Secret 'api-tls' has no nonempty tls.crt"

        secret_api.get.return_value = {"data": {"tls.crt": "Y2VydA==", "tls.key": "a2V5"}}
        assert (
            handler_module._certificate_secret_failure(
                dynamic_client,
                {},
                planned,
                certificate,
            )
            is None
        )

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


class TestServiceAccountAutomountFlipDiagnostic:
    """Base-pass diagnostic for clusters created by an older release.

    When the incoming manifest disables automountServiceAccountToken on a
    ServiceAccount whose live object still allows it, the apply names every
    planned workload running as that account and whether each carries the
    compensating projected token — the log line that identifies the exact
    workload the flip is about to strand.
    """

    _SA_DOC = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": "gco-test-sa", "namespace": "gco-system"},
        "automountServiceAccountToken": False,
    }

    @staticmethod
    def _plan_with_worker(project_token: bool) -> dict:
        pod_spec: dict = {
            "serviceAccountName": "gco-test-sa",
            "containers": [{"name": "worker", "image": "python:3.14"}],
        }
        if project_token:
            pod_spec["volumes"] = [
                {
                    "name": "kubernetes-api-token",
                    "projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]},
                }
            ]
            pod_spec["containers"][0]["volumeMounts"] = [
                {
                    "name": "kubernetes-api-token",
                    "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
                }
            ]
        return {
            "phases": {
                "base": [],
                "post-helm": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "namespace": "gco-system",
                        "name": "worker",
                        "sourceFile": "post-helm-worker.yaml",
                        "phase": "post-helm",
                        "document": {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "metadata": {"name": "worker", "namespace": "gco-system"},
                            "spec": {"template": {"spec": pod_spec}},
                        },
                    }
                ],
            }
        }

    def _live_sa(self, automount):
        live = MagicMock()
        live.automount_service_account_token = automount
        return live

    def test_flip_on_live_cluster_names_workloads_missing_the_projection(
        self, handler_module, caplog
    ):
        v1 = MagicMock()
        v1.read_namespaced_service_account.return_value = self._live_sa(None)
        with caplog.at_level(logging.WARNING):
            handler_module._log_service_account_automount_flip(
                v1, self._SA_DOC, "gco-system", "gco-test-sa", self._plan_with_worker(False)
            )
        assert "flipped to false" in caplog.text
        assert "post-helm:post-helm-worker.yaml Deployment/worker" in caplog.text
        assert "projected-token=MISSING" in caplog.text

    def test_flip_reports_present_projections(self, handler_module, caplog):
        v1 = MagicMock()
        v1.read_namespaced_service_account.return_value = self._live_sa(True)
        with caplog.at_level(logging.WARNING):
            handler_module._log_service_account_automount_flip(
                v1, self._SA_DOC, "gco-system", "gco-test-sa", self._plan_with_worker(True)
            )
        assert "projected-token=present" in caplog.text

    def test_flip_with_no_referencing_workloads_logs_none(self, handler_module, caplog):
        v1 = MagicMock()
        v1.read_namespaced_service_account.return_value = self._live_sa(None)
        empty_plan = {"phases": {"base": [], "post-helm": []}}
        with caplog.at_level(logging.WARNING):
            handler_module._log_service_account_automount_flip(
                v1, self._SA_DOC, "gco-system", "gco-test-sa", empty_plan
            )
        assert "<none>" in caplog.text

    def test_already_hardened_live_sa_is_not_a_flip(self, handler_module, caplog):
        v1 = MagicMock()
        v1.read_namespaced_service_account.return_value = self._live_sa(False)
        with caplog.at_level(logging.WARNING):
            handler_module._log_service_account_automount_flip(
                v1, self._SA_DOC, "gco-system", "gco-test-sa", self._plan_with_worker(True)
            )
        assert caplog.text == ""

    def test_missing_live_sa_is_a_fresh_cluster_not_a_flip(self, handler_module, caplog):
        v1 = MagicMock()
        v1.read_namespaced_service_account.side_effect = handler_module.ApiException(status=404)
        with caplog.at_level(logging.WARNING):
            handler_module._log_service_account_automount_flip(
                v1, self._SA_DOC, "gco-system", "gco-test-sa", self._plan_with_worker(True)
            )
        assert caplog.text == ""

    def test_live_read_failure_never_blocks_the_apply(self, handler_module, caplog):
        v1 = MagicMock()
        v1.read_namespaced_service_account.side_effect = handler_module.ApiException(status=503)
        with caplog.at_level(logging.WARNING):
            handler_module._log_service_account_automount_flip(
                v1, self._SA_DOC, "gco-system", "gco-test-sa", self._plan_with_worker(True)
            )
        assert caplog.text == ""

    def test_document_without_disabled_automount_skips_the_live_read(self, handler_module, caplog):
        v1 = MagicMock()
        unhardened = {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "gco-test-sa", "namespace": "gco-system"},
        }
        with caplog.at_level(logging.WARNING):
            handler_module._log_service_account_automount_flip(
                v1, unhardened, "gco-system", "gco-test-sa", self._plan_with_worker(True)
            )
        v1.read_namespaced_service_account.assert_not_called()
        assert caplog.text == ""


class TestPlannerPodSpecHelpers:
    """Malformed pod specs never crash the cross-phase token-projection guard."""

    _MOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"

    def test_pod_spec_path_stops_at_a_non_mapping_node(self, handler_module):
        """A Deployment whose template is a scalar has no pod spec."""
        document = {"kind": "Deployment", "spec": {"template": "not-a-mapping"}}

        assert handler_module._planned_pod_spec(document) is None

    def test_pod_spec_is_found_for_each_workload_kind(self, handler_module):
        pod_spec = {"containers": [{"name": "c"}]}
        cron_job = {
            "kind": "CronJob",
            "spec": {"jobTemplate": {"spec": {"template": {"spec": pod_spec}}}},
        }
        scaled_job = {
            "kind": "ScaledJob",
            "spec": {"jobTargetRef": {"template": {"spec": pod_spec}}},
        }
        pod = {"kind": "Pod", "spec": pod_spec}

        assert handler_module._planned_pod_spec(cron_job) is pod_spec
        assert handler_module._planned_pod_spec(scaled_job) is pod_spec
        assert handler_module._planned_pod_spec(pod) is pod_spec
        assert handler_module._planned_pod_spec({"kind": "ConfigMap", "data": {}}) is None

    def test_projection_check_skips_malformed_volumes_and_containers(self, handler_module):
        """Garbage entries are skipped; the init container's mount still counts."""
        pod_spec = {
            "volumes": [
                "garbage",
                {"name": "config", "configMap": {"name": "settings"}},
                {"name": "certs", "projected": {"sources": [{"secret": {"name": "tls"}}]}},
                {"projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]}},
                {
                    "name": "kubernetes-api-token",
                    "projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]},
                },
            ],
            "containers": "garbage",
            "initContainers": [
                "garbage",
                {
                    "name": "init",
                    "volumeMounts": [{"name": "kubernetes-api-token", "mountPath": self._MOUNT}],
                },
            ],
        }

        assert handler_module._pod_spec_projects_service_account_token(pod_spec) is True

    def test_projection_check_requires_a_token_volume_to_mount(self, handler_module):
        """A projected volume carrying only a Secret is not a token, even when mounted."""
        pod_spec = {
            "volumes": [{"name": "certs", "projected": {"sources": [{"secret": {"name": "t"}}]}}],
            "containers": [
                {"name": "app", "volumeMounts": [{"name": "certs", "mountPath": self._MOUNT}]}
            ],
        }

        assert handler_module._pod_spec_projects_service_account_token(pod_spec) is False

    def test_rbac_subjects_bind_only_namespaced_service_accounts(self, handler_module):
        """User subjects, garbage entries and namespace-less ClusterRoleBinding
        subjects never make an account API-bound."""
        planned = [
            {
                "kind": "RoleBinding",
                "namespace": "gco-system",
                "document": {
                    "subjects": [
                        {"kind": "User", "name": "alice"},
                        "garbage",
                        {"kind": "ServiceAccount", "name": "gco-worker-sa"},
                    ]
                },
            },
            {
                "kind": "ClusterRoleBinding",
                "namespace": handler_module._CLUSTER_SCOPE,
                "document": {"subjects": [{"kind": "ServiceAccount", "name": "gco-unscoped-sa"}]},
            },
            {"kind": "ConfigMap", "namespace": "gco-system", "document": {}},
        ]

        assert handler_module._rbac_bound_service_accounts(planned) == {
            ("gco-system", "gco-worker-sa")
        }

    def test_deployment_patch_body_without_spec_is_unchanged(self, handler_module):
        """HPA ownership with no spec to trim is a plain deep copy."""
        document = {"metadata": {"annotations": {"gco.aws/hpa-controls-replicas": "true"}}}

        patch_body = handler_module._deployment_patch_body(document)

        assert patch_body == document
        assert patch_body is not document


class TestPlannerInputValidation:
    """plan_manifests rejects malformed replacements and documents with exact messages."""

    @pytest.mark.parametrize(
        ("replacements", "message"),
        [
            (["{{IMAGE}}", "image"], "ImageReplacements must be a string-to-string mapping"),
            ({"": "image"}, "ImageReplacements contains an empty or non-string key"),
            ({7: "image"}, "ImageReplacements contains an empty or non-string key"),
            ({"{{IMAGE}}": 7}, "ImageReplacements value for '{{IMAGE}}' is not a string"),
        ],
        ids=["not-a-mapping", "empty-key", "non-string-key", "non-string-value"],
    )
    def test_rejects_malformed_replacements_before_reading_any_file(
        self, handler_module, tmp_path, replacements, message
    ):
        (tmp_path / "10-cm.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n"
        )

        with (
            patch.object(handler_module.os, "listdir", wraps=os.listdir) as listdir,
            pytest.raises(ValueError, match=re.escape(message)),
        ):
            handler_module.plan_manifests(str(tmp_path), replacements)

        listdir.assert_not_called()

    def test_unreadable_manifest_is_a_planning_error(self, handler_module, tmp_path):
        """A directory carrying the .yaml suffix cannot be read and names itself."""
        (tmp_path / "10-directory.yaml").mkdir()

        with pytest.raises(ValueError, match=r"10-directory\.yaml: unable to read manifest"):
            handler_module.plan_manifests(str(tmp_path), {})

    def test_empty_documents_are_ignored(self, handler_module, tmp_path):
        """Leading and trailing document separators yield no planned resource."""
        (tmp_path / "10-cm.yaml").write_text(
            "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n  namespace: demo\n---\n"
        )

        plan = handler_module.plan_manifests(str(tmp_path), {})

        assert [item["name"] for item in plan["phases"]["base"]] == ["a"]

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            (
                "kind: ConfigMap\nmetadata:\n  name: a\n",
                "10-invalid.yaml document 1: apiVersion must be a nonempty string",
            ),
            (
                "apiVersion: v1\nkind: '  '\nmetadata:\n  name: a\n",
                "10-invalid.yaml document 1: kind must be a nonempty string",
            ),
            (
                "apiVersion: v1\nkind: ConfigMap\nmetadata: a-name\n",
                "10-invalid.yaml document 1: metadata must be a mapping",
            ),
            (
                "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n  namespace: 42\n",
                "10-invalid.yaml document 1: metadata.namespace must be a nonempty string",
            ),
        ],
        ids=["missing-apiVersion", "blank-kind", "scalar-metadata", "numeric-namespace"],
    )
    def test_rejects_documents_with_a_broken_identity(
        self, handler_module, tmp_path, content, message
    ):
        (tmp_path / "10-invalid.yaml").write_text(content)

        with pytest.raises(ValueError, match=re.escape(message)):
            handler_module.plan_manifests(str(tmp_path), {})

    def test_error_report_is_bounded_with_a_hidden_count(self, handler_module, tmp_path):
        """Only the first 20 errors are spelled out; the rest are counted."""
        documents = [
            {"apiVersion": "example.io/v1", "kind": "Mystery", "metadata": {"name": f"m{index}"}}
            for index in range(handler_module._MAX_PLANNING_FAILURES + 3)
        ]
        (tmp_path / "10-mysteries.yaml").write_text(yaml.safe_dump_all(documents))

        with pytest.raises(ValueError) as error:
            handler_module.plan_manifests(str(tmp_path), {})

        message = str(error.value)
        assert message.count("unsupported kind 'Mystery'") == handler_module._MAX_PLANNING_FAILURES
        assert message.endswith("; ... 3 additional error(s)")


class TestExactResourceDeletion:
    """_delete_exact_resources handles cluster-scoped targets and unexpected errors."""

    @staticmethod
    def _dynamic(handler_module, resource):
        dynamic_client = MagicMock()
        dynamic_client.resources.get.return_value = resource
        return (
            patch.object(handler_module.dynamic, "DynamicClient", return_value=dynamic_client),
            patch.object(handler_module.client, "ApiClient", return_value=MagicMock()),
            patch.object(handler_module.client, "V1DeleteOptions", return_value=MagicMock()),
        )

    def test_cluster_scoped_targets_are_deleted_without_a_namespace(self, handler_module):
        resource = MagicMock()
        dynamic_patch, api_patch, options_patch = self._dynamic(handler_module, resource)

        with dynamic_patch, api_patch, options_patch:
            result = handler_module._prune_disabled_feature("{{FSX_FILE_SYSTEM_ID}}", False)

        deletes = {
            entry.kwargs["name"]: sorted(entry.kwargs) for entry in resource.delete.call_args_list
        }
        assert deletes["gco-fsx-pv-default"] == ["body", "name"]
        assert deletes["fsx-sc"] == ["body", "name"]
        assert deletes["gco-fsx-storage"] == ["body", "name", "namespace"]
        assert result["failed"] == []
        assert "v1/PersistentVolume/<cluster>/gco-fsx-pv-default" in result["pruned"]
        assert "storage.k8s.io/v1/StorageClass/<cluster>/fsx-sc" in result["pruned"]

    def test_unexpected_error_types_are_reported_or_treated_as_absent(self, handler_module, caplog):
        """Non-Kubernetes exceptions fail the prune unless they still signal 404."""

        class _GoneUpstream(Exception):
            """Transport-level error that still carries an HTTP 404 status."""

            status = 404

        resource = MagicMock()
        resource.delete.side_effect = [ValueError("connection reset"), _GoneUpstream("gone")]
        dynamic_patch, api_patch, options_patch = self._dynamic(handler_module, resource)

        with dynamic_patch, api_patch, options_patch, caplog.at_level(logging.INFO):
            result = handler_module._delete_exact_resources(
                (
                    ("v1", "ConfigMap", "gco-system", "gco-valkey"),
                    ("v1", "ConfigMap", "gco-jobs", "gco-valkey"),
                ),
                "disabled-feature",
            )

        assert result == {
            "pruned": [],
            "failed": ["v1/ConfigMap/gco-system/gco-valkey:connection reset"],
        }
        assert "already absent: v1/ConfigMap/gco-jobs/gco-valkey" in caplog.text


class TestEksClientPlumbing:
    """Lazy EKS client, STS presigned token, and default Kubernetes configuration."""

    def test_eks_client_is_created_once_and_cached(self, handler_module):
        with patch.object(handler_module.boto3, "client") as boto_client:
            first = handler_module.get_eks_client()
            second = handler_module.get_eks_client()

        boto_client.assert_called_once_with("eks")
        assert first is second is boto_client.return_value

    def test_eks_token_wraps_a_presigned_get_caller_identity_url(self, handler_module):
        """The token is k8s-aws-v1. + unpadded urlsafe base64 of the signed URL,
        which must carry the cluster name in the x-k8s-aws-id header."""
        session = MagicMock()
        session.client.return_value.meta.service_model.service_id = "STS"
        signer = MagicMock()
        presigned = (
            "https://sts.us-west-2.amazonaws.com/?Action=GetCallerIdentity"
            "&Version=2011-06-15&X-Amz-Signature=deadbeef"
        )
        signer.generate_presigned_url.return_value = presigned

        with (
            patch.object(handler_module.boto3, "Session", return_value=session),
            patch("botocore.signers.RequestSigner", return_value=signer) as signer_cls,
        ):
            token = handler_module.get_eks_token("gco-us-west-2", "us-west-2")

        prefix, _, encoded = token.partition(".")
        assert prefix == "k8s-aws-v1"
        assert "=" not in encoded
        padded = encoded + "=" * (-len(encoded) % 4)
        assert base64.urlsafe_b64decode(padded).decode("utf-8") == presigned
        session.client.assert_called_once_with("sts", region_name="us-west-2")
        signer_cls.assert_called_once_with(
            "STS",
            "us-west-2",
            "sts",
            "v4",
            session.get_credentials.return_value,
            session.events,
        )
        params = signer.generate_presigned_url.call_args.args[0]
        assert params["method"] == "GET"
        assert params["url"].startswith(
            "https://sts.us-west-2.amazonaws.com/?Action=GetCallerIdentity"
        )
        assert params["headers"] == {"x-k8s-aws-id": "gco-us-west-2"}
        assert signer.generate_presigned_url.call_args.kwargs == {
            "region_name": "us-west-2",
            "expires_in": 60,
            "operation_name": "",
        }

    @staticmethod
    def _cluster(ca_pem: bytes) -> dict:
        return {
            "cluster": {
                "endpoint": "https://ABCDEF.gr7.us-east-1.eks.amazonaws.com",
                "certificateAuthority": {"data": base64.b64encode(ca_pem).decode("ascii")},
            }
        }

    def test_configure_writes_the_ca_bundle_and_installs_the_default_configuration(
        self, handler_module, tmp_path
    ):
        ca_pem = b"-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
        eks = MagicMock()
        eks.describe_cluster.return_value = self._cluster(ca_pem)
        ca_path = tmp_path / "eks-ca.crt"
        fd = os.open(ca_path, os.O_RDWR | os.O_CREAT, 0o600)

        with (
            patch.object(handler_module, "get_eks_client", return_value=eks),
            patch.object(handler_module, "get_eks_token", return_value="k8s-aws-v1.tok") as token,
            patch.object(handler_module.client.Configuration, "set_default") as set_default,
            patch("tempfile.mkstemp", return_value=(fd, str(ca_path))) as mkstemp,
            patch("socket.setdefaulttimeout") as set_timeout,
        ):
            handler_module.configure_k8s_client("gco-us-east-1", "us-east-1")

        eks.describe_cluster.assert_called_once_with(name="gco-us-east-1")
        mkstemp.assert_called_once_with(suffix=".crt")
        set_timeout.assert_called_once_with(30)
        token.assert_called_once_with("gco-us-east-1", "us-east-1")
        assert ca_path.read_bytes() == ca_pem
        configuration = set_default.call_args.args[0]
        assert isinstance(configuration, handler_module.client.Configuration)
        assert configuration.host == "https://ABCDEF.gr7.us-east-1.eks.amazonaws.com"
        assert configuration.verify_ssl is True
        assert configuration.connection_pool_maxsize == 1
        assert configuration.retries == 3
        assert configuration.ssl_ca_cert == str(ca_path)
        assert configuration.api_key == {"authorization": "Bearer k8s-aws-v1.tok"}

    def test_configure_closes_the_descriptor_when_the_ca_write_fails(
        self, handler_module, tmp_path
    ):
        eks = MagicMock()
        eks.describe_cluster.return_value = self._cluster(b"ca")
        ca_path = tmp_path / "eks-ca.crt"
        fd = os.open(ca_path, os.O_RDWR | os.O_CREAT, 0o600)

        with (
            patch.object(handler_module, "get_eks_client", return_value=eks),
            patch.object(handler_module, "get_eks_token") as token,
            patch.object(handler_module.client.Configuration, "set_default") as set_default,
            patch("tempfile.mkstemp", return_value=(fd, str(ca_path))),
            patch("socket.setdefaulttimeout"),
            patch.object(handler_module.os, "fdopen", side_effect=OSError("disk full")),
            patch.object(handler_module.os, "close", wraps=os.close) as close,
            pytest.raises(OSError, match="disk full"),
        ):
            handler_module.configure_k8s_client("gco-us-east-1", "us-east-1")

        close.assert_called_once_with(fd)
        token.assert_not_called()
        set_default.assert_not_called()


class TestSendResponse:
    """The CloudFormation callback PUTs the exact response body and never raises."""

    _EVENT = {
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/gco/abc",
        "RequestId": "req-1",
        "LogicalResourceId": "KubectlApply",
        "ResponseURL": "https://cloudformation-custom-resource-response.s3.amazonaws.com/x",
    }

    def test_puts_the_cloudformation_response_body(self, handler_module):
        http = MagicMock()
        context = MagicMock()
        context.log_stream_name = "2026/09/05/[$LATEST]abc"

        with patch.object(handler_module.urllib3, "PoolManager", return_value=http):
            handler_module.send_response(
                self._EVENT, context, "SUCCESS", {"AppliedCount": 3}, "kubectl-KubectlApply"
            )

        http.request.assert_called_once()
        method, url = http.request.call_args.args
        kwargs = http.request.call_args.kwargs
        assert (method, url) == ("PUT", self._EVENT["ResponseURL"])
        assert kwargs["headers"] == {"Content-Type": "application/json"}
        assert kwargs["timeout"] == 10.0
        assert json.loads(kwargs["body"]) == {
            "Status": "SUCCESS",
            "Reason": "See CloudWatch Log Stream: 2026/09/05/[$LATEST]abc",
            "PhysicalResourceId": "kubectl-KubectlApply",
            "StackId": self._EVENT["StackId"],
            "RequestId": "req-1",
            "LogicalResourceId": "KubectlApply",
            "Data": {"AppliedCount": 3},
        }

    def test_explicit_reason_overrides_the_log_stream_pointer(self, handler_module):
        http = MagicMock()

        with patch.object(handler_module.urllib3, "PoolManager", return_value=http):
            handler_module.send_response(
                self._EVENT, MagicMock(), "FAILED", {}, "phys", reason="Deployment/api failed"
            )

        assert json.loads(http.request.call_args.kwargs["body"])["Reason"] == (
            "Deployment/api failed"
        )

    def test_callback_transport_failure_is_logged_not_raised(self, handler_module, caplog):
        http = MagicMock()
        http.request.side_effect = handler_module.urllib3.exceptions.MaxRetryError(
            MagicMock(), self._EVENT["ResponseURL"], reason="timed out"
        )

        with (
            patch.object(handler_module.urllib3, "PoolManager", return_value=http),
            caplog.at_level(logging.ERROR),
        ):
            handler_module.send_response(self._EVENT, MagicMock(), "SUCCESS", {}, "phys")

        assert "Failed to send response" in caplog.text


class TestRestartDaemonSetFailures:
    """Non-404 DaemonSet restart errors are recorded as failures."""

    def test_restart_daemonsets_records_non_404_errors(self, handler_module):
        from kubernetes.client.rest import ApiException

        mock_apps_v1 = MagicMock()
        mock_apps_v1.patch_namespaced_daemon_set.side_effect = [
            ApiException(status=500, reason="Internal Server Error"),
            MagicMock(),
        ]

        with patch.object(handler_module.client, "AppsV1Api", return_value=mock_apps_v1):
            result = handler_module.restart_daemonsets(
                "kube-system", ["efs-csi-node", "fsx-csi-node"]
            )

        assert result == {"restarted": ["fsx-csi-node"], "failed": ["efs-csi-node"]}


class TestWorkloadCredentialVerification:
    """_verify_workload_credentials inspects live pod specs and ServiceAccounts."""

    _DEPLOYMENTS = (
        ("gco-system", "health-monitor", "gco-health-monitor-sa"),
        ("gco-system", "manifest-processor", "gco-manifest-processor-sa"),
        ("gco-system", "inference-proxy", "gco-inference-proxy-sa"),
        ("gco-system", "inference-monitor", "gco-inference-monitor-sa"),
    )

    @staticmethod
    def _env(*names):
        variables = []
        for name in names:
            variable = MagicMock()
            variable.name = name
            variables.append(variable)
        return variables

    @staticmethod
    def _volume(*, projected: bool, audience: str | None = "sts.amazonaws.com"):
        volume = MagicMock()
        if not projected:
            volume.projected = None
            return volume
        source = MagicMock()
        if audience is None:
            source.service_account_token = None
        else:
            source.service_account_token.audience = audience
        volume.projected.sources = [source]
        return volume

    @classmethod
    def _deployment(cls, service_account, *, volumes, env):
        deployment = MagicMock()
        spec = deployment.spec.template.spec
        spec.service_account_name = service_account
        spec.volumes = volumes
        if env is None:
            spec.containers = []
        else:
            container = MagicMock()
            container.env = env
            spec.containers = [container]
        return deployment

    @staticmethod
    def _service_account(annotations):
        service_account = MagicMock()
        service_account.metadata.annotations = annotations
        return service_account

    def _run(self, handler_module, apps_v1, v1):
        with patch.object(handler_module.client, "CoreV1Api", return_value=v1):
            return handler_module._verify_workload_credentials(apps_v1)

    def test_fully_configured_workloads_produce_no_warnings(self, handler_module, caplog):
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment.side_effect = lambda name, namespace: self._deployment(
            next(sa for ns, dep, sa in self._DEPLOYMENTS if (ns, dep) == (namespace, name)),
            volumes=[
                self._volume(projected=False),
                self._volume(projected=True, audience=None),
                self._volume(projected=True, audience="vault"),
                self._volume(projected=True),
            ],
            env=self._env("AWS_REGION", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE"),
        )
        v1 = MagicMock()
        v1.read_namespaced_service_account.return_value = self._service_account(
            {"eks.amazonaws.com/role-arn": "arn:aws:iam::123456789012:role/gco"}
        )

        with caplog.at_level(logging.INFO):
            warnings = self._run(handler_module, apps_v1, v1)

        assert warnings == []
        assert "All workload IAM credential configurations verified" in caplog.text
        read_sas = {entry.args for entry in v1.read_namespaced_service_account.call_args_list}
        assert read_sas == {
            ("gco-health-monitor-sa", "gco-system"),
            ("gco-manifest-processor-sa", "gco-system"),
            ("gco-inference-monitor-sa", "gco-system"),
            ("gco-service-account", "gco-jobs"),
            ("gco-service-account", "gco-inference"),
        }

    def test_each_misconfiguration_is_named(self, handler_module, caplog):
        from kubernetes.client.rest import ApiException

        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment.side_effect = [
            self._deployment(
                "default",
                volumes=None,
                env=self._env("AWS_REGION"),
            ),
            self._deployment(
                "gco-manifest-processor-sa",
                volumes=[self._volume(projected=True, audience="vault")],
                env=None,
            ),
            ApiException(status=404, reason="Not Found"),
            ApiException(status=503, reason="Service Unavailable"),
        ]
        v1 = MagicMock()
        v1.read_namespaced_service_account.side_effect = [
            self._service_account(None),
            self._service_account({"other": "x"}),
            ApiException(status=404, reason="Not Found"),
            ApiException(status=403, reason="Forbidden"),
            self._service_account({"eks.amazonaws.com/role-arn": "arn"}),
        ]

        with caplog.at_level(logging.WARNING):
            warnings = self._run(handler_module, apps_v1, v1)

        assert warnings == [
            "gco-system/health-monitor: uses SA 'default' instead of gco-health-monitor-sa",
            "gco-system/health-monitor: missing projected service-account token volume for IRSA",
            "gco-system/health-monitor: missing AWS_ROLE_ARN env var",
            "gco-system/health-monitor: missing AWS_WEB_IDENTITY_TOKEN_FILE env var",
            "gco-system/manifest-processor: missing projected service-account token volume "
            "for IRSA",
            "gco-system/inference-proxy: deployment not found",
            "gco-system/inference-monitor: failed to read (503)",
            "gco-system/gco-health-monitor-sa: missing eks.amazonaws.com/role-arn annotation",
            "gco-system/gco-manifest-processor-sa: missing eks.amazonaws.com/role-arn annotation",
            "gco-system/gco-inference-monitor-sa: ServiceAccount not found",
            "gco-jobs/gco-service-account: failed to read (403)",
        ]
        assert caplog.text.count("Credential check:") == len(warnings)

    def test_unexpected_read_error_becomes_a_verification_warning(self, handler_module):
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment.side_effect = ValueError("malformed response")
        v1 = MagicMock()
        v1.read_namespaced_service_account.return_value = self._service_account(
            {"eks.amazonaws.com/role-arn": "arn"}
        )

        warnings = self._run(handler_module, apps_v1, v1)

        assert warnings == [
            f"{namespace}/{name}: verification error (malformed response)"
            for namespace, name, _sa in self._DEPLOYMENTS
        ]


# ── Apply dispatch table ─────────────────────────────────────────────────────
#
# One row per kind the apply loop dispatches through a plain create/patch pair:
# (kind, apiVersion, kubernetes.client factory, create method, patch method,
# expected create call, expected patch call). PersistentVolume,
# PersistentVolumeClaim, PriorityClass and StorageClass have bespoke 409
# handling and dedicated test classes; a lockstep test below proves the table
# plus those four equal _SUPPORTED_MANIFEST_KINDS exactly.

_APPLY_NAMESPACE = "demo"
_APPLY_NAME = "obj"


def _cluster_typed_calls():
    return (lambda doc: call(body=doc), lambda doc: call(_APPLY_NAME, body=doc))


def _namespaced_typed_calls():
    return (
        lambda doc: call(_APPLY_NAMESPACE, body=doc),
        lambda doc: call(_APPLY_NAME, _APPLY_NAMESPACE, body=doc),
    )


def _cluster_custom_calls(group, version, plural):
    return (
        lambda doc: call(group, version, plural, body=doc),
        lambda doc: call(group, version, plural, _APPLY_NAME, body=doc),
    )


def _namespaced_custom_calls(group, version, plural):
    return (
        lambda doc: call(group, version, _APPLY_NAMESPACE, plural, body=doc),
        lambda doc: call(group, version, _APPLY_NAMESPACE, plural, _APPLY_NAME, body=doc),
    )


_RBAC = "rbac.authorization.k8s.io/v1"
_RBAC_API = "RbacAuthorizationV1Api"
_CUSTOM_API = "CustomObjectsApi"
_CREATE_CLUSTER_CUSTOM = "create_cluster_custom_object"
_PATCH_CLUSTER_CUSTOM = "patch_cluster_custom_object"
_CREATE_NAMESPACED_CUSTOM = "create_namespaced_custom_object"
_PATCH_NAMESPACED_CUSTOM = "patch_namespaced_custom_object"


def _typed(kind, api_version, factory, create, patch_method, *, cluster_scoped=False):
    calls = _cluster_typed_calls() if cluster_scoped else _namespaced_typed_calls()
    return pytest.param(kind, api_version, factory, create, patch_method, *calls, id=kind)


def _cluster_custom(kind, api_version, plural):
    group, version = api_version.split("/")
    return pytest.param(
        kind,
        api_version,
        _CUSTOM_API,
        _CREATE_CLUSTER_CUSTOM,
        _PATCH_CLUSTER_CUSTOM,
        *_cluster_custom_calls(group, version, plural),
        id=kind,
    )


def _namespaced_custom(kind, api_version, plural):
    group, version = api_version.split("/")
    return pytest.param(
        kind,
        api_version,
        _CUSTOM_API,
        _CREATE_NAMESPACED_CUSTOM,
        _PATCH_NAMESPACED_CUSTOM,
        *_namespaced_custom_calls(group, version, plural),
        id=kind,
    )


_APPLY_DISPATCH = [
    _typed(
        "Namespace", "v1", "CoreV1Api", "create_namespace", "patch_namespace", cluster_scoped=True
    ),
    _typed(
        "ServiceAccount",
        "v1",
        "CoreV1Api",
        "create_namespaced_service_account",
        "patch_namespaced_service_account",
    ),
    _typed(
        "ClusterRole",
        _RBAC,
        _RBAC_API,
        "create_cluster_role",
        "patch_cluster_role",
        cluster_scoped=True,
    ),
    _typed(
        "ClusterRoleBinding",
        _RBAC,
        _RBAC_API,
        "create_cluster_role_binding",
        "patch_cluster_role_binding",
        cluster_scoped=True,
    ),
    _typed("Role", _RBAC, _RBAC_API, "create_namespaced_role", "patch_namespaced_role"),
    _typed(
        "RoleBinding",
        _RBAC,
        _RBAC_API,
        "create_namespaced_role_binding",
        "patch_namespaced_role_binding",
    ),
    _typed(
        "Lease",
        "coordination.k8s.io/v1",
        "CoordinationV1Api",
        "create_namespaced_lease",
        "patch_namespaced_lease",
    ),
    _typed(
        "Deployment",
        "apps/v1",
        "AppsV1Api",
        "create_namespaced_deployment",
        "patch_namespaced_deployment",
    ),
    _typed(
        "StatefulSet",
        "apps/v1",
        "AppsV1Api",
        "create_namespaced_stateful_set",
        "patch_namespaced_stateful_set",
    ),
    _typed(
        "DaemonSet",
        "apps/v1",
        "AppsV1Api",
        "create_namespaced_daemon_set",
        "patch_namespaced_daemon_set",
    ),
    _typed("Job", "batch/v1", "BatchV1Api", "create_namespaced_job", "patch_namespaced_job"),
    _typed(
        "CronJob",
        "batch/v1",
        "BatchV1Api",
        "create_namespaced_cron_job",
        "patch_namespaced_cron_job",
    ),
    _typed(
        "HorizontalPodAutoscaler",
        "autoscaling/v2",
        "AutoscalingV2Api",
        "create_namespaced_horizontal_pod_autoscaler",
        "patch_namespaced_horizontal_pod_autoscaler",
    ),
    _typed(
        "PodDisruptionBudget",
        "policy/v1",
        "PolicyV1Api",
        "create_namespaced_pod_disruption_budget",
        "patch_namespaced_pod_disruption_budget",
    ),
    _typed("Service", "v1", "CoreV1Api", "create_namespaced_service", "patch_namespaced_service"),
    _typed("Pod", "v1", "CoreV1Api", "create_namespaced_pod", "patch_namespaced_pod"),
    _typed(
        "ConfigMap",
        "v1",
        "CoreV1Api",
        "create_namespaced_config_map",
        "patch_namespaced_config_map",
    ),
    _typed("Secret", "v1", "CoreV1Api", "create_namespaced_secret", "patch_namespaced_secret"),
    _cluster_custom("GatewayClass", "gateway.networking.k8s.io/v1", "gatewayclasses"),
    _namespaced_custom("Gateway", "gateway.networking.k8s.io/v1", "gateways"),
    _namespaced_custom("HTTPRoute", "gateway.networking.k8s.io/v1", "httproutes"),
    _namespaced_custom(
        "LoadBalancerConfiguration", "gateway.k8s.aws/v1", "loadbalancerconfigurations"
    ),
    _namespaced_custom(
        "TargetGroupConfiguration", "gateway.k8s.aws/v1", "targetgroupconfigurations"
    ),
    _cluster_custom("ResourceFlavor", "kueue.x-k8s.io/v1beta1", "resourceflavors"),
    _cluster_custom("ClusterQueue", "kueue.x-k8s.io/v1beta1", "clusterqueues"),
    _namespaced_custom("LocalQueue", "kueue.x-k8s.io/v1beta1", "localqueues"),
    _namespaced_custom("Issuer", "cert-manager.io/v1", "issuers"),
    _namespaced_custom("Certificate", "cert-manager.io/v1", "certificates"),
    _cluster_custom("NodePool", "karpenter.sh/v1", "nodepools"),
    _cluster_custom("EC2NodeClass", "karpenter.k8s.aws/v1", "ec2nodeclasses"),
    _typed(
        "APIService",
        "apiregistration.k8s.io/v1",
        "ApiregistrationV1Api",
        "create_api_service",
        "patch_api_service",
        cluster_scoped=True,
    ),
    _typed(
        "CustomResourceDefinition",
        "apiextensions.k8s.io/v1",
        "ApiextensionsV1Api",
        "create_custom_resource_definition",
        "patch_custom_resource_definition",
        cluster_scoped=True,
    ),
    _cluster_custom("DeviceClass", "resource.k8s.io/v1", "deviceclasses"),
    _cluster_custom(
        "ClusterTrainingRuntime", "trainer.kubeflow.org/v1alpha1", "clustertrainingruntimes"
    ),
    _typed(
        "NetworkPolicy",
        "networking.k8s.io/v1",
        "NetworkingV1Api",
        "create_namespaced_network_policy",
        "patch_namespaced_network_policy",
    ),
    _typed(
        "ResourceQuota",
        "v1",
        "CoreV1Api",
        "create_namespaced_resource_quota",
        "patch_namespaced_resource_quota",
    ),
    _typed(
        "LimitRange",
        "v1",
        "CoreV1Api",
        "create_namespaced_limit_range",
        "patch_namespaced_limit_range",
    ),
    _namespaced_custom("ScaledJob", "keda.sh/v1alpha1", "scaledjobs"),
    _namespaced_custom("ScaledObject", "keda.sh/v1alpha1", "scaledobjects"),
    _namespaced_custom("ServiceMonitor", "monitoring.coreos.com/v1", "servicemonitors"),
    _namespaced_custom("PodMonitor", "monitoring.coreos.com/v1", "podmonitors"),
]

_APPLY_DISPATCH_PARAMS = (
    "kind",
    "api_version",
    "factory",
    "create",
    "patch_method",
    "create_call",
    "patch_call",
)

# Kinds whose 409 handling is bespoke and pinned by their own test classes.
_BESPOKE_CONFLICT_KINDS = frozenset(
    {"PersistentVolume", "PersistentVolumeClaim", "PriorityClass", "StorageClass"}
)


def _write_apply_manifest(tmp_path, kind, api_version, handler_module):
    metadata = {"name": _APPLY_NAME}
    if kind not in handler_module._CLUSTER_SCOPED_KINDS:
        metadata["namespace"] = _APPLY_NAMESPACE
    document = {"apiVersion": api_version, "kind": kind, "metadata": metadata}
    (tmp_path / "10-obj.yaml").write_text(yaml.safe_dump(document))
    return document


def _apply_base_pass(handler_module, tmp_path):
    """Run a base apply with the post-apply restarts and credential check stubbed."""
    restart = {"restarted": [], "failed": []}
    with (
        patch.object(handler_module, "configure_k8s_client"),
        patch.object(handler_module, "restart_deployments", return_value=restart),
        patch.object(handler_module, "restart_daemonsets", return_value=restart),
        patch.object(handler_module, "_verify_workload_credentials", return_value=[]),
    ):
        return handler_module.apply_manifests("cluster", "us-east-1", str(tmp_path), {})


class TestApplyDispatchTable:
    """Every dispatched kind creates, patches on 409, and fails loudly otherwise."""

    def test_table_and_bespoke_classes_cover_every_supported_kind(self, handler_module):
        tabled = {param.values[0] for param in _APPLY_DISPATCH}

        assert tabled & _BESPOKE_CONFLICT_KINDS == set()
        assert tabled | _BESPOKE_CONFLICT_KINDS == set(handler_module._SUPPORTED_MANIFEST_KINDS)

    @pytest.mark.parametrize(_APPLY_DISPATCH_PARAMS, _APPLY_DISPATCH)
    def test_fresh_object_is_created_once(
        self,
        handler_module,
        tmp_path,
        kind,
        api_version,
        factory,
        create,
        patch_method,
        create_call,
        patch_call,
    ):
        document = _write_apply_manifest(tmp_path, kind, api_version, handler_module)

        with patch("handler.client") as mock_client:
            result = _apply_base_pass(handler_module, tmp_path)
            api = getattr(mock_client, factory).return_value

        assert (result["AppliedCount"], result["FailedCount"]) == (1, 0)
        assert getattr(api, create).call_args_list == [create_call(document)]
        getattr(api, patch_method).assert_not_called()

    @pytest.mark.parametrize(_APPLY_DISPATCH_PARAMS, _APPLY_DISPATCH)
    def test_existing_object_is_patched_on_conflict(
        self,
        handler_module,
        tmp_path,
        kind,
        api_version,
        factory,
        create,
        patch_method,
        create_call,
        patch_call,
    ):
        document = _write_apply_manifest(tmp_path, kind, api_version, handler_module)

        with patch("handler.client") as mock_client:
            api = getattr(mock_client, factory).return_value
            getattr(api, create).side_effect = handler_module.ApiException(
                status=409, reason="Conflict"
            )
            result = _apply_base_pass(handler_module, tmp_path)

        assert (result["AppliedCount"], result["FailedCount"]) == (1, 0)
        assert getattr(api, patch_method).call_args_list == [patch_call(document)]

    @pytest.mark.parametrize(_APPLY_DISPATCH_PARAMS, _APPLY_DISPATCH)
    def test_non_conflict_create_error_fails_only_that_resource(
        self,
        handler_module,
        tmp_path,
        kind,
        api_version,
        factory,
        create,
        patch_method,
        create_call,
        patch_call,
        caplog,
    ):
        _write_apply_manifest(tmp_path, kind, api_version, handler_module)

        with patch("handler.client") as mock_client, caplog.at_level(logging.ERROR):
            api = getattr(mock_client, factory).return_value
            getattr(api, create).side_effect = handler_module.ApiException(
                status=403, reason="Forbidden"
            )
            result = _apply_base_pass(handler_module, tmp_path)

        assert (result["AppliedCount"], result["FailedCount"]) == (0, 1)
        assert result["Failed"] == f"10-obj.yaml:{kind}/{_APPLY_NAME}"
        getattr(api, patch_method).assert_not_called()
        assert f"API error applying {kind}/{_APPLY_NAME}: 403 - Forbidden" in caplog.text


class TestStorageClassApply:
    """StorageClass fields are immutable, so an existing class is left untouched."""

    @staticmethod
    def _write(tmp_path):
        (tmp_path / "20-storage-class.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "storage.k8s.io/v1",
                    "kind": "StorageClass",
                    "metadata": {"name": "fsx-sc"},
                    "provisioner": "fsx.csi.aws.com",
                }
            )
        )

    def test_created_cluster_scoped(self, handler_module, tmp_path):
        self._write(tmp_path)

        with patch("handler.client") as mock_client:
            result = _apply_base_pass(handler_module, tmp_path)
            storage = mock_client.StorageV1Api.return_value

        assert (result["AppliedCount"], result["FailedCount"]) == (1, 0)
        storage.create_storage_class.assert_called_once()
        assert storage.create_storage_class.call_args.kwargs["body"]["metadata"] == {
            "name": "fsx-sc"
        }

    def test_existing_class_is_skipped_not_patched(self, handler_module, tmp_path, caplog):
        self._write(tmp_path)

        with patch("handler.client") as mock_client, caplog.at_level(logging.INFO):
            storage = mock_client.StorageV1Api.return_value
            storage.create_storage_class.side_effect = handler_module.ApiException(status=409)
            result = _apply_base_pass(handler_module, tmp_path)

        assert (result["AppliedCount"], result["FailedCount"]) == (1, 0)
        assert "StorageClass fsx-sc already exists, skipping update" in caplog.text
        assert [name for name, _a, _k in storage.mock_calls] == ["create_storage_class"]

    def test_non_conflict_error_fails_the_class(self, handler_module, tmp_path):
        self._write(tmp_path)

        with patch("handler.client") as mock_client:
            storage = mock_client.StorageV1Api.return_value
            storage.create_storage_class.side_effect = handler_module.ApiException(status=403)
            result = _apply_base_pass(handler_module, tmp_path)

        assert (result["AppliedCount"], result["FailedCount"]) == (0, 1)
        assert result["Failed"] == "20-storage-class.yaml:StorageClass/fsx-sc"


class TestApplyLoopGuards:
    """Non-API failures are isolated per file; unknown gates never prune."""

    def test_kind_admitted_by_planning_without_a_dispatch_branch_fails_its_file(
        self, handler_module, tmp_path, caplog
    ):
        """Planner/dispatch drift is a recorded per-file failure, not a crash.

        Reproduces the 2026-08-14 shape (a kind added to
        _SUPPORTED_MANIFEST_KINDS with no apply branch) by widening the
        planner's allowlist; the defensive ValueError must be caught by the
        per-file isolation so the remaining manifests still apply.
        """
        (tmp_path / "10-mystery.yaml").write_text(
            "apiVersion: example.io/v1\nkind: Mystery\nmetadata:\n  name: m\n  namespace: demo\n"
        )
        (tmp_path / "20-cm.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: keep\n  namespace: demo\n"
        )
        widened = frozenset(handler_module._SUPPORTED_MANIFEST_KINDS | {"Mystery"})

        with (
            patch.object(handler_module, "_SUPPORTED_MANIFEST_KINDS", widened),
            patch("handler.client") as mock_client,
            caplog.at_level(logging.ERROR),
        ):
            result = _apply_base_pass(handler_module, tmp_path)

        assert (result["AppliedCount"], result["ExpectedCount"], result["FailedCount"]) == (1, 2, 1)
        assert result["Failed"] == "10-mystery.yaml"
        assert "Planner admitted unsupported kind: Mystery" in caplog.text
        mock_client.CoreV1Api.return_value.create_namespaced_config_map.assert_called_once()

    def test_gate_without_a_prune_inventory_is_skipped_without_pruning(
        self, handler_module, tmp_path
    ):
        (tmp_path / "40-optional.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: optional\n"
            "data:\n  enabled: '{{OPTIONAL_FEATURE_ENABLED}}'\n"
        )

        with (
            patch.object(handler_module, "_prune_disabled_feature") as prune,
            patch("handler.client"),
        ):
            result = _apply_base_pass(handler_module, tmp_path)

        prune.assert_not_called()
        assert result["AppliedCount"] == 0
        assert result["Skipped"] == "40-optional.yaml:unreplaced-placeholders"
        assert result["PrunedCount"] == 0


class TestPersistentVolumeDeleteWait:
    """Recreating a PV polls for its disappearance within a bounded budget."""

    _DOC = {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": "gco-fsx-pv-jobs"},
        "spec": {"csi": {"driver": "fsx.csi.aws.com", "volumeHandle": "fs-new"}},
    }

    @staticmethod
    def _existing(handle):
        existing = MagicMock()
        existing.spec.csi.volume_handle = handle
        return existing

    def _run(self, handler_module, tmp_path, *, reads, creates):
        (tmp_path / "20-pv.yaml").write_text(yaml.safe_dump(self._DOC))
        with (
            patch("handler.client") as mock_client,
            patch.object(handler_module.time, "sleep") as sleep,
        ):
            v1 = mock_client.CoreV1Api.return_value
            v1.create_persistent_volume.side_effect = creates
            v1.read_persistent_volume.side_effect = reads
            result = _apply_base_pass(handler_module, tmp_path)
        return result, v1, sleep

    def test_polls_and_sleeps_until_the_old_volume_is_gone(self, handler_module, tmp_path):
        result, v1, sleep = self._run(
            handler_module,
            tmp_path,
            reads=[
                self._existing("fs-old"),
                self._existing("fs-old"),
                handler_module.ApiException(status=404),
            ],
            creates=[handler_module.ApiException(status=409), None],
        )

        assert (result["AppliedCount"], result["FailedCount"]) == (1, 0)
        sleep.assert_called_once_with(handler_module.PV_PVC_DELETE_POLL_INTERVAL_SECONDS)
        assert v1.create_persistent_volume.call_count == 2

    def test_exhausted_wait_budget_still_attempts_the_recreate(self, handler_module, tmp_path):
        with patch.object(handler_module, "PV_PVC_DELETE_WAIT_SECONDS", 2):
            result, v1, sleep = self._run(
                handler_module,
                tmp_path,
                reads=[self._existing("fs-old")] * 3,
                creates=[handler_module.ApiException(status=409), None],
            )

        assert (result["AppliedCount"], result["FailedCount"]) == (1, 0)
        assert sleep.call_count == 2
        assert v1.read_persistent_volume.call_count == 3
        assert v1.create_persistent_volume.call_count == 2

    def test_poll_error_other_than_404_fails_the_volume(self, handler_module, tmp_path):
        result, v1, sleep = self._run(
            handler_module,
            tmp_path,
            reads=[self._existing("fs-old"), handler_module.ApiException(status=500)],
            creates=[handler_module.ApiException(status=409), None],
        )

        assert (result["AppliedCount"], result["FailedCount"]) == (0, 1)
        assert result["Failed"] == "20-pv.yaml:PersistentVolume/gco-fsx-pv-jobs"
        sleep.assert_not_called()
        assert v1.create_persistent_volume.call_count == 1

    def test_non_conflict_create_error_fails_the_volume(self, handler_module, tmp_path):
        result, v1, _sleep = self._run(
            handler_module,
            tmp_path,
            reads=[],
            creates=handler_module.ApiException(status=403),
        )

        assert result["Failed"] == "20-pv.yaml:PersistentVolume/gco-fsx-pv-jobs"
        v1.read_persistent_volume.assert_not_called()


class TestPersistentVolumeClaimDeleteWait:
    """Recreating a Lost PVC polls for its disappearance within a bounded budget."""

    _DOC = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "gco-fsx-storage", "namespace": "gco-jobs"},
        "spec": {"storageClassName": "fsx-sc"},
    }

    @staticmethod
    def _existing(phase):
        existing = MagicMock()
        existing.status.phase = phase
        return existing

    def _run(self, handler_module, tmp_path, *, reads, creates):
        (tmp_path / "21-pvc.yaml").write_text(yaml.safe_dump(self._DOC))
        with (
            patch("handler.client") as mock_client,
            patch.object(handler_module.time, "sleep") as sleep,
        ):
            v1 = mock_client.CoreV1Api.return_value
            v1.create_namespaced_persistent_volume_claim.side_effect = creates
            v1.read_namespaced_persistent_volume_claim.side_effect = reads
            result = _apply_base_pass(handler_module, tmp_path)
        return result, v1, sleep

    def test_polls_and_sleeps_until_the_lost_claim_is_gone(self, handler_module, tmp_path):
        result, v1, sleep = self._run(
            handler_module,
            tmp_path,
            reads=[
                self._existing("Lost"),
                self._existing("Lost"),
                handler_module.ApiException(status=404),
            ],
            creates=[handler_module.ApiException(status=409), None],
        )

        assert (result["AppliedCount"], result["FailedCount"]) == (1, 0)
        sleep.assert_called_once_with(handler_module.PV_PVC_DELETE_POLL_INTERVAL_SECONDS)
        assert v1.create_namespaced_persistent_volume_claim.call_count == 2

    def test_exhausted_wait_budget_still_attempts_the_recreate(self, handler_module, tmp_path):
        with patch.object(handler_module, "PV_PVC_DELETE_WAIT_SECONDS", 2):
            result, v1, sleep = self._run(
                handler_module,
                tmp_path,
                reads=[self._existing("Lost")] * 3,
                creates=[handler_module.ApiException(status=409), None],
            )

        assert (result["AppliedCount"], result["FailedCount"]) == (1, 0)
        assert sleep.call_count == 2
        assert v1.create_namespaced_persistent_volume_claim.call_count == 2

    def test_poll_error_other_than_404_fails_the_claim(self, handler_module, tmp_path):
        result, v1, sleep = self._run(
            handler_module,
            tmp_path,
            reads=[self._existing("Lost"), handler_module.ApiException(status=500)],
            creates=[handler_module.ApiException(status=409), None],
        )

        assert result["Failed"] == "21-pvc.yaml:PersistentVolumeClaim/gco-fsx-storage"
        sleep.assert_not_called()
        assert v1.create_namespaced_persistent_volume_claim.call_count == 1

    def test_non_conflict_create_error_fails_the_claim(self, handler_module, tmp_path):
        result, v1, _sleep = self._run(
            handler_module,
            tmp_path,
            reads=[],
            creates=handler_module.ApiException(status=403),
        )

        assert result["Failed"] == "21-pvc.yaml:PersistentVolumeClaim/gco-fsx-storage"
        v1.read_namespaced_persistent_volume_claim.assert_not_called()


class TestAutomountFlipDiagnosticSkipsUnrelatedPlanItems:
    """Only workloads in the same namespace running as the account are named."""

    def test_flip_ignores_other_namespaces_accounts_and_non_workloads(self, handler_module, caplog):
        def planned(kind, namespace, document):
            return {
                "apiVersion": "apps/v1",
                "kind": kind,
                "namespace": namespace,
                "name": "x",
                "sourceFile": "post-helm-x.yaml",
                "phase": "post-helm",
                "document": document,
            }

        worker = {"spec": {"template": {"spec": {"serviceAccountName": "gco-test-sa"}}}}
        other_account = {"spec": {"template": {"spec": {"serviceAccountName": "other-sa"}}}}
        plan = {
            "phases": {
                "base": [planned("ConfigMap", "gco-system", {"kind": "ConfigMap"})],
                "post-helm": [
                    planned("Deployment", "gco-jobs", {"kind": "Deployment", **worker}),
                    planned("Deployment", "gco-system", {"kind": "Deployment", **other_account}),
                ],
            }
        }
        v1 = MagicMock()
        v1.read_namespaced_service_account.return_value.automount_service_account_token = None
        document = {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "gco-test-sa", "namespace": "gco-system"},
            "automountServiceAccountToken": False,
        }

        with caplog.at_level(logging.WARNING):
            handler_module._log_service_account_automount_flip(
                v1, document, "gco-system", "gco-test-sa", plan
            )

        assert "flipped to false" in caplog.text
        assert "running as it: <none>" in caplog.text


class TestGatewayResourceDeletionErrors:
    """Teardown surfaces API errors with the failing object and polls while present."""

    @staticmethod
    def _custom_api(handler_module):
        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = handler_module.ApiException(
            status=404, reason="Not Found"
        )
        custom_api.get_cluster_custom_object.side_effect = handler_module.ApiException(
            status=404, reason="Not Found"
        )
        return custom_api

    def _patches(self, handler_module, custom_api):
        return (
            patch.object(handler_module, "configure_k8s_client"),
            patch.object(handler_module.client, "CustomObjectsApi", return_value=custom_api),
            patch.object(handler_module.client, "V1DeleteOptions"),
        )

    def test_delete_error_other_than_404_names_the_object(self, handler_module):
        custom_api = self._custom_api(handler_module)
        custom_api.delete_namespaced_custom_object.side_effect = handler_module.ApiException(
            status=403, reason="Forbidden"
        )
        configure, api, options = self._patches(handler_module, custom_api)

        with (
            configure,
            api,
            options,
            pytest.raises(
                RuntimeError, match=r"failed to delete HTTPRoute/gco-system/gco-routes"
            ) as e,
        ):
            handler_module._delete_gateway_resources("cluster", "us-east-1")

        assert "Kubernetes API 403 (Forbidden)" in str(e.value)
        assert isinstance(e.value.__cause__, handler_module.ApiException)

    def test_poll_error_other_than_404_names_the_object(self, handler_module):
        custom_api = self._custom_api(handler_module)
        custom_api.get_namespaced_custom_object.side_effect = handler_module.ApiException(
            status=500, reason="Internal Server Error"
        )
        configure, api, options = self._patches(handler_module, custom_api)

        with (
            configure,
            api,
            options,
            pytest.raises(
                RuntimeError, match=r"waiting for HTTPRoute/gco-system/gco-routes deletion"
            ),
        ):
            handler_module._delete_gateway_resources("cluster", "us-east-1")

    def test_still_present_objects_are_polled_after_a_sleep(self, handler_module):
        custom_api = self._custom_api(handler_module)
        custom_api.get_namespaced_custom_object.side_effect = [
            {"metadata": {"deletionTimestamp": "2026-09-05T00:00:00Z"}},
            handler_module.ApiException(status=404, reason="Not Found"),
            *[handler_module.ApiException(status=404, reason="Not Found") for _ in range(6)],
        ]
        configure, api, options = self._patches(handler_module, custom_api)

        with (
            configure,
            api,
            options,
            patch.object(handler_module.time, "monotonic", return_value=1000.0),
            patch.object(handler_module.time, "sleep") as sleep,
        ):
            result = handler_module._delete_gateway_resources("cluster", "us-east-1")

        sleep.assert_called_once_with(handler_module._GATEWAY_DELETE_POLL_SECONDS)
        assert result["DeletedCount"] == 8
        assert result["Deleted"][0] == "HTTPRoute/gco-system/gco-routes"


class TestReadinessHelpers:
    """Condition, generation and replica helpers tolerate real API status shapes."""

    def test_as_plain_dict_accepts_dynamic_resource_instances(self, handler_module):
        class _ResourceInstance:
            def to_dict(self):
                return {"metadata": {"name": "api"}}

        class _WeirdInstance:
            """to_dict() misbehaves, but the object is still a mapping of pairs."""

            def to_dict(self):
                return "not-a-mapping"

            def __iter__(self):
                return iter([("kind", "Service")])

        assert handler_module._as_plain_dict({"a": 1}) == {"a": 1}
        assert handler_module._as_plain_dict(_ResourceInstance()) == {"metadata": {"name": "api"}}
        assert handler_module._as_plain_dict(_WeirdInstance()) == {"kind": "Service"}
        assert handler_module._as_plain_dict([("items", [])]) == {"items": []}

    def test_as_plain_dict_rejects_non_mappings(self, handler_module):
        with pytest.raises(TypeError, match="Kubernetes response is not a mapping: int"):
            handler_module._as_plain_dict(42)

    def test_condition_status_accepts_booleans_and_strings(self, handler_module):
        assert handler_module._condition_matches({"status": True}, "True") is True
        assert handler_module._condition_matches({"status": False}, "False") is True
        assert handler_module._condition_matches({"status": "true"}, "True") is True
        assert handler_module._condition_matches({"status": "Unknown"}, "True") is False

    def test_conditions_ignore_a_non_list_and_non_mapping_entries(self, handler_module):
        assert handler_module._conditions({"conditions": "Ready"}) == []
        assert handler_module._conditions({"conditions": ["x", {"type": "Ready"}]}) == [
            {"type": "Ready"}
        ]

    def test_required_condition_reports_the_last_matching_detail(self, handler_module):
        status = {
            "conditions": [
                {"type": "Ready", "status": "False", "reason": "Pending"},
                {"type": "Ready", "status": "False", "message": "waiting for nodes"},
            ]
        }

        assert handler_module._required_condition_failure(status, "Ready") == (
            "condition Ready is not True (waiting for nodes)"
        )
        assert handler_module._required_condition_failure({}, "Ready") == (
            "condition Ready is missing"
        )

    def test_generation_must_be_observed(self, handler_module):
        stale = {"metadata": {"generation": 4}, "status": {"observedGeneration": 3}}

        assert handler_module._generation_failure(stale) == (
            "generation not observed (generation=4, observedGeneration=3)"
        )
        assert handler_module._generation_failure({"metadata": {"generation": True}}) is not None
        assert (
            handler_module._generation_failure(
                {"metadata": {"generation": 2}, "status": {"observedGeneration": 2}}
            )
            is None
        )

    def test_replica_failure_rejects_unavailable_replicas(self, handler_module):
        status = {
            "replicas": 2,
            "updatedReplicas": 2,
            "readyReplicas": 2,
            "availableReplicas": 2,
            "unavailableReplicas": 1,
        }
        fields = ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")

        assert handler_module._replica_failure(status, 2, fields) == "replicas unavailable (1)"

    def test_current_condition_failure_reports_current_stale_and_missing(self, handler_module):
        conditions = [
            {"type": "Accepted", "status": "False", "observedGeneration": 2, "reason": "Invalid"},
            {"type": "Programmed", "status": "True", "observedGeneration": 1},
        ]

        assert handler_module._current_condition_failure(conditions, "Accepted", 2, "Gateway") == (
            "Gateway condition Accepted is not True (Invalid)"
        )
        assert handler_module._current_condition_failure(
            conditions, "Programmed", 2, "Gateway"
        ) == ("Gateway condition Programmed is stale (generation=2, observedGeneration=1)")
        assert handler_module._current_condition_failure(conditions, "Ready", 2, "Gateway") == (
            "Gateway condition Ready is missing"
        )


def _gateway_condition(condition_type, generation, status="True"):
    return {"type": condition_type, "status": status, "observedGeneration": generation}


class TestGatewayApiReadiness:
    """Gateway API objects are ready only with current, listener- and parent-level evidence."""

    _GENERATION = 3

    @classmethod
    def _gateway(cls, **overrides):
        generation = cls._GENERATION
        resource = {
            "metadata": {
                "name": "gco-gateway",
                "namespace": "gco-system",
                "generation": generation,
            },
            "spec": {"listeners": [{"name": "https", "port": 443}, "garbage", {"port": 80}]},
            "status": {
                "conditions": [
                    _gateway_condition("Accepted", generation),
                    _gateway_condition("Programmed", generation),
                ],
                "addresses": [{"type": "Hostname", "value": "k8s-gco-abc.elb.amazonaws.com"}],
                "listeners": [
                    {
                        "name": "https",
                        "conditions": [
                            _gateway_condition("Accepted", generation),
                            _gateway_condition("ResolvedRefs", generation),
                            _gateway_condition("Programmed", generation),
                        ],
                    },
                    "garbage",
                ],
            },
        }
        for key, value in overrides.items():
            resource["status"][key] = value
        return resource

    @classmethod
    def _http_route(cls, **overrides):
        generation = cls._GENERATION
        parent_ref = {"name": "gco-gateway", "sectionName": "https"}
        resource = {
            "metadata": {"name": "gco-routes", "namespace": "gco-system", "generation": generation},
            "spec": {"parentRefs": [parent_ref, "garbage"]},
            "status": {
                "parents": [
                    {
                        "parentRef": {
                            "group": "gateway.networking.k8s.io",
                            "kind": "Gateway",
                            "namespace": "gco-system",
                            "name": "gco-gateway",
                            "sectionName": "https",
                        },
                        "conditions": [
                            _gateway_condition("Accepted", generation),
                            _gateway_condition("ResolvedRefs", generation),
                        ],
                    },
                    {"controllerName": "gateway.k8s.aws/alb"},
                ]
            },
        }
        for key, value in overrides.items():
            resource["status"][key] = value
        return resource

    def test_readiness_dispatches_gateway_kinds_and_requires_a_generation(self, handler_module):
        failure = handler_module._resource_readiness_failure("Gateway", {"metadata": {}})

        assert failure == "invalid metadata.generation (None)"

    def test_gateway_class_requires_a_current_accepted_condition(self, handler_module):
        accepted = {
            "metadata": {"generation": 2},
            "status": {"conditions": [_gateway_condition("Accepted", 2)]},
        }
        stale = {
            "metadata": {"generation": 2},
            "status": {"conditions": [_gateway_condition("Accepted", 1)]},
        }

        assert handler_module._gateway_api_readiness_failure("GatewayClass", accepted) is None
        assert handler_module._gateway_api_readiness_failure("GatewayClass", stale) == (
            "GatewayClass condition Accepted is stale (generation=2, observedGeneration=1)"
        )

    def test_fully_programmed_gateway_is_ready(self, handler_module):
        assert handler_module._gateway_api_readiness_failure("Gateway", self._gateway()) is None

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            (
                {"conditions": [_gateway_condition("Accepted", 3)]},
                "Gateway condition Programmed is missing",
            ),
            ({"addresses": []}, "Gateway has no address"),
            (
                {"addresses": [{"type": "Hostname", "value": "  "}, "garbage"]},
                "Gateway has no address",
            ),
            ({"listeners": {}}, "Gateway listener status is missing"),
            ({"listeners": [{"name": "http"}]}, "Gateway listener 'https' status is missing"),
            (
                {"listeners": [{"name": "https", "conditions": "Ready"}]},
                "Gateway listener 'https' conditions are missing",
            ),
            (
                {
                    "listeners": [
                        {
                            "name": "https",
                            "conditions": [
                                "garbage",
                                _gateway_condition("Accepted", 3),
                                _gateway_condition("ResolvedRefs", 3, status="False"),
                            ],
                        }
                    ]
                },
                "Gateway listener 'https' condition ResolvedRefs is not True (status is not True)",
            ),
        ],
        ids=[
            "not-programmed",
            "no-address",
            "blank-address",
            "listener-status-not-a-list",
            "intended-listener-missing",
            "listener-conditions-not-a-list",
            "listener-condition-false",
        ],
    )
    def test_gateway_failures_name_the_missing_evidence(self, handler_module, overrides, message):
        resource = self._gateway(**overrides)

        assert handler_module._gateway_api_readiness_failure("Gateway", resource) == message

    def test_http_route_with_an_accepted_intended_parent_is_ready(self, handler_module):
        assert (
            handler_module._gateway_api_readiness_failure("HTTPRoute", self._http_route()) is None
        )

    def test_http_route_parent_ref_defaults_fill_group_kind_and_namespace(self, handler_module):
        """A parentRef omitting group/kind/namespace matches a fully qualified status entry."""
        route = self._http_route()
        route["status"]["parents"][0]["parentRef"] = {"name": "gco-gateway", "sectionName": "https"}

        assert handler_module._gateway_api_readiness_failure("HTTPRoute", route) is None

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"parents": None}, "HTTPRoute parent status is missing"),
            (
                {"parents": []},
                "HTTPRoute intended parent ('gateway.networking.k8s.io', 'Gateway', 'gco-system', "
                "'gco-gateway', 'https') status is missing",
            ),
            (
                {
                    "parents": [
                        {
                            "parentRef": {"name": "gco-gateway", "sectionName": "https"},
                            "conditions": {"type": "Accepted"},
                        }
                    ]
                },
                "HTTPRoute intended parent ('gateway.networking.k8s.io', 'Gateway', 'gco-system', "
                "'gco-gateway', 'https') conditions are missing",
            ),
            (
                {
                    "parents": [
                        {
                            "parentRef": {"name": "gco-gateway", "sectionName": "https"},
                            "conditions": [
                                _gateway_condition("Accepted", 3),
                                {
                                    "type": "ResolvedRefs",
                                    "status": "False",
                                    "observedGeneration": 3,
                                    "message": "backend Service not found",
                                },
                            ],
                        }
                    ]
                },
                "HTTPRoute parent ('gateway.networking.k8s.io', 'Gateway', 'gco-system', "
                "'gco-gateway', 'https') condition ResolvedRefs is not True "
                "(backend Service not found)",
            ),
        ],
        ids=[
            "parents-not-a-list",
            "intended-parent-missing",
            "parent-conditions-not-a-list",
            "parent-condition-false",
        ],
    )
    def test_http_route_failures_name_the_intended_parent(self, handler_module, overrides, message):
        resource = self._http_route(**overrides)

        assert handler_module._gateway_api_readiness_failure("HTTPRoute", resource) == message

    def test_non_gateway_kinds_have_no_gateway_readiness_contract(self, handler_module):
        resource = {"metadata": {"generation": 1}}

        assert handler_module._gateway_api_readiness_failure("ConfigMap", resource) is None


class TestResourceReadinessFailures:
    """Per-kind readiness reasons are actionable and name the offending field."""

    def test_terminating_object_is_never_ready(self, handler_module):
        resource = {"metadata": {"deletionTimestamp": "2026-09-05T10:00:00Z"}}

        assert handler_module._resource_readiness_failure("ConfigMap", resource) == (
            "object is terminating since 2026-09-05T10:00:00Z"
        )

    def test_explicit_false_ready_or_available_condition_fails_any_kind(self, handler_module):
        resource = {
            "status": {
                "conditions": [
                    {
                        "type": "Available",
                        "status": "False",
                        "message": "MinimumReplicasUnavailable",
                    }
                ]
            }
        }

        assert handler_module._resource_readiness_failure("Deployment", resource) == (
            "condition Available is False (MinimumReplicasUnavailable)"
        )

    @pytest.mark.parametrize(
        ("kind", "resource", "message"),
        [
            (
                "Deployment",
                {"metadata": {"generation": 2}, "status": {"observedGeneration": 1}},
                "generation not observed (generation=2, observedGeneration=1)",
            ),
            (
                "StatefulSet",
                {
                    "metadata": {"generation": 1},
                    "spec": {"replicas": "two"},
                    "status": {"observedGeneration": 1},
                },
                "invalid desired replica count (two)",
            ),
            (
                "DaemonSet",
                {"metadata": {"generation": 5}, "status": {"observedGeneration": 4}},
                "generation not observed (generation=5, observedGeneration=4)",
            ),
            (
                "DaemonSet",
                {"metadata": {"generation": 1}, "status": {"observedGeneration": 1}},
                "invalid desiredNumberScheduled (None)",
            ),
            (
                "DaemonSet",
                {
                    "metadata": {"generation": 1},
                    "status": {
                        "observedGeneration": 1,
                        "desiredNumberScheduled": 2,
                        "currentNumberScheduled": 2,
                        "updatedNumberScheduled": 2,
                        "numberReady": 1,
                    },
                },
                "replicas not converged (numberReady=1, desired=2)",
            ),
            (
                "Job",
                {"status": {"conditions": [{"type": "Failed", "status": "True"}]}},
                "condition Failed is True",
            ),
            (
                "Job",
                {"status": {"conditions": [{"type": "Complete", "status": "False"}]}},
                "condition Complete is not True (status is not True)",
            ),
            (
                "PersistentVolumeClaim",
                {"status": {"phase": "Pending"}},
                "PVC phase is 'Pending', expected 'Bound'",
            ),
            (
                "PersistentVolume",
                {"status": {"phase": "Released"}},
                "PV phase is 'Released', expected 'Bound' or 'Available'",
            ),
            (
                "HorizontalPodAutoscaler",
                {"status": {"conditions": [{"type": "AbleToScale", "status": "True"}]}},
                "condition ScalingActive is missing",
            ),
            (
                "PodDisruptionBudget",
                {"metadata": {"generation": 2}, "status": {"observedGeneration": 1}},
                "generation not observed (generation=2, observedGeneration=1)",
            ),
            (
                "PodDisruptionBudget",
                {
                    "metadata": {"generation": 1},
                    "status": {"observedGeneration": 1, "currentHealthy": 2},
                },
                "invalid PDB health (currentHealthy=2, desiredHealthy=None)",
            ),
            (
                "PodDisruptionBudget",
                {
                    "metadata": {"generation": 1},
                    "status": {"observedGeneration": 1, "currentHealthy": 1, "desiredHealthy": 2},
                },
                "PDB health below target (currentHealthy=1, desiredHealthy=2)",
            ),
            ("Certificate", {"metadata": {}, "status": {}}, "invalid metadata.generation (None)"),
        ],
        ids=[
            "deployment-stale-generation",
            "statefulset-invalid-replicas",
            "daemonset-stale-generation",
            "daemonset-missing-desired",
            "daemonset-not-converged",
            "job-failed",
            "job-incomplete",
            "pvc-pending",
            "pv-released",
            "hpa-missing-condition",
            "pdb-stale-generation",
            "pdb-invalid-health",
            "pdb-below-target",
            "certificate-no-generation",
        ],
    )
    def test_each_kind_reports_its_unready_reason(self, handler_module, kind, resource, message):
        assert handler_module._resource_readiness_failure(kind, resource) == message


class TestServiceEndpointEvidence:
    """Selector-backed Services need one ready, nonterminating EndpointSlice endpoint."""

    @staticmethod
    def _planned(spec, annotations=None):
        metadata = {"name": "api", "namespace": "gco-system"}
        if annotations:
            metadata["annotations"] = annotations
        return {
            "namespace": "gco-system",
            "name": "api",
            "document": {"apiVersion": "v1", "kind": "Service", "metadata": metadata, "spec": spec},
        }

    @staticmethod
    def _dynamic(items):
        dynamic_client = MagicMock()
        dynamic_client.resources.get.return_value.get.return_value = {"items": items}
        return dynamic_client

    def test_selectorless_service_has_no_endpoint_contract(self, handler_module):
        dynamic_client = MagicMock()
        planned = self._planned({"type": "ExternalName", "externalName": "example.invalid"})

        assert handler_module._service_endpoint_failure(dynamic_client, {}, planned) is None
        dynamic_client.resources.get.assert_not_called()

    def test_items_must_be_a_list(self, handler_module):
        dynamic_client = self._dynamic(None)
        planned = self._planned({"selector": {"app": "api"}})

        assert handler_module._service_endpoint_failure(dynamic_client, {}, planned) == (
            "EndpointSlice response does not contain an items list"
        )

    def test_terminating_slices_and_malformed_entries_are_skipped(self, handler_module):
        items = [
            {
                "metadata": {"deletionTimestamp": "2026-09-05T10:00:00Z"},
                "endpoints": [{"conditions": {"ready": True}}],
            },
            {"metadata": {}, "endpoints": "garbage"},
            {"metadata": {}, "endpoints": ["garbage", {"conditions": "garbage"}]},
            {"metadata": {}, "endpoints": [{"conditions": {"ready": False}}]},
        ]
        planned = self._planned({"selector": {"app": "api"}})

        failure = handler_module._service_endpoint_failure(self._dynamic(items), {}, planned)
        assert (
            failure == "selector-backed Service has no ready, nonterminating EndpointSlice endpoint"
        )

        items.append({"metadata": {}, "endpoints": [{"conditions": {"ready": True}}]})
        dynamic_client = self._dynamic(items)
        assert handler_module._service_endpoint_failure(dynamic_client, {}, planned) is None
        dynamic_client.resources.get.return_value.get.assert_called_once_with(
            namespace="gco-system", label_selector="kubernetes.io/service-name=api"
        )

    def test_certificate_without_a_secret_name_is_not_ready(self, handler_module):
        dynamic_client = MagicMock()

        failure = handler_module._certificate_secret_failure(
            dynamic_client, {}, {"namespace": "gco-system"}, {"spec": {"issuerRef": {"name": "x"}}}
        )

        assert failure == "Certificate spec.secretName is missing"
        dynamic_client.resources.get.assert_not_called()


class TestManifestValidationEdgeCases:
    """validate_manifests bounds its report and classifies every failure type."""

    def test_certificate_validation_reads_the_issued_secret(self, handler_module, tmp_path):
        (tmp_path / "post-helm-tls.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Certificate",
                    "metadata": {"name": "api-tls", "namespace": "gco-system"},
                    "spec": {"secretName": "api-tls", "issuerRef": {"name": "gco-issuer"}},
                }
            )
        )
        live_objects = {
            ("cert-manager.io/v1", "Certificate", "gco-system", "api-tls"): {
                "metadata": {"generation": 1},
                "spec": {"secretName": "api-tls"},
                "status": {"conditions": [_gateway_condition("Ready", 1)]},
            },
            ("v1", "Secret", "gco-system", "api-tls"): {
                "data": {"tls.crt": "Y2VydA==", "tls.key": "a2V5"}
            },
        }

        result, dynamic_client = _validate_with_fake_dynamic(handler_module, tmp_path, live_objects)

        assert result["ValidatedCount"] == 1
        discovered = {entry.kwargs["kind"] for entry in dynamic_client.resources.get.call_args_list}
        assert discovered == {"Certificate", "Secret"}

    def test_failure_report_is_bounded_and_reuses_discovered_resources(
        self, handler_module, tmp_path
    ):
        count = handler_module._MAX_VALIDATION_FAILURES + 2
        documents = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": f"cm-{i}", "namespace": "d"},
            }
            for i in range(count)
        ]
        (tmp_path / "10-many.yaml").write_text(yaml.safe_dump_all(documents))

        with pytest.raises(RuntimeError) as error:
            _validate_with_fake_dynamic(handler_module, tmp_path, {})

        message = str(error.value)
        assert message.startswith(f"Manifest validation failed: validated=0 expected={count};")
        assert message.count("Kubernetes API error 404") == handler_module._MAX_VALIDATION_FAILURES
        assert message.endswith("; ... 2 additional failure(s)")

    def test_missing_api_resource_and_unexpected_errors_are_classified(
        self, handler_module, tmp_path
    ):
        documents = [
            {
                "apiVersion": "kueue.x-k8s.io/v1beta1",
                "kind": "ResourceFlavor",
                "metadata": {"name": "gco-default-flavor"},
            },
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "odd", "namespace": "d"},
            },
        ]
        (tmp_path / "post-helm-mixed.yaml").write_text(yaml.safe_dump_all(documents))
        dynamic_client = MagicMock()
        odd_resource = MagicMock()
        odd_resource.get.return_value = 42
        dynamic_client.resources.get.side_effect = [
            handler_module.ResourceNotFoundError("resourceflavors not registered"),
            odd_resource,
        ]

        with (
            patch.object(handler_module, "configure_k8s_client"),
            patch.object(handler_module.dynamic, "DynamicClient", return_value=dynamic_client),
            patch.object(handler_module.client, "ApiClient", return_value=MagicMock()),
            pytest.raises(RuntimeError) as error,
        ):
            handler_module.validate_manifests("cluster", "us-east-1", str(tmp_path), {})

        message = str(error.value)
        assert (
            "kueue.x-k8s.io/v1beta1/ResourceFlavor/<cluster>/gco-default-flavor "
            "[post-helm:post-helm-mixed.yaml]: object or API resource not found "
            "(resourceflavors not registered)"
        ) in message
        assert (
            "v1/ConfigMap/d/odd [post-helm:post-helm-mixed.yaml]: validation error "
            "(Kubernetes response is not a mapping: int)"
        ) in message


class TestPhaseStatusRecording:
    """Convergence phase status is a best-effort SSM write keyed by project and region."""

    def test_writes_a_json_parameter_when_configured(self, handler_module):
        ssm = MagicMock()

        with (
            patch.dict(os.environ, {"PROJECT_NAME": "gco", "REGION": "us-east-1"}),
            patch.object(handler_module.boto3, "client", return_value=ssm) as boto_client,
            patch.object(handler_module.time, "time", return_value=1_757_000_000.7),
        ):
            handler_module._record_phase_status("base-manifests", "applied", "applied=40 " * 200)

        boto_client.assert_called_once_with("ssm")
        kwargs = ssm.put_parameter.call_args.kwargs
        assert kwargs["Name"] == "/gco/addons/us-east-1/base-manifests"
        assert kwargs["Type"] == "String"
        assert kwargs["Overwrite"] is True
        value = json.loads(kwargs["Value"])
        assert value["phase"] == "base-manifests"
        assert value["status"] == "applied"
        assert value["updated_at"] == 1_757_000_000
        assert len(value["message"]) == 1024

    def test_unconfigured_environment_is_a_noop(self, handler_module):
        environment = {k: v for k, v in os.environ.items() if k not in ("PROJECT_NAME", "REGION")}

        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(handler_module.boto3, "client") as boto_client,
        ):
            handler_module._record_phase_status("base-manifests", "applied", "ok")

        boto_client.assert_not_called()

    def test_ssm_failures_are_swallowed(self, handler_module):
        ssm = MagicMock()
        ssm.put_parameter.side_effect = RuntimeError("AccessDeniedException")

        with (
            patch.dict(os.environ, {"PROJECT_NAME": "gco", "REGION": "us-east-1"}),
            patch.object(handler_module.boto3, "client", return_value=ssm),
        ):
            handler_module._record_phase_status("post-helm-manifests", "failed", "boom")

        ssm.put_parameter.assert_called_once()


class TestHandleTaskFailurePaths:
    """Every task action records a failed phase status before the error escapes."""

    _EVENT = {"ClusterName": "gco-us-east-1", "Region": "us-east-1"}

    def test_gateway_teardown_failure_is_recorded_then_raised(self, handler_module):
        with (
            patch.object(
                handler_module,
                "_delete_gateway_resources",
                side_effect=RuntimeError("timed out waiting"),
            ),
            patch.object(handler_module, "_record_phase_status") as record,
            pytest.raises(RuntimeError, match="timed out waiting"),
        ):
            handler_module.handle_task({**self._EVENT, "Action": "delete_gateway_resources"})

        record.assert_called_once_with("gateway-teardown", "failed", "timed out waiting")

    def test_unknown_action_is_rejected(self, handler_module):
        with pytest.raises(ValueError, match="Unsupported task action: reboot"):
            handler_module.handle_task({**self._EVENT, "Action": "reboot"})

    def test_apply_exception_is_recorded_then_raised(self, handler_module):
        with (
            patch.object(
                handler_module,
                "apply_manifests",
                side_effect=ValueError("Manifest planning failed: bad"),
            ),
            patch.object(handler_module, "_record_phase_status") as record,
            pytest.raises(ValueError, match="planning failed"),
        ):
            handler_module.handle_task({**self._EVENT, "Action": "apply_manifests"})

        record.assert_called_once_with("base-manifests", "failed", "Manifest planning failed: bad")

    def test_apply_count_mismatch_is_recorded_then_raised(self, handler_module):
        with (
            patch.object(
                handler_module,
                "apply_manifests",
                return_value={"AppliedCount": 3, "ExpectedCount": 4, "FailedCount": 0},
            ),
            patch.object(handler_module, "_record_phase_status") as record,
            pytest.raises(RuntimeError, match="apply count mismatch: applied=3 expected=4"),
        ):
            handler_module.handle_task(
                {**self._EVENT, "Action": "apply_manifests", "PostHelm": "true"}
            )

        record.assert_called_once_with(
            "post-helm-manifests", "failed", "apply count mismatch: applied=3 expected=4"
        )


class TestLambdaHandlerDelete:
    """Stack deletion always reports SUCCESS so CloudFormation never gets stuck."""

    @staticmethod
    def _event(properties):
        return {
            "RequestType": "Delete",
            "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/gco/abc",
            "RequestId": "req-1",
            "LogicalResourceId": "KubectlApply",
            "PhysicalResourceId": "kubectl-KubectlApply",
            "ResponseURL": "https://example.invalid/response",
            "ResourceProperties": properties,
        }

    @pytest.mark.parametrize("skip_deletion", ["true", "false"])
    def test_delete_reports_deleted_without_touching_the_cluster(
        self, handler_module, caplog, skip_deletion
    ):
        event = self._event(
            {"ClusterName": "c", "Region": "us-east-1", "SkipDeletionOnStackDelete": skip_deletion}
        )

        with (
            patch.object(handler_module, "apply_manifests") as apply,
            patch.object(handler_module, "send_response") as send,
            caplog.at_level(logging.INFO),
        ):
            handler_module.lambda_handler(event, MagicMock())

        apply.assert_not_called()
        send.assert_called_once()
        assert send.call_args.args[2:] == (
            handler_module.SUCCESS,
            {"Status": "Deleted"},
            "kubectl-KubectlApply",
        )
        assert ("Skipping deletion" in caplog.text) is (skip_deletion == "true")

    def test_delete_forces_success_when_the_event_is_malformed(self, handler_module):
        event = self._event({"Region": "us-east-1"})

        with patch.object(handler_module, "send_response") as send:
            handler_module.lambda_handler(event, MagicMock())

        send.assert_called_once()
        assert send.call_args.args[2:] == (
            handler_module.SUCCESS,
            {"Status": "Forced success on delete"},
            "kubectl-KubectlApply",
        )

    def test_unrecognized_request_type_fails_the_resource_without_applying(self, handler_module):
        """A RequestType other than Create/Update/Delete gets a FAILED callback.

        CloudFormation never sends one, but if it did, staying silent would
        leave the stack waiting on this resource until its timeout.
        """
        event = {**self._event({"ClusterName": "c", "Region": "us-east-1"}), "RequestType": "Read"}
        context = MagicMock()

        with (
            patch.object(handler_module, "apply_manifests") as apply,
            patch.object(handler_module, "send_response") as send,
        ):
            handler_module.lambda_handler(event, context)

        apply.assert_not_called()
        send.assert_called_once_with(
            event,
            context,
            handler_module.FAILED,
            {},
            event["PhysicalResourceId"],
            "Unsupported RequestType: Read",
        )
