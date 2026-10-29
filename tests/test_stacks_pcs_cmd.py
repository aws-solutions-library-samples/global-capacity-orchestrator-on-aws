"""Tests for ``gco stacks pcs`` and the AWS PCS preflight hook on ``gco stacks deploy``.

Every command runs through ``CliRunner`` against a real ``cdk.json`` in a
temporary working directory, so the block is read, validated and written the
way an operator's checkout is; only AWS is faked (``cli.pcs_login.stack_outputs``
for the regional stack's outputs, ``cli.pcs_preflight.run_preflight`` for the
account checks, ``cli.stacks.get_stack_manager`` for the deploy itself).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli import pcs_preflight as pf
from cli.main import cli
from cli.pcs_login import PcsLoginError

_REGIONS = ["us-east-1", "eu-west-1"]
_CLUSTER_ID = "pcs_0123456789abcdef"
_PROJECT = "acme"


def _write_cdk_json(path: Path, **context: Any) -> Path:
    document = {
        "app": "python3 app.py",
        "context": {
            "project_name": _PROJECT,
            "deployment_regions": {"global": "us-east-1", "regional": list(_REGIONS)},
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
    region: str, *checks: tuple[str, str, str], mode: str = "managed", enabled: bool = True
) -> pf.PreflightReport:
    return pf.PreflightReport(
        region=region,
        mode=mode,
        enabled=enabled,
        cluster=f"{_PROJECT}-pcs" if mode == "managed" else _CLUSTER_ID,
        checks=[pf.Check(*check) for check in checks],
    )


# ---------------------------------------------------------------------------
# gco stacks pcs status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_managed_and_deployed(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True})
        outputs = {
            "PcsClusterId": _CLUSTER_ID,
            "PcsMode": "managed",
            "PcsSlurmVersion": "25.11",
            "ClusterName": "acme-us-east-1",  # not a PCS output: left out
        }
        with patch("cli.pcs_login.stack_outputs", return_value=outputs) as stack_outputs:
            result = _invoke(["stacks", "pcs", "status"])

        assert result.exit_code == 0, result.output
        stack_outputs.assert_called_once_with("us-east-1", "acme-us-east-1")
        assert "AWS PCS in us-east-1: enabled, managed mode" in result.output
        assert "cluster: acme-pcs (Slurm 25.11), login node on" in result.output
        assert "gco-compute" in result.output and "identity: ec2-user" in result.output
        assert (
            f"deployed: ClusterId {_CLUSTER_ID}, Mode managed, SlurmVersion 25.11" in result.output
        )

    @pytest.mark.parametrize("endpoint_cidr", [None, "10.42.0.0/16"])
    def test_existing_mode_when_the_stack_cannot_be_read(
        self, workdir: Path, endpoint_cidr: str | None
    ) -> None:
        _write_cdk_json(
            workdir / "cdk.json",
            pcs={
                "enabled": True,
                "mode": "existing",
                "cluster_id": _CLUSTER_ID,
                "endpoint_cidr": endpoint_cidr,
            },
        )
        with patch(
            "cli.pcs_login.stack_outputs",
            side_effect=PcsLoginError("could not read stack acme-eu-west-1 in eu-west-1: denied"),
        ):
            result = _invoke(["stacks", "pcs", "status", "-r", "eu-west-1"])

        assert result.exit_code == 0, result.output
        assert "AWS PCS in eu-west-1: enabled, existing mode" in result.output
        assert f"cluster: {_CLUSTER_ID}" in result.output
        assert ("endpoint CIDR: 10.42.0.0/16" in result.output) is (endpoint_cidr is not None)
        assert "[eu-west-1] could not read the regional stack: could not read stack" in (
            result.output
        )
        assert "deployed: no (the regional stack publishes no PCS outputs)" in result.output

    def test_all_regions_as_json(self, workdir: Path) -> None:
        _write_cdk_json(
            workdir / "cdk.json",
            pcs={"enabled": False},
            pcs_regions={
                "eu-west-1": {"enabled": True, "mode": "existing", "cluster_id": _CLUSTER_ID}
            },
        )
        by_region = {"us-east-1": {"PcsClusterId": _CLUSTER_ID, "PcsMode": "managed"}}
        with patch(
            "cli.pcs_login.stack_outputs",
            side_effect=lambda region, _stack: by_region.get(region, {}),
        ):
            result = _invoke(["--output", "json", "stacks", "pcs", "status", "--all-regions"])

        assert result.exit_code == 0, result.output
        documents = json.loads(result.stdout)
        assert [d["region"] for d in documents] == _REGIONS
        first, second = documents
        assert (first["enabled"], first["mode"], first["cluster_name"]) == (
            False,
            "managed",
            "acme-pcs",
        )
        assert first["deployed"] == {"ClusterId": _CLUSTER_ID, "Mode": "managed"}
        assert first["login"] is True
        assert [g["name"] for g in first["node_groups"]] == ["gco-compute"]
        assert [q["name"] for q in first["queues"]] == ["gco"]
        assert (second["mode"], second["cluster_id"], second["cluster_name"]) == (
            "existing",
            _CLUSTER_ID,
            None,
        )
        assert second["deployed"] is None

    def test_one_region_as_json_is_a_single_document(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True})
        with patch("cli.pcs_login.stack_outputs", return_value={}):
            result = _invoke(["--output", "json", "stacks", "pcs", "status"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["region"] == "us-east-1"

    def test_an_invalid_block_is_reported(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True, "mode": "existing"})
        with patch("cli.pcs_login.stack_outputs") as stack_outputs:
            result = _invoke(["stacks", "pcs", "status"])

        assert result.exit_code == 1
        assert "[us-east-1] cdk.json pcs block is invalid for us-east-1" in result.output
        assert "pcs.cluster_id is required" in result.output
        stack_outputs.assert_not_called()


# ---------------------------------------------------------------------------
# gco stacks pcs enable / disable
# ---------------------------------------------------------------------------


class TestEnable:
    def test_global_managed(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", pcs={"enabled": False})
        result = _invoke(["stacks", "pcs", "enable", "-y"])

        assert result.exit_code == 0, result.output
        assert _context(path)["pcs"] == {"enabled": True}
        assert "AWS PCS enabled in cdk.json for all regions (global) (managed mode)" in (
            result.output
        )
        assert "Run 'gco stacks pcs preflight' to check the account" in result.output
        assert "'gco stacks deploy-all' to apply" in result.output

    def test_without_a_block_the_defaults_are_written(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json")
        result = _invoke(["stacks", "pcs", "enable", "-y"])

        assert result.exit_code == 0, result.output
        assert _context(path)["pcs"] == {
            "enabled": True,
            "mode": "managed",
            "cluster_id": None,
            "endpoint_cidr": None,
        }

    def test_existing_mode_for_one_region_after_confirmation(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", pcs={"enabled": False})
        result = _invoke(
            [
                "stacks",
                "pcs",
                "enable",
                "--region",
                "eu-west-1",
                "--mode",
                "existing",
                "--cluster-id",
                _CLUSTER_ID,
                "--endpoint-cidr",
                "10.42.0.0/16",
            ],
            input="y\n",
        )

        assert result.exit_code == 0, result.output
        assert "AWS PCS configuration for region eu-west-1:" in result.output
        assert "Mode: existing" in result.output
        assert f"Cluster: {_CLUSTER_ID}" in result.output
        assert "Endpoint CIDR: 10.42.0.0/16" in result.output
        assert "Enable AWS PCS for region eu-west-1?" in result.output
        context = _context(path)
        assert context["pcs"] == {"enabled": False}  # the global block is untouched
        assert context["pcs_regions"] == {
            "eu-west-1": {
                "enabled": True,
                "mode": "existing",
                "cluster_id": _CLUSTER_ID,
                "endpoint_cidr": "10.42.0.0/16",
            }
        }
        assert "'gco stacks deploy acme-eu-west-1' to apply" in result.output

    def test_managed_mode_prompt_and_decline(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", pcs={"enabled": False})
        result = _invoke(["stacks", "pcs", "enable"], input="n\n")

        assert result.exit_code == 1
        assert "The regional stack creates the cluster from pcs.managed" in result.output
        assert _context(path)["pcs"] == {"enabled": False}

    def test_existing_mode_without_a_cluster_id_is_refused(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", pcs={"enabled": False})
        result = _invoke(["stacks", "pcs", "enable", "--mode", "existing", "-y"])

        assert result.exit_code == 1
        assert "Refusing to write an invalid pcs block: pcs.cluster_id is required" in (
            result.output
        )
        assert _context(path)["pcs"] == {"enabled": False}

    def test_a_region_overlay_is_validated_on_top_of_the_global_block(self, workdir: Path) -> None:
        # The global block is existing mode; the overlay only flips enabled and
        # inherits the cluster id, so the merged block is valid.
        path = _write_cdk_json(
            workdir / "cdk.json",
            pcs={"enabled": False, "mode": "existing", "cluster_id": _CLUSTER_ID},
            pcs_regions={"us-east-1": {"enabled": False}},
        )
        result = _invoke(["stacks", "pcs", "enable", "-r", "us-east-1"], input="y\n")

        assert result.exit_code == 0, result.output
        assert f"Cluster: {_CLUSTER_ID}" in result.output
        assert "Endpoint CIDR" not in result.output
        assert "(existing mode)" in result.output
        assert _context(path)["pcs_regions"]["us-east-1"] == {"enabled": True}

    def test_unreadable_cdk_json(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json")
        with patch("cli.stacks.get_pcs_config", side_effect=RuntimeError("cdk.json not found")):
            result = _invoke(["stacks", "pcs", "enable", "-y"])

        assert result.exit_code == 1
        assert "Failed to read cdk.json: cdk.json not found" in result.output

    def test_write_failure(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json")
        with patch("cli.stacks.update_pcs_config", side_effect=OSError("disk full")):
            result = _invoke(["stacks", "pcs", "enable", "-y"])

        assert result.exit_code == 1
        assert "Failed to enable PCS: disk full" in result.output


class TestDisable:
    def test_global(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True, "mode": "managed"})
        result = _invoke(["stacks", "pcs", "disable", "-y"])

        assert result.exit_code == 0, result.output
        assert _context(path)["pcs"] == {"enabled": False, "mode": "managed"}
        assert "AWS PCS disabled in cdk.json for all regions (global)" in result.output
        assert "Run 'gco stacks deploy-all' to apply" in result.output

    def test_one_region_after_confirmation(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True})
        result = _invoke(["stacks", "pcs", "disable", "--region", "us-east-1"], input="y\n")

        assert result.exit_code == 0, result.output
        assert "This will disable AWS PCS for region us-east-1." in result.output
        assert "removal_policy is 'retain'" in result.output
        context = _context(path)
        assert context["pcs"] == {"enabled": True}
        assert context["pcs_regions"] == {"us-east-1": {"enabled": False}}
        assert "Run 'gco stacks deploy acme-us-east-1' to apply" in result.output

    def test_declined(self, workdir: Path) -> None:
        path = _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True})
        result = _invoke(["stacks", "pcs", "disable"], input="n\n")

        assert result.exit_code == 1
        assert _context(path)["pcs"] == {"enabled": True}

    def test_write_failure(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json")
        with patch("cli.stacks.update_pcs_config", side_effect=OSError("read-only")):
            result = _invoke(["stacks", "pcs", "disable", "-y"])

        assert result.exit_code == 1
        assert "Failed to disable PCS: read-only" in result.output


# ---------------------------------------------------------------------------
# gco stacks pcs preflight
# ---------------------------------------------------------------------------


class TestPreflightCommand:
    def test_table_report(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True})
        report = _report("us-east-1", ("pcs-service", pf.PASS, "AWS PCS answers in us-east-1"))
        with patch("cli.pcs_preflight.run_preflight", return_value=report) as run:
            result = _invoke(["stacks", "pcs", "preflight"])

        assert result.exit_code == 0, result.output
        [(args, kwargs)] = run.call_args_list
        assert args[0] == "us-east-1" and args[1]["enabled"] is True
        assert kwargs == {"project": _PROJECT}
        assert "AWS PCS preflight — us-east-1 (managed mode, enabled): acme-pcs" in result.output
        assert "✓ pcs-service" in result.output
        assert "ready: no failures" in result.output

    def test_a_failure_in_one_region_exits_1_with_json(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True})
        reports = {
            "us-east-1": _report("us-east-1", ("pcs-service", pf.PASS, "ok")),
            "eu-west-1": _report("eu-west-1", ("instance-types", pf.FAIL, "p5.48xlarge missing")),
        }
        with patch(
            "cli.pcs_preflight.run_preflight",
            side_effect=lambda region, _config, **_kw: reports[region],
        ):
            result = _invoke(["--output", "json", "stacks", "pcs", "preflight", "-A"])

        assert result.exit_code == 1
        document = json.loads(result.stdout)
        assert document["ok"] is False
        assert [(r["region"], r["ok"]) for r in document["regions"]] == [
            ("us-east-1", True),
            ("eu-west-1", False),
        ]
        assert document["regions"][1]["checks"] == [
            {"name": "instance-types", "status": "fail", "detail": "p5.48xlarge missing"}
        ]

    def test_a_disabled_block_is_still_checked(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": False})
        report = _report("eu-west-1", enabled=False)
        with patch("cli.pcs_preflight.run_preflight", return_value=report) as run:
            result = _invoke(["stacks", "pcs", "preflight", "-r", "eu-west-1"])

        assert result.exit_code == 0, result.output
        assert run.call_args.args[1]["enabled"] is False
        assert "(managed mode, disabled in cdk.json)" in result.output
        assert "(nothing to check)" in result.output

    def test_an_invalid_block_is_an_error(self, workdir: Path) -> None:
        _write_cdk_json(
            workdir / "cdk.json",
            pcs={"enabled": True},
            pcs_regions={"eu-west-1": {"mode": "existing", "cluster_id": "nope"}},
        )
        with patch("cli.pcs_preflight.run_preflight") as run:
            result = _invoke(["stacks", "pcs", "preflight", "-r", "eu-west-1"])

        assert result.exit_code == 1
        assert "[eu-west-1] cdk.json pcs block is invalid for eu-west-1" in result.output
        run.assert_not_called()


# ---------------------------------------------------------------------------
# The deploy hook
# ---------------------------------------------------------------------------


def _manager() -> MagicMock:
    manager = MagicMock()
    manager.deploy.return_value = True
    manager.list_stacks.return_value = ["acme-global", "acme-us-east-1", "acme-eu-west-1"]
    manager.deploy_orchestrated.return_value = (True, manager.list_stacks.return_value, [])
    return manager


class TestDeployHook:
    def test_a_passing_preflight_lets_the_deploy_run_and_relays_warnings(
        self, workdir: Path
    ) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True})
        report = _report(
            "us-east-1",
            ("pcs-service", pf.PASS, "AWS PCS answers in us-east-1"),
            ("spot-role", pf.WARN, "could not check: AccessDenied"),
        )
        manager = _manager()
        with (
            patch("cli.pcs_preflight.run_preflight", return_value=report) as run,
            patch("cli.stacks.get_stack_manager", return_value=manager),
        ):
            result = _invoke(["stacks", "deploy", "acme-us-east-1", "-y"])

        assert result.exit_code == 0, result.output
        run.assert_called_once()
        assert run.call_args.args[0] == "us-east-1"
        assert "AWS PCS preflight for us-east-1..." in result.output
        assert "[us-east-1] spot-role: could not check: AccessDenied" in result.output
        assert "AWS PCS preflight passed for us-east-1" in result.output
        manager.deploy.assert_called_once()
        assert manager.deploy.call_args.kwargs["stack_name"] == "acme-us-east-1"

    def test_a_failing_preflight_stops_the_deploy(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True})
        report = _report(
            "us-east-1", ("instance-types", pf.FAIL, "p5.48xlarge (gpu) is not available")
        )
        with (
            patch("cli.pcs_preflight.run_preflight", return_value=report),
            patch("cli.stacks.get_stack_manager") as get_manager,
        ):
            result = _invoke(["stacks", "deploy", "acme-us-east-1", "-y"])

        assert result.exit_code == 1
        assert "✗ instance-types" in result.output
        assert "p5.48xlarge (gpu) is not available" in result.output
        assert "AWS PCS preflight failed; fix the findings above" in result.output
        assert "--skip-pcs-preflight" in result.output
        get_manager.assert_not_called()

    def test_skip_flag(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True})
        manager = _manager()
        with (
            patch("cli.pcs_preflight.run_preflight") as run,
            patch("cli.stacks.get_stack_manager", return_value=manager),
        ):
            result = _invoke(["stacks", "deploy", "acme-us-east-1", "-y", "--skip-pcs-preflight"])

        assert result.exit_code == 0, result.output
        run.assert_not_called()
        manager.deploy.assert_called_once()

    @pytest.mark.parametrize(
        "stack_name",
        ["acme-global", "acme-api-gateway", "acme-ap-south-1", "other-us-east-1"],
    )
    def test_stacks_without_a_pcs_cluster_are_not_preflighted(
        self, workdir: Path, stack_name: str
    ) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True})
        manager = _manager()
        with (
            patch("cli.pcs_preflight.run_preflight") as run,
            patch("cli.stacks.get_stack_manager", return_value=manager),
        ):
            result = _invoke(["stacks", "deploy", stack_name, "-y"])

        assert result.exit_code == 0, result.output
        run.assert_not_called()
        assert "AWS PCS preflight" not in result.output

    def test_a_disabled_block_is_not_preflighted_unless_enabled_for_the_run(
        self, workdir: Path
    ) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": False})
        manager = _manager()
        with (
            patch("cli.pcs_preflight.run_preflight") as run,
            patch("cli.stacks.get_stack_manager", return_value=manager),
        ):
            plain = _invoke(["stacks", "deploy", "acme-us-east-1", "-y"])
            assert plain.exit_code == 0, plain.output
            run.assert_not_called()

            run.return_value = _report("us-east-1", ("pcs-service", pf.PASS, "ok"))
            forced = _invoke(["stacks", "deploy", "acme-us-east-1", "-y", "--enable", "pcs,slurm"])

        assert forced.exit_code == 0, forced.output
        run.assert_called_once()
        assert run.call_args.args[1]["enabled"] is True  # the --enable pcs override applied
        assert "AWS PCS preflight passed for us-east-1" in forced.output
        assert manager.deploy.call_count == 2

    def test_deploy_all_checks_every_region_with_pcs_on(self, workdir: Path) -> None:
        # Global off, one Region on through its overlay, the other Region's
        # overlay invalid: only the valid enabled Region is preflighted (the
        # synth reports the invalid block with its full message).
        _write_cdk_json(
            workdir / "cdk.json",
            pcs={"enabled": False},
            pcs_regions={
                "eu-west-1": {"enabled": True},
                "us-east-1": {"enabled": True, "mode": "existing"},
            },
        )
        manager = _manager()
        report = _report("eu-west-1", ("pcs-service", pf.PASS, "ok"))
        with (
            patch("cli.pcs_preflight.run_preflight", return_value=report) as run,
            patch("cli.stacks.get_stack_manager", return_value=manager),
        ):
            result = _invoke(["stacks", "deploy-all", "-y"])

        assert result.exit_code == 0, result.output
        assert [call.args[0] for call in run.call_args_list] == ["eu-west-1"]
        assert "AWS PCS preflight passed for eu-west-1" in result.output
        manager.deploy_orchestrated.assert_called_once()

    def test_deploy_all_skip_flag(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True})
        manager = _manager()
        with (
            patch("cli.pcs_preflight.run_preflight") as run,
            patch("cli.stacks.get_stack_manager", return_value=manager),
        ):
            result = _invoke(["stacks", "deploy-all", "-y", "--skip-pcs-preflight"])

        assert result.exit_code == 0, result.output
        run.assert_not_called()
        manager.deploy_orchestrated.assert_called_once()

    def test_deploy_all_stops_on_a_failure(self, workdir: Path) -> None:
        _write_cdk_json(workdir / "cdk.json", pcs={"enabled": True})
        reports = {
            "us-east-1": _report("us-east-1", ("pcs-service", pf.PASS, "ok")),
            "eu-west-1": _report("eu-west-1", ("spot-role", pf.FAIL, "missing")),
        }
        with (
            patch(
                "cli.pcs_preflight.run_preflight",
                side_effect=lambda region, _config, **_kw: reports[region],
            ),
            patch("cli.stacks.get_stack_manager") as get_manager,
        ):
            result = _invoke(["stacks", "deploy-all", "-y"])

        assert result.exit_code == 1
        assert "AWS PCS preflight for us-east-1, eu-west-1..." in result.output
        assert "AWS PCS preflight — eu-west-1" in result.output
        assert "AWS PCS preflight — us-east-1" not in result.output  # only failures are shown
        get_manager.assert_not_called()


class TestHelp:
    def test_group_and_flags(self) -> None:
        group = _invoke(["stacks", "pcs", "--help"])
        assert group.exit_code == 0
        for command in ("status", "enable", "disable", "preflight"):
            assert command in group.output
        for command in ("deploy", "deploy-all"):
            result = _invoke(["stacks", command, "--help"])
            assert result.exit_code == 0
            assert "--skip-pcs-preflight" in result.output
