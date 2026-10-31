"""Tests for ``gco stacks hyperpod`` and the HyperPod preflight hook on ``gco stacks deploy``.

Every command runs through ``CliRunner`` against a real ``cdk.json`` in a
temporary working directory, so the block is read, validated and written the
way an operator's checkout is; only AWS is faked
(``cli.hyperpod_status.deployed_cluster`` for the deployed cluster,
``cli.hyperpod_preflight.run_preflight`` for the account checks,
``cli.stacks.get_stack_manager`` for the deploy itself).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli import hyperpod_preflight as pf
from cli import hyperpod_status as hs
from cli.main import cli

_REGIONS = ["us-east-1", "eu-west-1"]
_PROJECT = "acme"
_CLUSTER = f"{_PROJECT}-hyperpod"
_ARN = "arn:aws:sagemaker:us-east-1:123456789012:cluster/abc123"
_PLAN_ARN = "arn:aws:sagemaker:us-east-1:123456789012:training-plan/p5-plan"


_K8S = "1.36"


def _write_cdk_json(path: Path, **context: Any) -> Path:
    document = {
        "app": "python3 app.py",
        "context": {
            "project_name": _PROJECT,
            "deployment_regions": {"global": "us-east-1", "regional": list(_REGIONS)},
            "kubernetes_version": _K8S,
            **context,
        },
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _context(path: Path) -> dict[str, Any]:
    context: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["context"]
    return context


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout-shaped working directory the CLI reads ``cdk.json`` from."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _invoke(args: list[str], *, input: str | None = None) -> Any:
    return CliRunner().invoke(cli, args, input=input)


def _report(
    region: str, *checks: tuple[str, str, str], enabled: bool = True
) -> pf.HyperpodPreflightReport:
    return pf.HyperpodPreflightReport(
        region=region,
        enabled=enabled,
        cluster=_CLUSTER,
        checks=[pf.Check(*check) for check in checks],
    )


def _deployed(**overrides: Any) -> hs.DeployedCluster:
    fields: dict[str, Any] = {
        "name": _CLUSTER,
        "arn": _ARN,
        "status": "InService",
        "failure_message": None,
        "instance_groups": [
            hs.InstanceGroupStatus(
                name="accelerated",
                instance_type="ml.g5.8xlarge",
                current=1,
                target=2,
                status="Updating",
            )
        ],
        "outputs": {"ClusterName": _CLUSTER, "ClusterArn": _ARN},
    }
    fields.update(overrides)
    return hs.DeployedCluster(**fields)


# ---------------------------------------------------------------------------
# gco stacks hyperpod status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_configured_and_deployed(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        with patch("cli.hyperpod_status.deployed_cluster", return_value=_deployed()) as deployed:
            result = _invoke(["stacks", "hyperpod", "status"])
        assert result.exit_code == 0, result.output
        deployed.assert_called_once_with("us-east-1", "acme-us-east-1")
        assert "SageMaker HyperPod in us-east-1: enabled" in result.output
        assert (
            "cluster: acme-hyperpod, node recovery Automatic, zone first EKS-supported subnet, "
            "removal policy destroy" in result.output
        )
        assert "accelerated" in result.output and "ml.g5.8xlarge" in result.output
        assert "queue quotas: cpu 1000, memory 4Ti, gpu 64" in result.output
        assert f"deployed: {_CLUSTER} InService ({_ARN})" in result.output
        assert "Updating" in result.output

    def test_not_deployed_and_a_failure_message(self, workdir: Path) -> None:
        _write_cdk_json(
            workdir / "cdk.json",
            hyperpod={"enabled": False, "availability_zone": "eu-west-1b", "cluster_name": "hp"},
        )
        with patch("cli.hyperpod_status.deployed_cluster", return_value=None):
            result = _invoke(["stacks", "hyperpod", "status", "-r", "eu-west-1"])
        assert result.exit_code == 0, result.output
        assert "SageMaker HyperPod in eu-west-1: disabled" in result.output
        assert "cluster: hp, node recovery Automatic, zone eu-west-1b" in result.output
        assert "deployed: no (the regional stack publishes no HyperPod cluster)" in result.output

        failed = _deployed(status="Failed", failure_message="insufficient capacity")
        with patch("cli.hyperpod_status.deployed_cluster", return_value=failed):
            result = _invoke(["stacks", "hyperpod", "status", "-r", "eu-west-1"])
        assert result.exit_code == 0, result.output
        assert f"deployed: {_CLUSTER} Failed" in result.output
        assert "failure: insufficient capacity" in result.output

    def test_an_unreadable_cluster_is_a_warning(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        with patch(
            "cli.hyperpod_status.deployed_cluster",
            side_effect=hs.HyperpodStatusError("could not read stack acme-us-east-1: denied"),
        ):
            result = _invoke(["stacks", "hyperpod", "status"])
        assert result.exit_code == 0, result.output
        assert (
            "[us-east-1] could not read the deployed cluster: could not read stack" in result.output
        )
        assert "deployed: no" in result.output

    def test_all_regions_as_json(self, workdir: Path) -> None:
        _write_cdk_json(
            workdir / "cdk.json",
            hyperpod={"enabled": False},
            hyperpod_regions={
                "eu-west-1": {
                    "enabled": True,
                    "instance_groups": [
                        {
                            "name": "p5",
                            "instance_type": "ml.p5.48xlarge",
                            "instance_count": 4,
                            "training_plan_arn": _PLAN_ARN,
                        }
                    ],
                }
            },
        )
        deployed = {"us-east-1": None, "eu-west-1": _deployed()}
        with patch(
            "cli.hyperpod_status.deployed_cluster",
            side_effect=lambda region, _stack: deployed[region],
        ):
            result = _invoke(["--output", "json", "stacks", "hyperpod", "status", "--all-regions"])
        assert result.exit_code == 0, result.output
        documents = json.loads(result.output)
        assert [d["region"] for d in documents] == _REGIONS
        assert documents[0]["enabled"] is False
        assert documents[0]["deployed"] is None
        assert documents[0]["instance_groups"][0]["instance_type"] == "ml.g5.8xlarge"
        assert documents[1]["enabled"] is True
        assert documents[1]["instance_groups"] == [
            {
                "name": "p5",
                "instance_type": "ml.p5.48xlarge",
                "instance_count": 4,
                "threads_per_core": 1,
                "training_plan_arn": _PLAN_ARN,
                "root_volume_size_gb": 100,
            }
        ]
        assert documents[1]["deployed"]["status"] == "InService"
        assert documents[1]["deployed"]["instance_groups"][0]["target"] == 2
        assert documents[1]["deployed"]["outputs"]["ClusterArn"] == _ARN

    def test_one_region_as_json_is_a_single_document(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        with patch("cli.hyperpod_status.deployed_cluster", return_value=None):
            result = _invoke(["--output", "json", "stacks", "hyperpod", "status"])
        assert result.exit_code == 0, result.output
        document = json.loads(result.output)
        assert document["region"] == "us-east-1"
        assert document["cluster_name"] == _CLUSTER

    def test_an_invalid_block_is_reported(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True, "node_recovery": "Maybe"})
        with patch("cli.hyperpod_status.deployed_cluster") as deployed:
            result = _invoke(["stacks", "hyperpod", "status"])
        assert result.exit_code == 1
        assert "[us-east-1] cdk.json hyperpod block is invalid" in result.output
        assert "node_recovery" in result.output
        deployed.assert_not_called()


# ---------------------------------------------------------------------------
# gco stacks hyperpod enable / disable
# ---------------------------------------------------------------------------


class TestEnable:
    def test_global_with_defaults(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": False})
        result = _invoke(["stacks", "hyperpod", "enable", "-y"])
        assert result.exit_code == 0, result.output
        assert _context(path)["hyperpod"] == {"enabled": True}
        assert "SageMaker HyperPod enabled in cdk.json for all regions (global)" in result.output
        assert "'gco stacks deploy-all' to apply" in result.output

    def test_without_a_block_the_defaults_are_written(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json")
        result = _invoke(["stacks", "hyperpod", "enable", "-y", "--cluster-name", "hp"])
        assert result.exit_code == 0, result.output
        assert _context(path)["hyperpod"] == {
            "enabled": True,
            "cluster_name": "hp",
            "availability_zone": None,
        }

    def test_instance_group_options_rewrite_the_first_group(self, workdir: Path) -> None:
        path = _write_cdk_json(
            workdir / "cdk.json",
            hyperpod={
                "enabled": False,
                "instance_groups": [
                    {"name": "gpu", "instance_type": "ml.g5.8xlarge", "threads_per_core": 2},
                    {"name": "cpu", "instance_type": "ml.c5.large"},
                ],
            },
        )
        result = _invoke(
            [
                "stacks",
                "hyperpod",
                "enable",
                "--instance-type",
                "ml.p5.48xlarge",
                "--instance-count",
                "4",
                "--training-plan-arn",
                _PLAN_ARN,
                "--availability-zone",
                "us-east-1b",
            ],
            input="y\n",
        )
        assert result.exit_code == 0, result.output
        assert "Cluster: acme-hyperpod, zone us-east-1b" in result.output
        assert f"Instance group gpu: 4 x ml.p5.48xlarge, training plan {_PLAN_ARN}" in result.output
        block = _context(path)["hyperpod"]
        assert block["enabled"] is True
        assert block["availability_zone"] == "us-east-1b"
        # The first group keeps what the options did not set; the second is dropped.
        assert block["instance_groups"] == [
            {
                "name": "gpu",
                "instance_type": "ml.p5.48xlarge",
                "threads_per_core": 2,
                "instance_count": 4,
                "training_plan_arn": _PLAN_ARN,
            }
        ]

    def test_instance_group_options_without_a_block_name_the_default_group(
        self, workdir: Path
    ) -> None:
        path = _write_cdk_json(workdir / "cdk.json")
        result = _invoke(
            ["stacks", "hyperpod", "enable", "-y", "--instance-type", "ml.g4dn.4xlarge"]
        )
        assert result.exit_code == 0, result.output
        assert _context(path)["hyperpod"]["instance_groups"] == [
            {"name": "accelerated", "instance_type": "ml.g4dn.4xlarge"}
        ]

    def test_one_region_after_confirmation(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": False})
        result = _invoke(
            ["stacks", "hyperpod", "enable", "--region", "eu-west-1", "--instance-count", "0"],
            input="y\n",
        )
        assert result.exit_code == 0, result.output
        assert "SageMaker HyperPod configuration for region eu-west-1:" in result.output
        assert "Instance group accelerated: 0 x ml.g5.8xlarge" in result.output
        assert "'gco stacks deploy acme-eu-west-1' to apply" in result.output
        context = _context(path)
        assert context["hyperpod"] == {"enabled": False}
        assert context["hyperpod_regions"]["eu-west-1"] == {
            "enabled": True,
            "instance_groups": [{"name": "accelerated", "instance_count": 0}],
        }

    def test_declined(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": False})
        result = _invoke(["stacks", "hyperpod", "enable"], input="n\n")
        assert result.exit_code == 1
        assert _context(path)["hyperpod"] == {"enabled": False}

    def test_an_invalid_result_is_refused(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": False})
        result = _invoke(["stacks", "hyperpod", "enable", "-y", "--instance-type", "p5.48xlarge"])
        assert result.exit_code == 1
        assert "Refusing to write an invalid hyperpod block" in result.output
        assert "instance_type" in result.output
        assert _context(path)["hyperpod"] == {"enabled": False}

    def test_a_region_overlay_is_validated_on_top_of_the_global_block(self, workdir: Path) -> None:
        _write_cdk_json(
            workdir / "cdk.json",
            hyperpod={"enabled": False, "node_recovery": "None"},
            hyperpod_regions={"eu-west-1": {"availability_zone": "eu-west-1a"}},
        )
        result = _invoke(
            ["stacks", "hyperpod", "enable", "-r", "eu-west-1", "--availability-zone", "nowhere"]
        )
        assert result.exit_code == 1
        assert "availability_zone" in result.output

    def test_unreadable_cdk_json(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json")
        with patch(
            "cli.stacks.get_hyperpod_config", side_effect=RuntimeError("cdk.json not found")
        ):
            result = _invoke(["stacks", "hyperpod", "enable", "-y"])
        assert result.exit_code == 1
        assert "Failed to read the HyperPod configuration: cdk.json not found" in result.output

    def test_write_failure(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json")
        with patch("cli.stacks.update_hyperpod_config", side_effect=OSError("read-only")):
            result = _invoke(["stacks", "hyperpod", "enable", "-y"])
        assert result.exit_code == 1
        assert "Failed to enable HyperPod: read-only" in result.output


class TestDisable:
    def test_global(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        result = _invoke(["stacks", "hyperpod", "disable", "-y"])
        assert result.exit_code == 0, result.output
        assert _context(path)["hyperpod"] == {"enabled": False}
        assert "SageMaker HyperPod disabled in cdk.json for all regions (global)" in result.output
        assert "Run 'gco stacks deploy-all' to apply" in result.output

    def test_one_region_after_confirmation(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        result = _invoke(["stacks", "hyperpod", "disable", "-r", "us-east-1"], input="y\n")
        assert result.exit_code == 0, result.output
        assert "This will disable SageMaker HyperPod for region us-east-1." in result.output
        assert "work running on them is lost" in result.output
        assert "Run 'gco stacks deploy acme-us-east-1' to apply" in result.output
        context = _context(path)
        assert context["hyperpod"] == {"enabled": True}
        assert context["hyperpod_regions"]["us-east-1"] == {"enabled": False}

    def test_declined(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        result = _invoke(["stacks", "hyperpod", "disable"], input="n\n")
        assert result.exit_code == 1
        assert _context(path)["hyperpod"] == {"enabled": True}

    def test_write_failure(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        with patch("cli.stacks.update_hyperpod_config", side_effect=OSError("disk full")):
            result = _invoke(["stacks", "hyperpod", "disable", "-y"])
        assert result.exit_code == 1
        assert "Failed to disable HyperPod: disk full" in result.output


# ---------------------------------------------------------------------------
# gco stacks hyperpod preflight
# ---------------------------------------------------------------------------


class TestPreflightCommand:
    def test_table_report(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        report = _report(
            "us-east-1",
            ("agent-image", pf.PASS, "health-monitoring agent image published for this Region"),
            ("instance-types", pf.WARN, "could not check: AccessDenied"),
        )
        with patch("cli.hyperpod_preflight.run_preflight", return_value=report) as run:
            result = _invoke(["stacks", "hyperpod", "preflight"])
        assert result.exit_code == 0, result.output
        run.assert_called_once()
        assert run.call_args.args[0] == "us-east-1"
        assert run.call_args.args[1]["enabled"] is True
        # The cluster minor from cdk.json rides along for the kubernetes-version check.
        assert run.call_args.kwargs == {"project": _PROJECT, "kubernetes_version": _K8S}
        assert f"SageMaker HyperPod preflight — us-east-1 (enabled): {_CLUSTER}" in result.output
        assert "✓ agent-image" in result.output
        assert "! instance-types" in result.output
        assert "ready: no failures" in result.output

    def test_a_cdk_json_without_the_cluster_minor_is_an_error(self, workdir: Path) -> None:
        """The kubernetes-version check needs the minor; a cdk.json without it fails like a bad block."""
        path = _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        document = json.loads(path.read_text(encoding="utf-8"))
        del document["context"]["kubernetes_version"]
        path.write_text(json.dumps(document), encoding="utf-8")
        with patch("cli.hyperpod_preflight.run_preflight") as run:
            result = _invoke(["stacks", "hyperpod", "preflight"])
        assert result.exit_code == 1
        run.assert_not_called()
        assert "[us-east-1] cdk.json context.kubernetes_version is missing" in result.output

    def test_a_failure_in_one_region_exits_1_with_json(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        reports = {
            "us-east-1": _report("us-east-1", ("agent-image", pf.PASS, "ok")),
            "eu-west-1": _report("eu-west-1", ("instance-types", pf.FAIL, "quota is 0")),
        }
        with patch(
            "cli.hyperpod_preflight.run_preflight",
            side_effect=lambda region, _config, **_kw: reports[region],
        ):
            result = _invoke(["--output", "json", "stacks", "hyperpod", "preflight", "-A"])
        assert result.exit_code == 1
        document = json.loads(result.output)
        assert document["ok"] is False
        assert [r["region"] for r in document["regions"]] == _REGIONS
        assert document["regions"][1]["checks"][0]["status"] == "fail"

    def test_a_disabled_block_is_still_checked(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": False})
        report = _report("us-east-1", ("agent-image", pf.PASS, "ok"), enabled=False)
        with patch("cli.hyperpod_preflight.run_preflight", return_value=report) as run:
            result = _invoke(["stacks", "hyperpod", "preflight"])
        assert result.exit_code == 0, result.output
        assert run.call_args.args[1]["enabled"] is False
        assert "(disabled in cdk.json)" in result.output

    def test_an_invalid_block_is_an_error(self, workdir: Path) -> None:
        _write_cdk_json(
            workdir / "cdk.json",
            hyperpod={"enabled": True},
            hyperpod_regions={"eu-west-1": {"instance_groups": "many"}},
        )
        report = _report("us-east-1", ("agent-image", pf.PASS, "ok"))
        with patch("cli.hyperpod_preflight.run_preflight", return_value=report) as run:
            result = _invoke(["stacks", "hyperpod", "preflight", "--all-regions"])
        assert result.exit_code == 1
        assert [call.args[0] for call in run.call_args_list] == ["us-east-1"]
        assert "[eu-west-1] cdk.json hyperpod block is invalid for eu-west-1" in result.output
        assert "instance_groups" in result.output


# ---------------------------------------------------------------------------
# The hook on gco stacks deploy / deploy-all
# ---------------------------------------------------------------------------


def _manager() -> MagicMock:
    manager = MagicMock()
    manager.deploy.return_value = (True, "ok")
    manager.list_stacks.return_value = ["acme-global", "acme-us-east-1", "acme-eu-west-1"]
    manager.deploy_orchestrated.return_value = (True, manager.list_stacks.return_value, [])
    return manager


class TestDeployHook:
    def test_a_passing_preflight_lets_the_deploy_run_and_relays_warnings(
        self, workdir: Path
    ) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        report = _report(
            "us-east-1",
            ("agent-image", pf.PASS, "ok"),
            ("cluster-name", pf.WARN, "could not check: AccessDenied"),
        )
        manager = _manager()
        with (
            patch("cli.hyperpod_preflight.run_preflight", return_value=report) as run,
            patch("cli.pcs_preflight.run_preflight") as pcs_run,
            patch("cli.stacks.get_stack_manager", return_value=manager),
        ):
            result = _invoke(["stacks", "deploy", "acme-us-east-1", "-y"])

        assert result.exit_code == 0, result.output
        run.assert_called_once()
        assert run.call_args.args[0] == "us-east-1"
        pcs_run.assert_not_called()  # PCS is off in this cdk.json
        assert "SageMaker HyperPod preflight for us-east-1..." in result.output
        assert "[us-east-1] cluster-name: could not check: AccessDenied" in result.output
        assert "SageMaker HyperPod preflight passed for us-east-1" in result.output
        manager.deploy.assert_called_once()
        assert manager.deploy.call_args.kwargs["stack_name"] == "acme-us-east-1"

    def test_a_failing_preflight_stops_the_deploy(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        report = _report(
            "us-east-1", ("instance-types", pf.FAIL, "ml.p5.48xlarge: no quota in us-east-1")
        )
        with (
            patch("cli.hyperpod_preflight.run_preflight", return_value=report),
            patch("cli.stacks.get_stack_manager") as get_manager,
        ):
            result = _invoke(["stacks", "deploy", "acme-us-east-1", "-y"])

        assert result.exit_code == 1
        assert "✗ instance-types" in result.output
        assert "ml.p5.48xlarge: no quota in us-east-1" in result.output
        assert "SageMaker HyperPod preflight failed; fix the findings above" in result.output
        assert "--skip-hyperpod-preflight" in result.output
        get_manager.assert_not_called()

    def test_skip_flag(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        manager = _manager()
        with (
            patch("cli.hyperpod_preflight.run_preflight") as run,
            patch("cli.stacks.get_stack_manager", return_value=manager),
        ):
            result = _invoke(
                ["stacks", "deploy", "acme-us-east-1", "-y", "--skip-hyperpod-preflight"]
            )

        assert result.exit_code == 0, result.output
        run.assert_not_called()
        manager.deploy.assert_called_once()

    @pytest.mark.parametrize(
        "stack_name",
        ["acme-global", "acme-api-gateway", "acme-ap-south-1", "other-us-east-1"],
    )
    def test_stacks_without_a_hyperpod_cluster_are_not_preflighted(
        self, workdir: Path, stack_name: str
    ) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        manager = _manager()
        with (
            patch("cli.hyperpod_preflight.run_preflight") as run,
            patch("cli.stacks.get_stack_manager", return_value=manager),
        ):
            result = _invoke(["stacks", "deploy", stack_name, "-y"])

        assert result.exit_code == 0, result.output
        run.assert_not_called()
        assert "HyperPod preflight" not in result.output

    def test_a_disabled_block_is_not_preflighted_unless_enabled_for_the_run(
        self, workdir: Path
    ) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": False})
        manager = _manager()
        with (
            patch("cli.hyperpod_preflight.run_preflight") as run,
            patch("cli.stacks.get_stack_manager", return_value=manager),
        ):
            plain = _invoke(["stacks", "deploy", "acme-us-east-1", "-y"])
            assert plain.exit_code == 0, plain.output
            run.assert_not_called()

            run.return_value = _report("us-east-1", ("agent-image", pf.PASS, "ok"))
            forced = _invoke(
                ["stacks", "deploy", "acme-us-east-1", "-y", "--enable", "hyperpod,kueue"]
            )

        assert forced.exit_code == 0, forced.output
        run.assert_called_once()
        assert run.call_args.args[1]["enabled"] is True  # the --enable hyperpod override applied
        assert "SageMaker HyperPod preflight passed for us-east-1" in forced.output
        assert manager.deploy.call_count == 2

    def test_both_preflights_run_when_both_features_are_on(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True}, hyperpod={"enabled": True})
        from cli import pcs_preflight

        pcs_report = pcs_preflight.PreflightReport(
            region="us-east-1",
            mode="managed",
            enabled=True,
            cluster="acme-pcs",
            checks=[pcs_preflight.Check("pcs-service", pcs_preflight.PASS, "ok")],
        )
        manager = _manager()
        with (
            patch("cli.pcs_preflight.run_preflight", return_value=pcs_report) as pcs_run,
            patch(
                "cli.hyperpod_preflight.run_preflight",
                return_value=_report("us-east-1", ("agent-image", pf.PASS, "ok")),
            ) as hp_run,
            patch("cli.stacks.get_stack_manager", return_value=manager),
        ):
            result = _invoke(["stacks", "deploy", "acme-us-east-1", "-y"])

        assert result.exit_code == 0, result.output
        pcs_run.assert_called_once()
        hp_run.assert_called_once()
        assert result.output.index("AWS PCS preflight passed") < result.output.index(
            "SageMaker HyperPod preflight passed"
        )
        manager.deploy.assert_called_once()

    def test_deploy_all_checks_every_region_with_hyperpod_on(self, workdir: Path) -> None:
        # Global off, one Region on through its overlay, the other Region's
        # overlay invalid: only the valid enabled Region is preflighted (the
        # synth reports the invalid block with its full message).
        _write_cdk_json(
            workdir / "cdk.json",
            hyperpod={"enabled": False},
            hyperpod_regions={
                "eu-west-1": {"enabled": True},
                "us-east-1": {"enabled": True, "node_recovery": "Sometimes"},
            },
        )
        manager = _manager()
        report = _report("eu-west-1", ("agent-image", pf.PASS, "ok"))
        with (
            patch("cli.hyperpod_preflight.run_preflight", return_value=report) as run,
            patch("cli.stacks.get_stack_manager", return_value=manager),
        ):
            result = _invoke(["stacks", "deploy-all", "-y"])

        assert result.exit_code == 0, result.output
        assert [call.args[0] for call in run.call_args_list] == ["eu-west-1"]
        assert "SageMaker HyperPod preflight passed for eu-west-1" in result.output
        manager.deploy_orchestrated.assert_called_once()

    def test_deploy_all_skip_flag(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        manager = _manager()
        with (
            patch("cli.hyperpod_preflight.run_preflight") as run,
            patch("cli.stacks.get_stack_manager", return_value=manager),
        ):
            result = _invoke(["stacks", "deploy-all", "-y", "--skip-hyperpod-preflight"])

        assert result.exit_code == 0, result.output
        run.assert_not_called()
        manager.deploy_orchestrated.assert_called_once()

    def test_deploy_all_stops_on_a_failure(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", hyperpod={"enabled": True})
        reports = {
            "us-east-1": _report("us-east-1", ("agent-image", pf.PASS, "ok")),
            "eu-west-1": _report("eu-west-1", ("agent-image", pf.FAIL, "no agent image")),
        }
        with (
            patch(
                "cli.hyperpod_preflight.run_preflight",
                side_effect=lambda region, _config, **_kw: reports[region],
            ),
            patch("cli.stacks.get_stack_manager") as get_manager,
        ):
            result = _invoke(["stacks", "deploy-all", "-y"])

        assert result.exit_code == 1
        assert "SageMaker HyperPod preflight for us-east-1, eu-west-1..." in result.output
        assert "SageMaker HyperPod preflight — eu-west-1" in result.output
        assert "SageMaker HyperPod preflight — us-east-1" not in result.output  # failures only
        get_manager.assert_not_called()


class TestHelp:
    def test_group_and_flags(self) -> None:
        group = _invoke(["stacks", "hyperpod", "--help"])
        assert group.exit_code == 0
        for command in ("status", "enable", "disable", "preflight"):
            assert command in group.output
        for command in ("deploy", "deploy-all"):
            result = _invoke(["stacks", command, "--help"])
            assert result.exit_code == 0
            assert "--skip-hyperpod-preflight" in result.output
            assert "--skip-pcs-preflight" in result.output
