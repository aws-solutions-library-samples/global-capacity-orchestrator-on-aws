"""The operator surface of EKS Capabilities: ``gco stacks capabilities``.

* ``cli/eks_capabilities.py`` — the configured-vs-live merge behind
  ``gco stacks capabilities status`` (cdk.json intent, ``ListCapabilities`` /
  ``DescribeCapability`` results, drift sentences, unmanaged capabilities, the
  missing-cluster case) and the ``eks_capabilities_status`` MCP tool.
* The Click ``status`` command through ``CliRunner`` in table and JSON modes,
  with the AWS layer patched.

The CLI must import without ``aws_cdk``: the config module the command reads
lives at ``gco/eks_capabilities_config.py`` for that reason, pinned here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli import eks_capabilities
from cli.main import cli
from gco import eks_capabilities_config as caps

_REGION = "us-east-1"
_PROJECT = "gco"
_CLUSTER = "gco-us-east-1"
_ACCOUNT = "123456789012"


def _kro_config(*, regions: list[str] | None = None) -> dict[str, Any]:
    return caps.normalize_eks_capabilities_config(
        {"kro": {"enabled": True, "regions": regions or []}}
    )


def _live(
    type_name: str,
    *,
    name: str | None = None,
    status: str = "ACTIVE",
    issues: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "capabilityName": name or f"{_PROJECT}-{type_name}",
        "arn": f"arn:aws:eks:{_REGION}:{_ACCOUNT}:capability/{_CLUSTER}/{name or type_name}",
        "clusterName": _CLUSTER,
        "type": caps.CAPABILITY_TYPE_API_NAMES.get(type_name, type_name),
        "roleArn": f"arn:aws:iam::{_ACCOUNT}:role/{type_name}-capability",
        "status": status,
        "version": "3.1.0",
    }
    if issues:
        detail["health"] = {"issues": issues}
    return detail


def _client_error(code: str, operation: str = "DescribeCapability") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class _FakeEks:
    """Duck-typed EKS client: paginated ListCapabilities + DescribeCapability."""

    def __init__(self, pages: list[list[dict[str, Any]]], details: dict[str, dict[str, Any]]):
        self.pages = pages
        self.details = details
        self.described: list[str] = []
        self.list_error: Exception | None = None

    def get_paginator(self, operation: str) -> Any:
        assert operation == "list_capabilities"
        fake = self

        class _Paginator:
            def paginate(self, **kwargs: Any) -> Any:
                assert kwargs == {"clusterName": _CLUSTER}
                if fake.list_error is not None:
                    raise fake.list_error
                for page in fake.pages:
                    yield {"capabilities": page}

        return _Paginator()

    def describe_capability(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["clusterName"] == _CLUSTER
        self.described.append(kwargs["capabilityName"])
        detail = self.details.get(kwargs["capabilityName"])
        if isinstance(detail, Exception):
            raise detail
        return {"capability": detail} if detail is not None else {}


# ─── cli/eks_capabilities.py ─────────────────────────────────────────────────


class TestLoadConfig:
    def _write(self, tmp_path: Path, context: dict[str, Any]) -> Path:
        path = tmp_path / "cdk.json"
        path.write_text(json.dumps({"context": context}), encoding="utf-8")
        return path

    def test_absent_block_reads_as_all_off(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"project_name": _PROJECT})
        assert eks_capabilities.load_eks_capabilities_config(path) == caps.EKS_CAPABILITIES_DEFAULTS

    def test_block_is_validated_against_the_regional_deployment_regions(
        self, tmp_path: Path
    ) -> None:
        path = self._write(
            tmp_path,
            {
                "deployment_regions": {"regional": [_REGION]},
                "eks_capabilities": {"kro": {"enabled": True, "regions": ["eu-west-1"]}},
            },
        )
        with pytest.raises(caps.EksCapabilitiesConfigError, match="eu-west-1"):
            eks_capabilities.load_eks_capabilities_config(path)

    def test_missing_cdk_json_is_a_runtime_error(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match=r"cdk\.json not found"):
            eks_capabilities.load_eks_capabilities_config(tmp_path / "cdk.json")

    def test_defaults_to_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write(tmp_path, {"eks_capabilities": {"kro": {"enabled": True}}})
        monkeypatch.chdir(tmp_path)
        assert eks_capabilities.load_eks_capabilities_config()["kro"]["enabled"] is True

    def test_shipped_cdk_json_loads(self) -> None:
        root = Path(__file__).resolve().parent.parent
        assert (
            eks_capabilities.load_eks_capabilities_config(root / "cdk.json")
            == caps.EKS_CAPABILITIES_DEFAULTS
        )

    def test_run_scoped_overrides_merge_like_the_cdk_app(self, tmp_path: Path) -> None:
        """A harness that deployed with eks_capabilities_overrides reads the same block back."""
        path = self._write(
            tmp_path,
            {
                "deployment_regions": {"regional": [_REGION]},
                "eks_capabilities": {"ack": {"disabled_services": ["ec2"]}},
            },
        )
        overrides = json.dumps({"kro": {"enabled": True}})
        config = eks_capabilities.load_eks_capabilities_config(path, overrides=overrides)
        assert config["kro"]["enabled"] is True
        assert config["ack"]["disabled_services"] == ["ec2"]
        with pytest.raises(caps.EksCapabilitiesConfigError, match="must be a JSON object"):
            eks_capabilities.load_eks_capabilities_config(path, overrides="nope")


class TestDescribeLiveCapabilities:
    def test_describes_every_summary_across_pages(self) -> None:
        ack = _live("ack")
        kro = _live("kro")
        client = _FakeEks(
            pages=[
                [{"capabilityName": ack["capabilityName"]}],
                [{"capabilityName": kro["capabilityName"]}],
            ],
            details={ack["capabilityName"]: ack, kro["capabilityName"]: kro},
        )
        assert eks_capabilities.describe_live_capabilities(client, _CLUSTER) == [ack, kro]
        assert client.described == [ack["capabilityName"], kro["capabilityName"]]

    def test_falls_back_to_the_summary_when_describe_returns_nothing(self) -> None:
        summary = {"capabilityName": "gco-kro", "type": "KRO", "status": "CREATING"}
        client = _FakeEks(pages=[[summary, {"type": "ACK"}]], details={})
        assert eks_capabilities.describe_live_capabilities(client, _CLUSTER) == [summary]

    def test_no_capabilities(self) -> None:
        assert (
            eks_capabilities.describe_live_capabilities(_FakeEks(pages=[[]], details={}), _CLUSTER)
            == []
        )


class TestBuildStatus:
    def _status(
        self, config: dict[str, Any], live: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        return eks_capabilities.build_status(
            region=_REGION, project_name=_PROJECT, config=config, live=live, **kwargs
        )

    def test_all_off_and_nothing_attached_is_healthy(self) -> None:
        status = self._status(caps.EKS_CAPABILITIES_DEFAULTS, [])
        assert status["region"] == _REGION
        assert status["cluster_name"] == _CLUSTER
        assert status["cluster_found"] is True
        assert status["healthy"] is True
        assert status["unmanaged"] == []
        assert [row["type"] for row in status["capabilities"]] == ["ack", "kro"]
        for row in status["capabilities"]:
            assert row["capability_name"] == f"{_PROJECT}-{row['type']}"
            assert (row["configured"], row["deployed"], row["status"], row["drift"]) == (
                False,
                False,
                None,
                None,
            )
        # Argo CD is not a capability any more: the rows carry no Argo CD fields.
        assert all("argocd_server_url" not in row for row in status["capabilities"])

    def test_configured_but_not_attached_is_drift_naming_the_deploy(self) -> None:
        status = self._status(_kro_config(), [])
        kro = status["capabilities"][1]
        assert kro["configured"] is True and kro["deployed"] is False
        assert kro["drift"] == (
            f"configured in cdk.json but not attached; run 'gco stacks deploy {_CLUSTER} -y'"
        )
        assert status["healthy"] is False

    def test_attached_but_disabled_is_drift_naming_the_removal(self) -> None:
        status = self._status(caps.EKS_CAPABILITIES_DEFAULTS, [_live("kro")])
        kro = status["capabilities"][1]
        assert kro["configured"] is False and kro["deployed"] is True
        assert "attached but disabled in cdk.json" in kro["drift"]
        assert "RETAIN" in kro["drift"]
        assert status["healthy"] is False

    def test_non_active_status_is_drift_with_health_issues(self) -> None:
        live = _live(
            "kro",
            status="DEGRADED",
            issues=[
                {"code": "AccessDenied", "message": "role cannot assume"},
                {"code": "ClusterUnreachable"},
            ],
        )
        status = self._status(_kro_config(), [live])
        kro = status["capabilities"][1]
        assert kro["health_issues"] == ["AccessDenied: role cannot assume", "ClusterUnreachable"]
        assert kro["drift"] == (
            "status is DEGRADED, expected ACTIVE (AccessDenied: role cannot assume; ClusterUnreachable)"
        )
        assert status["healthy"] is False

    def test_region_subset_excludes_this_region(self) -> None:
        status = self._status(_kro_config(regions=["us-west-2"]), [])
        assert status["capabilities"][1]["configured"] is False
        assert status["healthy"] is True

    def test_unmanaged_capabilities_are_listed_separately(self) -> None:
        foreign = _live("kro", name="team-kro")
        status = self._status(
            caps.EKS_CAPABILITIES_DEFAULTS, [foreign, {"capabilityName": "odd", "type": "KRO"}]
        )
        assert status["capabilities"][0]["deployed"] is False
        assert status["unmanaged"] == [
            {"capability_name": "odd", "type": "kro", "status": None, "arn": None},
            {
                "capability_name": "team-kro",
                "type": "kro",
                "status": "ACTIVE",
                "arn": foreign["arn"],
            },
        ]
        # A stranger's capability is visible but not drift.
        assert status["healthy"] is True

    def test_missing_cluster_is_unhealthy_but_still_reports_intent(self) -> None:
        status = self._status(_kro_config(), [], cluster_found=False)
        assert status["cluster_found"] is False
        assert status["healthy"] is False
        assert status["capabilities"][1]["configured"] is True

    def test_loosely_shaped_live_details_are_tolerated(self) -> None:
        """Real DescribeCapability bodies: datetimes, half-formed issues, nameless entries."""
        from datetime import UTC, datetime

        modified = datetime(2026, 9, 24, 3, 4, 5, tzinfo=UTC)
        kro = _live("kro", status="DEGRADED")
        kro["modifiedAt"] = modified
        # One issue is not an object at all; it is skipped, the others render.
        kro["health"] = {"issues": ["garbage", {"code": "Throttled"}]}
        nameless = {"type": "KRO", "status": "ACTIVE"}  # no capabilityName: never a match
        status = self._status(_kro_config(), [kro, nameless])
        row = status["capabilities"][1]
        assert row["modified_at"] == modified.isoformat()
        assert row["health_issues"] == ["Throttled"]
        assert row["drift"] == "status is DEGRADED, expected ACTIVE (Throttled)"
        assert status["unmanaged"] == []


def test_load_config_tolerates_a_cdk_json_without_a_context_object(tmp_path: Path) -> None:
    """A cdk.json whose ``context`` is missing or not an object reads as all-off."""
    path = tmp_path / "cdk.json"
    path.write_text(json.dumps({"app": "python3 app.py", "context": []}), encoding="utf-8")
    assert eks_capabilities.load_eks_capabilities_config(path) == caps.EKS_CAPABILITIES_DEFAULTS


class TestCapabilitiesStatus:
    def test_merges_config_with_the_described_cluster(self) -> None:
        kro = _live("kro")
        client = _FakeEks(
            pages=[[{"capabilityName": kro["capabilityName"]}]],
            details={kro["capabilityName"]: kro},
        )
        status = eks_capabilities.capabilities_status(
            _REGION, _PROJECT, config=_kro_config(), eks_client=client
        )
        assert status["healthy"] is True
        assert status["capabilities"][1]["version"] == "3.1.0"

    def test_missing_cluster_reads_as_not_found(self) -> None:
        client = _FakeEks(pages=[], details={})
        client.list_error = _client_error("ResourceNotFoundException", "ListCapabilities")
        status = eks_capabilities.capabilities_status(
            _REGION, _PROJECT, config=caps.EKS_CAPABILITIES_DEFAULTS, eks_client=client
        )
        assert status["cluster_found"] is False
        assert all(row["deployed"] is False for row in status["capabilities"])

    def test_other_aws_errors_propagate(self) -> None:
        client = _FakeEks(pages=[], details={})
        client.list_error = _client_error("AccessDeniedException", "ListCapabilities")
        with pytest.raises(ClientError):
            eks_capabilities.capabilities_status(
                _REGION, _PROJECT, config=caps.EKS_CAPABILITIES_DEFAULTS, eks_client=client
            )

    def test_builds_a_boto3_client_and_loads_cdk_json_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "cdk.json").write_text(json.dumps({"context": {}}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        client = _FakeEks(pages=[[]], details={})
        with patch("boto3.client", return_value=client) as boto_client:
            status = eks_capabilities.capabilities_status(_REGION, _PROJECT)
        boto_client.assert_called_once_with("eks", region_name=_REGION)
        assert status["healthy"] is True


# ─── Click commands ──────────────────────────────────────────────────────────


def _healthy_status(**overrides: Any) -> dict[str, Any]:
    status = eks_capabilities.build_status(
        region=_REGION,
        project_name=_PROJECT,
        config=_kro_config(),
        live=[_live("kro")],
    )
    status.update(overrides)
    return status


class TestStatusCommand:
    def _invoke(
        self, args: list[str], statuses: list[Any], *, config_error: Exception | None = None
    ) -> Any:
        loaded = MagicMock(return_value=caps.EKS_CAPABILITIES_DEFAULTS)
        if config_error is not None:
            loaded.side_effect = config_error
        with (
            patch("cli.eks_capabilities.load_eks_capabilities_config", loaded),
            patch("cli.eks_capabilities.capabilities_status", side_effect=statuses) as status_fn,
            patch("cli.commands.stacks_cmd._project_name", return_value=_PROJECT),
            patch(
                "cli.commands.stacks_cmd._load_cdk_json",
                return_value={"regional": [_REGION, "us-west-2"]},
            ),
        ):
            result = CliRunner().invoke(cli, args)
        return result, status_fn

    def test_table_mode_prints_one_row_per_type(self) -> None:
        result, status_fn = self._invoke(["stacks", "capabilities", "status"], [_healthy_status()])
        assert result.exit_code == 0, result.output
        assert status_fn.call_args.args == (_REGION, _PROJECT)
        assert "TYPE" in result.output and "CONFIGURED" in result.output
        for type_name in caps.EKS_CAPABILITY_TYPES:
            assert type_name in result.output
        assert "argocd" not in result.output

    def test_json_mode_emits_the_document_once(self) -> None:
        result, _ = self._invoke(
            ["--output", "json", "stacks", "capabilities", "status", "-r", _REGION],
            [_healthy_status()],
        )
        assert result.exit_code == 0, result.output
        document = json.loads(result.stdout)
        assert document["region"] == _REGION
        assert document["healthy"] is True
        assert [row["type"] for row in document["capabilities"]] == ["ack", "kro"]
        assert document["capabilities"][1]["status"] == "ACTIVE"

    def test_all_regions_json_is_a_list_and_drift_exits_nonzero(self) -> None:
        drifted = eks_capabilities.build_status(
            region="us-west-2", project_name=_PROJECT, config=_kro_config(), live=[]
        )
        result, status_fn = self._invoke(
            ["--output", "json", "stacks", "capabilities", "status", "--all-regions"],
            [_healthy_status(), drifted],
        )
        assert result.exit_code == 1
        documents = json.loads(result.stdout)
        assert [doc["region"] for doc in documents] == [_REGION, "us-west-2"]
        assert documents[1]["healthy"] is False
        assert "configured in cdk.json but not attached" in result.stderr
        assert [call.args[0] for call in status_fn.call_args_list] == [_REGION, "us-west-2"]

    def test_describe_failure_is_reported_and_exits_nonzero(self) -> None:
        result, _ = self._invoke(
            ["stacks", "capabilities", "status"], [RuntimeError("eks unavailable")]
        )
        assert result.exit_code == 1
        assert "eks unavailable" in result.output

    def test_invalid_cdk_json_block_exits_before_any_aws_call(self) -> None:
        result, status_fn = self._invoke(
            ["stacks", "capabilities", "status"],
            [],
            config_error=caps.EksCapabilitiesConfigError(
                "eks_capabilities.kro.enabled must be a boolean"
            ),
        )
        assert result.exit_code == 1
        assert "eks_capabilities.kro.enabled must be a boolean" in result.output
        status_fn.assert_not_called()

    def test_missing_cluster_and_unmanaged_capabilities_are_warned(self) -> None:
        status = _healthy_status()
        status["cluster_found"] = False
        status["healthy"] = False
        status["unmanaged"] = [
            {"capability_name": "team-kro", "type": "kro", "status": "ACTIVE", "arn": None}
        ]
        result, _ = self._invoke(["stacks", "capabilities", "status"], [status])
        assert result.exit_code == 1
        assert "is not deployed" in result.output
        assert "team-kro" in result.output


# ─── Import posture ──────────────────────────────────────────────────────────


def test_cli_capabilities_modules_import_without_aws_cdk() -> None:
    """The CLI reads the eks_capabilities block through a CDK-free module."""
    snippet = (
        "import sys\n"
        "import cli.eks_capabilities, gco.eks_capabilities_config\n"
        "assert 'aws_cdk' not in sys.modules, 'aws_cdk was imported'\n"
        "assert 'gco.config' not in sys.modules, 'gco.config was imported'\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
