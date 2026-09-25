"""Tests for the self-managed platform add-ons: Argo CD, Crossplane and Crossview.

Mirrors tests/test_kubeflow_trainer_charts.py for the two opt-in add-ons:

* the static charts.yaml entries — pinned versions, off in charts.yaml (the
  stack decides), namespaces, bounded waits, install order before Kueue, and
  the values GCO's posture depends on (Argo CD's namespaced mode and in-cluster
  destination fenced to the tenant namespaces, no chart NetworkPolicies, no
  Dex; Crossview with auth none, no database, no chart RBAC, the ServiceAccount
  name the post-Helm binding names);
* the shipped cdk.json blocks (present, off, valid);
* GCORegionalStack chart selection — absent block means off (unlike the
  historical charts), the toggle and ``helm_enabled_overrides`` turn a chart on,
  and one ``crossplane`` toggle installs Crossplane and Crossview together;
* the kubectl-applier replacements and the Argo CD chart values (repo-server
  size and autoscaler) a synthesized stack hands the post-Helm pass and the
  helm installer (two synths: shipped default, everything on), and the
  stack-delete budgets;
* the helm installer's custom-resource cleanup groups and install/uninstall
  convergence for the three charts.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import assertions

from gco import argocd_config as ac
from gco.config.config_loader import ConfigLoader
from gco.enablement_overrides import HELM_CHART_CONFIG_KEYS
from gco.stacks import regional_stack as rs
from gco.stacks.regional_stack import GCORegionalStack as RS
from tests._lambda_imports import load_lambda_module
from tests.test_regional_stack import MockConfigLoader
from tests.test_regional_stack import TestRegionalStackSynthesis as _SynthFixtures

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHARTS_YAML = _REPO_ROOT / "lambda" / "helm-installer" / "charts.yaml"
_MANIFESTS = _REPO_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
_ACCOUNT = "123456789012"
_REGION = "us-east-1"


@pytest.fixture(scope="module")
def charts() -> dict[str, Any]:
    return yaml.safe_load(_CHARTS_YAML.read_text(encoding="utf-8"))["charts"]


@pytest.fixture(scope="module")
def cdk_context() -> dict[str, Any]:
    return json.loads((_REPO_ROOT / "cdk.json").read_text(encoding="utf-8"))["context"]


# ─── charts.yaml ─────────────────────────────────────────────────────────────


class TestArgoCdChartEntry:
    def test_upstream_chart_pinned_and_off_in_charts_yaml(self, charts: dict[str, Any]) -> None:
        entry = charts["argocd"]
        assert entry["repo_url"] == "https://argoproj.github.io/argo-helm"
        assert entry["chart"] == "argo-cd"
        assert re.fullmatch(r"\d+\.\d+\.\d+", entry["version"])
        # charts.yaml is the fallback; cdk.json helm.argocd (off by default)
        # decides through the stack's EnabledCharts.
        assert entry["enabled"] is False

    def test_release_and_namespace_give_the_upstream_names(self, charts: dict[str, Any]) -> None:
        # The release name is the charts.yaml key; "argocd" makes every object
        # argocd-* (argocd-server, argocd-initial-admin-secret) — the names the
        # CLI, the post-Helm RBAC and the kind job use.
        assert ac.ARGOCD_CHART_NAME == "argocd"
        entry = charts[ac.ARGOCD_CHART_NAME]
        assert entry["namespace"] == ac.ARGOCD_NAMESPACE
        assert entry["create_namespace"] is True
        assert entry["wait"] is True and entry["wait_timeout"] == "8m"

    def test_namespaced_mode_with_the_fenced_in_cluster_destination(
        self, charts: dict[str, Any]
    ) -> None:
        values = charts["argocd"]["values"]
        assert values["createClusterRoles"] is False
        in_cluster = values["configs"]["clusterCredentials"]["in-cluster"]
        assert in_cluster["server"] == "https://kubernetes.default.svc"
        assert in_cluster["namespaces"] == ",".join(ac.GITOPS_TENANT_NAMESPACES)
        assert "clusterResources" not in in_cluster
        assert in_cluster["config"]["tlsClientConfig"]["insecure"] is False
        assert values["configs"]["cm"]["resource.respectRBAC"] == "normal"

    def test_ui_posture(self, charts: dict[str, Any]) -> None:
        values = charts["argocd"]["values"]
        # TLS terminates at the kubectl port-forward (API server TLS); there
        # is no ingress, and no exec into pods from the UI.
        assert values["configs"]["params"]["server.insecure"] is True
        assert values["configs"]["cm"]["exec.enabled"] is False
        assert values["configs"]["cm"]["admin.enabled"] is True
        assert "ingress" not in values.get("server", {})
        assert values["dex"]["enabled"] is False
        assert values["notifications"]["enabled"] is False
        assert values["applicationSet"]["replicas"] == 0

    def test_no_chart_network_policies(self, charts: dict[str, Any]) -> None:
        # Kubelet probes arrive from the node network; the chart's
        # pod-selector-only policies would drop them under the VPC CNI agent.
        assert charts["argocd"]["values"]["global"]["networkPolicy"]["create"] is False

    def test_crds_install_and_survive_uninstall(self, charts: dict[str, Any]) -> None:
        crds = charts["argocd"]["values"]["crds"]
        assert crds == {"install": True, "keep": True}

    def test_controllers_are_bounded_and_protected(self, charts: dict[str, Any]) -> None:
        values = charts["argocd"]["values"]
        assert values["global"]["podAnnotations"]["karpenter.sh/do-not-disrupt"] == "true"
        for component in ("controller", "server", "repoServer", "redis"):
            resources = values[component]["resources"]
            assert resources["requests"]["cpu"] and resources["requests"]["memory"]
            assert resources["limits"]["cpu"] and resources["limits"]["memory"]


class TestCrossplaneChartEntries:
    def test_crossplane_pinned_and_off(self, charts: dict[str, Any]) -> None:
        entry = charts["crossplane"]
        assert entry["repo_url"] == "https://charts.crossplane.io/stable"
        assert entry["chart"] == "crossplane"
        assert re.fullmatch(r"\d+\.\d+\.\d+", entry["version"])
        assert entry["enabled"] is False
        assert entry["namespace"] == "crossplane-system"
        assert entry["create_namespace"] is True
        assert entry["wait"] is True and entry["wait_timeout"] == "8m"
        values = entry["values"]
        assert values["replicas"] == 1
        assert values["customAnnotations"]["karpenter.sh/do-not-disrupt"] == "true"
        for key in ("resourcesCrossplane", "resourcesRBACManager"):
            assert values[key]["limits"]["memory"]

    def test_crossview_pinned_in_lockstep(self, charts: dict[str, Any]) -> None:
        entry = charts["crossview"]
        assert entry["repo_url"] == "https://crossplane-contrib.github.io/crossview"
        assert entry["enabled"] is False
        # Installed into Crossplane's namespace, which the crossplane release
        # creates first.
        assert entry["namespace"] == "crossplane-system"
        assert entry["create_namespace"] is False
        image = entry["values"]["image"]
        assert image["repository"] == "ghcr.io/crossplane-contrib/crossview"
        assert image["tag"] == f"v{entry['version']}"

    def test_crossview_runs_read_only_behind_the_port_forward(self, charts: dict[str, Any]) -> None:
        values = charts["crossview"]["values"]
        assert values["config"]["server"]["auth"]["mode"] == "none"
        assert values["database"]["enabled"] is False
        assert values["config"]["database"]["enabled"] is False
        # No literal admin/password pair in a chart-created Secret.
        assert values["secrets"] == {"adminUsername": "", "adminPassword": ""}
        # The chart's ClusterRole reads every resource, Secrets included; GCO's
        # post-Helm bindings replace it.
        assert values["rbac"]["create"] is False
        assert values["serviceAccount"] == {"create": True, "name": "crossview"}
        assert values["podAnnotations"]["karpenter.sh/do-not-disrupt"] == "true"

    def test_crossview_service_account_matches_the_post_helm_bindings(
        self, charts: dict[str, Any]
    ) -> None:
        name = charts["crossview"]["values"]["serviceAccount"]["name"]
        manifest = (_MANIFESTS / "post-helm-crossplane.yaml").read_text(encoding="utf-8")
        documents = [
            doc
            for doc in yaml.safe_load_all(manifest.replace("{{CROSSPLANE_ENABLED}}", "true"))
            if doc
        ]
        crossview_bindings = [
            doc
            for doc in documents
            if doc["kind"] == "ClusterRoleBinding"
            and doc["metadata"]["name"].startswith("gco-crossview")
        ]
        assert len(crossview_bindings) == 2
        for binding in crossview_bindings:
            assert binding["subjects"] == [
                {
                    "kind": "ServiceAccount",
                    "name": name,
                    "namespace": charts["crossview"]["namespace"],
                }
            ]

    def test_install_order(self, charts: dict[str, Any]) -> None:
        order = list(charts)
        assert order.index("argocd") < order.index("kueue")
        assert order.index("crossplane") < order.index("crossview") < order.index("kueue")
        assert order[-1] == "kueue"


# ─── cdk.json ────────────────────────────────────────────────────────────────


class TestShippedCdkJson:
    def test_argocd_block_is_present_off_and_valid(self, cdk_context: dict[str, Any]) -> None:
        block = cdk_context["helm"]["argocd"]
        assert ac.validate_argocd_config(block) == ac.ARGOCD_DEFAULTS
        assert block["enabled"] is False

    def test_crossplane_block_is_present_and_off(self, cdk_context: dict[str, Any]) -> None:
        assert cdk_context["helm"]["crossplane"] == {"enabled": False}

    def test_both_are_helm_config_keys(self) -> None:
        assert {"argocd", "crossplane"} <= HELM_CHART_CONFIG_KEYS
        assert HELM_CHART_CONFIG_KEYS == rs._HELM_CHART_CONFIG_KEYS


# ─── Chart selection ─────────────────────────────────────────────────────────


class _MockNode:
    def __init__(self, context: dict[str, Any]) -> None:
        self._context = context

    def try_get_context(self, key: str) -> Any:
        return self._context.get(key)


def _selection_stub(
    cdk_context: dict[str, Any],
    *,
    helm: dict[str, Any] | None = None,
    helm_enabled_overrides: str | None = None,
) -> SimpleNamespace:
    context = copy.deepcopy(cdk_context)
    context["cluster_observability"] = {"enabled": False}
    context["cost_monitoring"] = {"enabled": False}
    if helm is not None:
        context["helm"] = helm
    if helm_enabled_overrides is not None:
        context["helm_enabled_overrides"] = helm_enabled_overrides
    node = _MockNode(context)
    config = ConfigLoader(SimpleNamespace(node=node))
    stub = SimpleNamespace(config=config, node=node)
    stub._cost_monitoring_active = lambda: RS._cost_monitoring_active(stub)
    stub._mlflow_active = lambda: RS._mlflow_active(stub)
    return stub


class TestChartSelection:
    def test_shipped_cdk_json_installs_neither(self, cdk_context: dict[str, Any]) -> None:
        enabled = RS._get_enabled_helm_charts(_selection_stub(cdk_context))
        assert not {"argocd", "crossplane", "crossview"} & set(enabled)

    def test_absent_blocks_mean_off(self, cdk_context: dict[str, Any]) -> None:
        helm = {
            key: value
            for key, value in cdk_context["helm"].items()
            if key not in {"argocd", "crossplane"}
        }
        enabled = RS._get_enabled_helm_charts(_selection_stub(cdk_context, helm=helm))
        assert not {"argocd", "crossplane", "crossview"} & set(enabled)
        # ...unlike a historical chart, whose missing key still means on.
        assert "volcano" in enabled

    def test_toggles_turn_them_on_in_order(self, cdk_context: dict[str, Any]) -> None:
        helm = copy.deepcopy(cdk_context["helm"])
        helm["argocd"]["enabled"] = True
        helm["crossplane"]["enabled"] = True
        enabled = RS._get_enabled_helm_charts(_selection_stub(cdk_context, helm=helm))
        assert "argocd" in enabled
        crossplane = enabled.index("crossplane")
        # One toggle, both releases, Crossplane first.
        assert enabled[crossplane + 1] == "crossview"
        assert enabled.index("argocd") < enabled.index("kueue")
        assert enabled.index("crossview") < enabled.index("kueue")

    def test_overrides_force_them_on_for_one_deploy(self, cdk_context: dict[str, Any]) -> None:
        enabled = RS._get_enabled_helm_charts(
            _selection_stub(cdk_context, helm_enabled_overrides="argocd,crossplane")
        )
        assert {"argocd", "crossplane", "crossview"} <= set(enabled)

    @pytest.mark.parametrize(
        ("helm", "key", "expected"),
        [
            ({}, "argocd", False),
            ({"argocd": {}}, "argocd", False),
            ({"argocd": True}, "argocd", False),
            ({"argocd": {"enabled": True}}, "argocd", True),
            ({}, "crossplane", False),
            ({}, "volcano", True),
            ({"volcano": True}, "volcano", True),
            ({"volcano": {"enabled": False}}, "volcano", False),
        ],
    )
    def test_enablement_defaults(self, helm: dict[str, Any], key: str, expected: bool) -> None:
        assert rs._helm_chart_enabled(helm, frozenset(), key) is expected

    def test_off_by_default_keys_are_optional_chart_keys(self) -> None:
        assert {"argocd", "crossplane"} == rs._OFF_BY_DEFAULT_CHART_KEYS
        assert rs._OFF_BY_DEFAULT_CHART_KEYS <= rs._HELM_CHART_CONFIG_KEYS
        assert not rs._OFF_BY_DEFAULT_CHART_KEYS & rs._MANDATORY_CHART_KEYS


class TestStackDeleteBudgets:
    def test_custom_resource_charts_get_longer_uninstall_tasks(self) -> None:
        assert rs._UNINSTALL_TIMEOUT_MINUTES == {"keda": 4, "argocd": 4, "crossplane": 6}

    def test_every_extended_budget_belongs_to_a_cleanup_chart(self, helm_handler: Any) -> None:
        assert set(rs._UNINSTALL_TIMEOUT_MINUTES) <= set(
            helm_handler.CHART_CUSTOM_RESOURCE_API_GROUPS
        )


# ─── Chart value overrides ───────────────────────────────────────────────────


def _overrides_stub(cdk_context: dict[str, Any], **kwargs: Any) -> SimpleNamespace:
    """``_selection_stub`` plus what ``_helm_chart_value_overrides`` reads."""
    stub = _selection_stub(cdk_context, **kwargs)
    stub.volcano_mirror_registry = None
    stub.cluster = SimpleNamespace(cluster_name=f"gco-{_REGION}")
    stub.deployment_region = _REGION
    stub.vpc = SimpleNamespace(vpc_id="vpc-0123456789abcdef0")
    stub.aws_load_balancer_controller_role = SimpleNamespace(
        role_arn=f"arn:aws:iam::{_ACCOUNT}:role/test-lbc"
    )
    stub._argocd_config = lambda: RS._argocd_config(stub)
    return stub


class TestArgoCdChartValueOverrides:
    def test_off_sends_no_argocd_values(self, cdk_context: dict[str, Any]) -> None:
        assert "argocd" not in RS._helm_chart_value_overrides(_overrides_stub(cdk_context))

    def test_toggle_sends_the_repo_server_autoscaler(self, cdk_context: dict[str, Any]) -> None:
        helm = copy.deepcopy(cdk_context["helm"])
        helm["argocd"]["enabled"] = True
        helm["argocd"]["repo_server"] = {
            "replicas": 2,
            "autoscaling": {"enabled": True, "max_replicas": 6},
        }
        overrides = RS._helm_chart_value_overrides(_overrides_stub(cdk_context, helm=helm))
        assert overrides["argocd"] == {
            "values": ac.argocd_chart_values(ac.validate_argocd_config(helm["argocd"]))
        }
        hpa = overrides["argocd"]["values"]["repoServer"]["autoscaling"]
        assert (hpa["enabled"], hpa["minReplicas"], hpa["maxReplicas"]) == (True, 2, 6)

    def test_run_scoped_enable_sends_the_configured_size(self, cdk_context: dict[str, Any]) -> None:
        helm = copy.deepcopy(cdk_context["helm"])
        helm["argocd"]["repo_server"] = {"replicas": 3}
        overrides = RS._helm_chart_value_overrides(
            _overrides_stub(cdk_context, helm=helm, helm_enabled_overrides="argocd")
        )
        assert overrides["argocd"]["values"]["repoServer"] == {
            "replicas": 3,
            "autoscaling": {"enabled": False},
        }


# ─── Synthesized replacements ────────────────────────────────────────────────

_ARGOCD_ON = {
    "enabled": True,
    "source_repos": ["https://github.com/example/*"],
    "gitops": {
        "repo_url": "https://github.com/example/tenants.git",
        "revision": "main",
        "path": "clusters/{cluster_name}",
        "sync_policy": "automated",
    },
    "repo_server": {"replicas": 2, "autoscaling": {"enabled": True, "max_replicas": 4}},
}


def _synth(helm: dict[str, Any] | None) -> tuple[RS, dict[str, Any]]:
    app = cdk.App(context={"helm": helm} if helm is not None else None)
    config = MockConfigLoader(app)
    argocd = ac.validate_argocd_config((helm or {}).get("argocd"))
    with (
        patch.object(
            MockConfigLoader, "get_argocd_config", lambda self: copy.deepcopy(argocd), create=True
        ),
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(RS, "_create_helm_installer_lambda", _SynthFixtures._mock_helm_installer),
    ):
        mock_image = MagicMock()
        mock_image.image_uri = f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/test:latest"
        mock_docker.return_value = mock_image
        stack = RS(
            app,
            "test-platform-add-ons",
            config=config,
            region=_REGION,
            auth_secret_arn=f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:test-secret",  # nosec B106
            env=cdk.Environment(account=_ACCOUNT, region=_REGION),
        )
    return stack, assertions.Template.from_stack(stack).to_json()


def _trigger_properties(template: dict[str, Any]) -> dict[str, Any]:
    (resource,) = [
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("HelmInstallCharts")
        and resource["Type"] == "AWS::CloudFormation::CustomResource"
    ]
    return resource["Properties"]


@pytest.fixture(scope="module")
def shipped_default() -> tuple[RS, dict[str, Any]]:
    return _synth(None)


@pytest.fixture(scope="module")
def everything_on() -> tuple[RS, dict[str, Any]]:
    return _synth({"argocd": copy.deepcopy(_ARGOCD_ON), "crossplane": {"enabled": True}})


class TestSynthesizedReplacements:
    def test_default_emits_no_add_on_token(self, shipped_default) -> None:
        stack, template = shipped_default
        properties = _trigger_properties(template)
        replacements = properties["ImageReplacements"]
        assert not [key for key in replacements if key.startswith(("{{ARGOCD_", "{{CROSSPLANE_"))]
        assert not {"argocd", "crossplane", "crossview"} & set(properties["EnabledCharts"])
        assert "argocd" not in properties["Charts"]
        assert stack._argocd_config() == ac.ARGOCD_DEFAULTS

    def test_everything_on_emits_every_token(self, everything_on) -> None:
        stack, template = everything_on
        properties = _trigger_properties(template)
        replacements = properties["ImageReplacements"]
        cluster_name = stack.cluster_config.cluster_name
        assert {key: replacements[key] for key in ac.ARGOCD_MANIFEST_TOKENS} == {
            "{{ARGOCD_ENABLED}}": "true",
            "{{ARGOCD_SOURCE_REPOS}}": '["https://github.com/example/*"]',
            "{{ARGOCD_GITOPS_REPO_URL}}": "https://github.com/example/tenants.git",
            "{{ARGOCD_GITOPS_REVISION}}": "main",
            "{{ARGOCD_GITOPS_PATH}}": f"clusters/{cluster_name}",
            "{{ARGOCD_GITOPS_SYNC_POLICY}}": '{"automated": {"selfHeal": true, "prune": false}}',
        }
        assert replacements["{{CROSSPLANE_ENABLED}}"] == "true"
        assert {"argocd", "crossplane", "crossview"} <= set(properties["EnabledCharts"])
        # The repo-server autoscaler reaches the installer as chart values.
        assert properties["Charts"]["argocd"] == {
            "values": ac.argocd_chart_values(ac.validate_argocd_config(_ARGOCD_ON))
        }
        assert (
            properties["Charts"]["argocd"]["values"]["repoServer"]["autoscaling"]["maxReplicas"]
            == 4
        )

    def test_config_doubles_without_the_getter_read_as_off(self) -> None:
        stack = RS.__new__(RS)
        stack.config = MagicMock(spec=[])
        assert stack._argocd_config() == ac.ARGOCD_DEFAULTS
        stack.config = MagicMock()
        assert stack._argocd_config() == ac.ARGOCD_DEFAULTS


# ─── Helm installer ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def helm_handler() -> Any:
    return load_lambda_module("helm-installer")


class TestHelmInstallerCleanup:
    def test_argocd_deletes_its_applications_before_the_controller_goes(
        self, helm_handler: Any
    ) -> None:
        assert helm_handler.CHART_CUSTOM_RESOURCE_API_GROUPS["argocd"] == ("argoproj.io",)

    def test_crossplane_deletes_usages_first_and_packages_last(self, helm_handler: Any) -> None:
        assert helm_handler.CHART_CUSTOM_RESOURCE_API_GROUPS["crossplane"] == (
            "protection.crossplane.io",
            "ops.crossplane.io",
            "apiextensions.crossplane.io",
            "pkg.crossplane.io",
        )

    def test_crossview_owns_no_custom_resources(self, helm_handler: Any) -> None:
        assert "crossview" not in helm_handler.CHART_CUSTOM_RESOURCE_API_GROUPS

    def test_crossplane_cleanup_discovers_every_group_in_order(
        self, helm_handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: Any) -> Any:
            commands.append(command)
            stdout = ""
            if "api-resources" in command:
                group = next(arg for arg in command if arg.startswith("--api-group="))
                namespaced = "--namespaced=true" in command
                stdout = {
                    (
                        "--api-group=protection.crossplane.io",
                        True,
                    ): "usages.protection.crossplane.io",
                    (
                        "--api-group=apiextensions.crossplane.io",
                        False,
                    ): "compositeresourcedefinitions.apiextensions.crossplane.io\n"
                    "compositions.apiextensions.crossplane.io",
                    ("--api-group=pkg.crossplane.io", False): "functions.pkg.crossplane.io",
                }.get((group, namespaced), "")
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(helm_handler.subprocess, "run", fake_run)
        ok, message = helm_handler._delete_chart_custom_resources("crossplane", "/tmp/kc")
        assert ok, message
        discovered_groups = [
            next(arg for arg in command if arg.startswith("--api-group="))
            for command in commands
            if "api-resources" in command
        ]
        assert discovered_groups == [
            f"--api-group={group}"
            for group in helm_handler.CROSSPLANE_API_GROUPS
            for _scope in (True, False)
        ]
        deletes = [command for command in commands if "delete" in command]
        assert deletes[0][deletes[0].index("delete") + 1] == "usages.protection.crossplane.io"
        assert "--all-namespaces" in deletes[0]
        # The definitions are gone before the packages get a pass of their own.
        assert [command[command.index("delete") + 1] for command in deletes[1:]] == [
            "compositeresourcedefinitions.apiextensions.crossplane.io,"
            "compositions.apiextensions.crossplane.io",
            "functions.pkg.crossplane.io",
        ]
        assert all("--all-namespaces" not in command for command in deletes[1:])
        assert message == "Deleted and waited for 4 crossplane custom resource type(s)"

    def test_projects_and_packages_wait_for_a_pass_of_their_own(self, helm_handler: Any) -> None:
        deferred = helm_handler.CHART_CUSTOM_RESOURCES_DELETED_LAST
        assert deferred == {
            "argocd": frozenset({"appprojects.argoproj.io"}),
            "crossplane": frozenset({"pkg.crossplane.io"}),
        }
        for chart, entries in deferred.items():
            groups = helm_handler.CHART_CUSTOM_RESOURCE_API_GROUPS[chart]
            # Each entry is one of the chart's groups or a type inside one.
            assert all(entry in groups or entry.partition(".")[2] in groups for entry in entries)

    @staticmethod
    def _argocd_run(commands: list[list[str]], failing_delete: str | None = None) -> Any:
        """A ``subprocess.run`` double serving the argo-cd chart's three kinds."""

        def fake_run(command: list[str], **_kwargs: Any) -> Any:
            commands.append(command)
            if "api-resources" in command:
                stdout = ""
                if "--namespaced=true" in command:
                    stdout = (
                        "applications.argoproj.io\n"
                        "applicationsets.argoproj.io\n"
                        "appprojects.argoproj.io\n"
                    )
                return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
            if "delete" in command and command[command.index("delete") + 1] == failing_delete:
                return SimpleNamespace(returncode=1, stdout="", stderr="webhook unavailable")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return fake_run

    def test_argocd_projects_go_after_the_applications_they_finalize(
        self, helm_handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The controller finalizes an Application only while its project exists."""
        commands: list[list[str]] = []
        monkeypatch.setattr(helm_handler.subprocess, "run", self._argocd_run(commands))
        ok, message = helm_handler._delete_chart_custom_resources("argocd", "/tmp/kc")
        assert ok, message
        deletes = [command for command in commands if "delete" in command]
        assert [command[command.index("delete") + 1] for command in deletes] == [
            "applications.argoproj.io,applicationsets.argoproj.io",
            "appprojects.argoproj.io",
        ]
        assert all("--wait=true" in command for command in deletes)
        assert all("--all-namespaces" in command for command in deletes)
        assert message == "Deleted and waited for 3 argocd custom resource type(s)"

    def test_a_failed_application_pass_keeps_the_projects(
        self, helm_handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands: list[list[str]] = []
        monkeypatch.setattr(
            helm_handler.subprocess,
            "run",
            self._argocd_run(
                commands, failing_delete="applications.argoproj.io,applicationsets.argoproj.io"
            ),
        )
        ok, message = helm_handler._delete_chart_custom_resources("argocd", "/tmp/kc")
        assert not ok
        assert "even after finalizer removal: webhook unavailable" in message
        deleted = [
            command[command.index("delete") + 1] for command in commands if "delete" in command
        ]
        assert "appprojects.argoproj.io" not in deleted


class TestHelmInstallerConvergence:
    def _event(self, chart: str, enabled: bool) -> dict[str, Any]:
        return {
            "Action": "install_chart",
            "Chart": chart,
            "ClusterName": "gco-us-east-1",
            "Region": "us-east-1",
            "EnabledCharts": ["keda", chart] if enabled else ["keda"],
            "Charts": {},
        }

    @pytest.mark.parametrize("chart", ["argocd", "crossplane", "crossview"])
    def test_enabled_chart_installs(self, helm_handler: Any, chart: str) -> None:
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(
                helm_handler,
                "install_chart",
                return_value=(True, f"Successfully installed {chart}"),
            ) as mock_install,
            patch.object(helm_handler.os, "remove"),
        ):
            result = helm_handler.handle_task(self._event(chart, enabled=True))
        assert result["status"] == "installed"
        mock_install.assert_called_once()

    @pytest.mark.parametrize("chart", ["argocd", "crossplane", "crossview"])
    def test_disabled_chart_uninstalls_on_the_same_pass(
        self, helm_handler: Any, chart: str
    ) -> None:
        with (
            patch.object(helm_handler, "configure_kubeconfig", return_value="/tmp/kc"),
            patch.object(
                helm_handler, "uninstall_chart", return_value=(True, "Successfully uninstalled")
            ) as mock_uninstall,
            patch.object(helm_handler, "install_chart") as mock_install,
            patch.object(helm_handler.os, "remove"),
        ):
            result = helm_handler.handle_task(self._event(chart, enabled=False))
        assert result["status"] == "uninstalled"
        mock_uninstall.assert_called_once()
        mock_install.assert_not_called()
