"""
Tests for Kubeflow Trainer chart wiring, the runtime manifest, and gating.

Mirrors tests/test_cost_opencost_charts.py for the training pipeline: the
static kubeflow-trainer entry in charts.yaml (OCI source, version + image
pins in lockstep, chart-shipped runtimes off, non-default namespace,
bounded wait), the GCORegionalStack enablement behavior in both directions
(cdk.json toggle off → chart absent → installer uninstalls on convergence;
helm_enabled_overrides forces on), the {{KUBEFLOW_TRAINER_ENABLED}}-gated
ClusterTrainingRuntime manifest and its exact-GVK prune inventory entry,
CRUD acceptance of the pinned TrainJob GVK only, and helm-installer
handle_task convergence for the chart.
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from gco.config.config_loader import ConfigLoader
from gco.services.manifest_processor import TRAINJOB_API_VERSION
from gco.stacks.regional_stack import (
    _HELM_CHART_CONFIG_KEYS,
    _compute_kubectl_scheduler_replacements,
)
from gco.stacks.regional_stack import GCORegionalStack as RS
from tests._lambda_imports import load_lambda_module

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHARTS_YAML = _REPO_ROOT / "lambda" / "helm-installer" / "charts.yaml"
_RUNTIME_MANIFEST = (
    _REPO_ROOT
    / "lambda"
    / "kubectl-applier-simple"
    / "manifests"
    / "post-helm-kubeflow-trainer-runtimes.yaml"
)


class _MockNode:
    def __init__(self, context: dict[str, Any]):
        self._context = context

    def try_get_context(self, key: str) -> Any:
        return self._context.get(key)


class _MockApp:
    def __init__(self, context: dict[str, Any]):
        self.node = _MockNode(context)


@pytest.fixture
def valid_cdk_context() -> dict[str, Any]:
    return {
        "project_name": "gco",
        "deployment_regions": {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
            "regional": ["us-east-1"],
        },
        "kubernetes_version": "1.36",
        "resource_thresholds": {
            "cpu_threshold": 80,
            "memory_threshold": 85,
            "gpu_threshold": 90,
        },
        "global_accelerator": {
            "health_check_grace_period": 30,
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "health_check_path": "/api/v1/health",
        },
        "alb_config": {
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "healthy_threshold": 2,
            "unhealthy_threshold": 2,
        },
        "manifest_processor": {
            "image": "gco/manifest-processor:latest",
            "replicas": 3,
            "resource_limits": {"cpu": "1000m", "memory": "2Gi"},
        },
        "job_validation_policy": {
            "allowed_namespaces": ["gco-jobs"],
            "resource_quotas": {
                "max_cpu_per_manifest": "10",
                "max_memory_per_manifest": "32Gi",
                "max_gpu_per_manifest": 4,
            },
        },
        "api_gateway": {
            "throttle_rate_limit": 1000,
            "throttle_burst_limit": 2000,
            "log_level": "INFO",
            "metrics_enabled": True,
            "tracing_enabled": True,
        },
        "tags": {"Environment": "test"},
    }


def _stub(
    context: dict[str, Any],
    *,
    helm: dict[str, Any] | None = None,
    helm_enabled_overrides: str | None = None,
):
    """GCORegionalStack stand-in exercising the real chart-enable resolution.

    Observability and cost stay off so the enabled set is purely the
    helm-block outcome under test.
    """
    ctx = copy.deepcopy(context)
    ctx["cluster_observability"] = {"enabled": False}
    ctx["cost_monitoring"] = {"enabled": False}
    if helm is not None:
        ctx["helm"] = helm
    if helm_enabled_overrides is not None:
        ctx["helm_enabled_overrides"] = helm_enabled_overrides
    app = _MockApp(ctx)
    config = ConfigLoader(app)
    stub = SimpleNamespace(config=config, node=app.node)
    stub._cost_monitoring_active = lambda: RS._cost_monitoring_active(stub)
    return stub


@pytest.fixture(scope="module")
def charts() -> dict[str, Any]:
    with open(_CHARTS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)["charts"]


class TestKubeflowTrainerChartEntry:
    def test_chart_comes_from_the_kubeflow_oci_registry(self, charts):
        trainer = charts["kubeflow-trainer"]
        assert trainer["repo_url"] == "oci://ghcr.io/kubeflow/charts"
        assert trainer["use_oci"] is True
        assert trainer["chart"] == "kubeflow-trainer"

    def test_chart_version_is_pinned(self, charts):
        assert re.fullmatch(r"\d+\.\d+\.\d+", charts["kubeflow-trainer"]["version"])

    def test_chart_is_enabled_by_default(self, charts):
        # 6.0 decision: the trainer ships on by default; opting out goes
        # through cdk.json helm.kubeflow_trainer.enabled (upgrade guide).
        assert charts["kubeflow-trainer"]["enabled"] is True

    def test_chart_installs_into_its_own_namespace(self, charts):
        trainer = charts["kubeflow-trainer"]
        assert trainer["namespace"] == "kubeflow-trainer"
        assert trainer["create_namespace"] is True

    def test_chart_wait_is_bounded_below_the_helm_guard(self, charts):
        # wait: true is safe only because the chart's slow post-install hook
        # (runtimes Job) is disabled below; the 8m ceiling keeps a wedged
        # install well inside the installer's synchronous budget.
        trainer = charts["kubeflow-trainer"]
        assert trainer["wait"] is True
        assert trainer["wait_timeout"] == "8m"

    def test_chart_orders_before_kueue_which_stays_last(self, charts):
        order = list(charts)
        assert order.index("kubeflow-trainer") < order.index("kueue")
        assert order[-1] == "kueue"

    def test_controller_image_tag_is_pinned_in_lockstep_with_the_chart(self, charts):
        trainer = charts["kubeflow-trainer"]
        image = trainer["values"]["image"]
        assert image["registry"] == "ghcr.io"
        assert image["repository"] == "kubeflow/trainer/trainer-controller-manager"
        # The chart defaults the tag to its own version; pinning it explicitly
        # feeds the deps-drift report. Keep tag == "v" + chart version.
        assert image["tag"] == f"v{trainer['version']}"

    def test_chart_shipped_runtimes_stay_disabled(self, charts):
        # Upstream delivers runtimes via a post-install hook Job that
        # downloads kubectl into an alpine pod at run time (unpinned fetch,
        # burns helm --wait budget). GCO ships the runtime through the
        # post-Helm kubectl pass instead.
        runtimes = charts["kubeflow-trainer"]["values"]["runtimes"]
        assert runtimes["defaultEnabled"] is False
        assert runtimes["torchDistributed"]["enabled"] is False

    def test_crds_render_as_templates_and_jobset_subchart_installs(self, charts):
        values = charts["kubeflow-trainer"]["values"]
        assert values["crds"]["enabled"] is True
        assert values["jobset"]["install"] is True

    def test_controller_is_protected_from_consolidation(self, charts):
        manager = charts["kubeflow-trainer"]["values"]["manager"]
        assert manager["podAnnotations"]["karpenter.sh/do-not-disrupt"] == "true"
        assert manager["replicas"] == 1
        assert manager["resources"]["requests"]["cpu"]
        assert manager["resources"]["limits"]["memory"]


class TestTrainerRuntimeManifest:
    @pytest.fixture(scope="class")
    def manifest_text(self) -> str:
        return _RUNTIME_MANIFEST.read_text(encoding="utf-8")

    def test_manifest_is_gated_on_the_trainer_placeholder(self, manifest_text):
        assert "{{KUBEFLOW_TRAINER_ENABLED}}" in manifest_text

    def test_no_other_upper_snake_tokens_leak(self, manifest_text):
        body = manifest_text.replace("{{KUBEFLOW_TRAINER_ENABLED}}", "")
        assert not re.search(r"\{\{[A-Z][A-Z0-9_]*\}\}", body)

    def test_manifest_renders_one_torch_distributed_runtime(self, manifest_text):
        rendered = manifest_text.replace("{{KUBEFLOW_TRAINER_ENABLED}}", "true")
        (runtime,) = [doc for doc in yaml.safe_load_all(rendered) if doc]
        assert runtime["apiVersion"] == TRAINJOB_API_VERSION
        assert runtime["kind"] == "ClusterTrainingRuntime"
        assert runtime["metadata"]["name"] == "torch-distributed"

    def test_runtime_keeps_the_upstream_torch_policy(self, manifest_text):
        rendered = manifest_text.replace("{{KUBEFLOW_TRAINER_ENABLED}}", "true")
        (runtime,) = [doc for doc in yaml.safe_load_all(rendered) if doc]
        ml_policy = runtime["spec"]["mlPolicy"]
        assert ml_policy["numNodes"] == 1
        assert "torch" in ml_policy
        replicated_jobs = runtime["spec"]["template"]["spec"]["replicatedJobs"]
        assert [job["name"] for job in replicated_jobs] == ["node"]

    def test_runtime_image_is_the_pinned_trusted_pytorch_build(self, manifest_text):
        from gco.services.manifest_processor import DEFAULT_TRUSTED_DOCKERHUB_ORGS

        rendered = manifest_text.replace("{{KUBEFLOW_TRAINER_ENABLED}}", "true")
        (runtime,) = [doc for doc in yaml.safe_load_all(rendered) if doc]
        pod_spec = runtime["spec"]["template"]["spec"]["replicatedJobs"][0]["template"]["spec"][
            "template"
        ]["spec"]
        (container,) = pod_spec["containers"]
        image = container["image"]
        assert image == "pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime"
        assert image.split("/")[0] in DEFAULT_TRUSTED_DOCKERHUB_ORGS

    def test_runtime_pods_do_not_automount_the_sa_token(self, manifest_text):
        # The one documented deviation from verbatim chart extraction: the
        # same SA-token default both submission paths inject into user pods.
        rendered = manifest_text.replace("{{KUBEFLOW_TRAINER_ENABLED}}", "true")
        (runtime,) = [doc for doc in yaml.safe_load_all(rendered) if doc]
        pod_spec = runtime["spec"]["template"]["spec"]["replicatedJobs"][0]["template"]["spec"][
            "template"
        ]["spec"]
        assert pod_spec["automountServiceAccountToken"] is False


class TestRegionalChartWiring:
    def test_chart_enabled_by_default(self, valid_cdk_context):
        charts = RS._get_enabled_helm_charts(_stub(valid_cdk_context))
        assert "kubeflow-trainer" in charts

    def test_chart_absent_when_toggle_off(self, valid_cdk_context):
        charts = RS._get_enabled_helm_charts(
            _stub(valid_cdk_context, helm={"kubeflow_trainer": {"enabled": False}})
        )
        assert "kubeflow-trainer" not in charts

    def test_override_forces_the_chart_on_for_one_deploy(self, valid_cdk_context):
        # The live-validation harness threads helm_enabled_overrides through
        # every CDK invocation instead of editing cdk.json.
        charts = RS._get_enabled_helm_charts(
            _stub(
                valid_cdk_context,
                helm={"kubeflow_trainer": {"enabled": False}},
                helm_enabled_overrides="kubeflow_trainer",
            )
        )
        assert "kubeflow-trainer" in charts

    def test_enabled_order_keeps_trainer_before_kueue(self, valid_cdk_context):
        charts = RS._get_enabled_helm_charts(_stub(valid_cdk_context))
        assert charts.index("kubeflow-trainer") < charts.index("kueue")

    def test_toggle_key_is_a_registered_chart_config_key(self):
        assert "kubeflow_trainer" in _HELM_CHART_CONFIG_KEYS


class TestSchedulerGateReplacements:
    def test_enabled_trainer_resolves_the_gate(self):
        replacements = _compute_kubectl_scheduler_replacements(
            kueue_enabled=False, slurm_enabled=False, kubeflow_trainer_enabled=True
        )
        assert replacements == {"{{KUBEFLOW_TRAINER_ENABLED}}": "true"}

    def test_disabled_trainer_leaves_the_gate_unresolved(self):
        replacements = _compute_kubectl_scheduler_replacements(
            kueue_enabled=True, slurm_enabled=False, kubeflow_trainer_enabled=False
        )
        assert "{{KUBEFLOW_TRAINER_ENABLED}}" not in replacements


class TestApplierTrainerWiring:
    @pytest.fixture(scope="class")
    def applier(self):
        handler_path = str(_REPO_ROOT / "lambda" / "kubectl-applier-simple")
        sys.path.insert(0, handler_path)
        try:
            sys.modules.pop("handler", None)
            import handler

            yield handler
        finally:
            sys.path.pop(0)
            sys.modules.pop("handler", None)

    def test_cluster_training_runtime_is_a_supported_cluster_scoped_kind(self, applier):
        assert "ClusterTrainingRuntime" in applier._SUPPORTED_MANIFEST_KINDS
        assert "ClusterTrainingRuntime" in applier._CLUSTER_SCOPED_KINDS

    def test_prune_inventory_removes_the_runtime_when_disabled(self, applier):
        targets = applier._FEATURE_RESOURCE_INVENTORY[("{{KUBEFLOW_TRAINER_ENABLED}}", True)]
        assert targets == (
            (TRAINJOB_API_VERSION, "ClusterTrainingRuntime", None, "torch-distributed"),
        )

    def test_inventory_matches_the_gated_manifest_resources(self, applier):
        """Every gated resource in the runtime manifest has a prune entry."""
        rendered = _RUNTIME_MANIFEST.read_text(encoding="utf-8").replace(
            "{{KUBEFLOW_TRAINER_ENABLED}}", "true"
        )
        manifest_resources = {
            (doc["apiVersion"], doc["kind"], doc["metadata"]["name"])
            for doc in yaml.safe_load_all(rendered)
            if doc
        }
        pruned = {
            (api, kind, name)
            for api, kind, _, name in applier._FEATURE_RESOURCE_INVENTORY[
                ("{{KUBEFLOW_TRAINER_ENABLED}}", True)
            ]
        }
        assert manifest_resources == pruned


class TestTrainJobCrudAcceptance:
    """CRUD endpoints accept the pinned TrainJob GVK and nothing near it."""

    @pytest.fixture
    def processor(self):
        from kubernetes import config as k8s_config

        with (
            patch("gco.services.manifest_processor.config") as mock_config,
            patch("gco.services.manifest_processor.client"),
        ):
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException("no")
            mock_config.load_kube_config.return_value = None
            from gco.services.manifest_processor import ManifestProcessor

            yield ManifestProcessor(
                cluster_id="test-cluster",
                region="us-east-1",
                config_dict={"allowed_namespaces": ["gco-jobs"]},
            )

    def test_pinned_gvk_is_accepted(self, processor):
        assert (
            processor._resource_access_error(TRAINJOB_API_VERSION, "TrainJob", "gco-jobs") is None
        )

    def test_foreign_group_with_the_same_kind_is_rejected(self, processor):
        error = processor._resource_access_error("example.com/v1alpha1", "TrainJob", "gco-jobs")
        assert error is not None
        assert "not allowed for kind 'TrainJob'" in error

    def test_other_api_version_of_the_trainer_group_is_rejected(self, processor):
        error = processor._resource_access_error(
            "trainer.kubeflow.org/v1beta1", "TrainJob", "gco-jobs"
        )
        assert error is not None


class TestHelmInstallerConvergence:
    """handle_task converges the trainer chart in both directions."""

    @pytest.fixture(scope="class")
    def helm_handler(self):
        return load_lambda_module("helm-installer")

    def _event(self, enabled: bool) -> dict[str, Any]:
        return {
            "Action": "install_chart",
            "Chart": "kubeflow-trainer",
            "ClusterName": "gco-us-east-1",
            "Region": "us-east-1",
            "EnabledCharts": ["keda", "kubeflow-trainer"] if enabled else ["keda"],
            "Charts": {},
        }

    def test_enabled_chart_installs(self, helm_handler):
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(
                helm_handler,
                "install_chart",
                return_value=(True, "Successfully installed kubeflow-trainer"),
            ) as mock_install,
            patch.object(helm_handler.os, "remove"),
        ):
            result = helm_handler.handle_task(self._event(enabled=True))

        assert result["status"] == "installed"
        assert result["chart"] == "kubeflow-trainer"
        mock_install.assert_called_once()

    def test_disabled_chart_uninstalls_on_the_same_pass(self, helm_handler):
        # EnabledCharts is the runtime authority: a chart missing from it is
        # UNINSTALLED by its install task, which is how flipping the cdk.json
        # toggle off actually removes the trainer on the next deploy.
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(
                helm_handler,
                "uninstall_chart",
                return_value=(True, "Successfully uninstalled"),
            ) as mock_uninstall,
            patch.object(helm_handler, "install_chart") as mock_install,
            patch.object(helm_handler.os, "remove"),
        ):
            result = helm_handler.handle_task(self._event(enabled=False))

        assert result["status"] == "uninstalled"
        mock_uninstall.assert_called_once()
        mock_install.assert_not_called()

    def test_trainer_is_not_a_finalizer_purge_chart(self, helm_handler):
        """Trainer v2.3.0 never ADDS its resource-in-use finalizer (verified
        against upstream source: controllers only remove it, legacy cleanup),
        so uninstall needs no kueue-style pre-purge. If a future bump starts
        attaching finalizers, add the API group and delete this pin."""
        assert "kubeflow-trainer" not in helm_handler.CHART_CUSTOM_RESOURCE_API_GROUPS
