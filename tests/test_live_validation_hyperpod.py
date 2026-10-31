"""Tests for the SageMaker HyperPod pieces of the live release validation harness.

``checks/hyperpod.py``: the release override, the merged block, the
enablement and its source, and the probe (SageMaker's view of the cluster,
the probe Job through the shared lifecycle, the placement report).
``actions/hyperpod.py``: validated or skipped with the source.
``actions/preflight.py::_hyperpod_preflight``: every Region with HyperPod on
checked with the merged block. ``inventory/scanners.py``: HyperPod clusters
owned by name or tag.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from cli.hyperpod_preflight import FAIL, PASS, WARN, Check, HyperpodPreflightReport
from scripts.live_release_validation.actions import hyperpod as actions_hyperpod
from scripts.live_release_validation.actions import preflight as actions_preflight
from scripts.live_release_validation.checks import hyperpod as checks_hyperpod
from scripts.live_release_validation.inventory import scanners as inventory_scanners
from tests._live_validation_patching import patch_live_validation_helper

_REGION = "us-east-1"
_ACCOUNT = "123456789012"
_PROJECT = "gco-live"
_CLUSTER = f"{_PROJECT}-hyperpod"
_ARN = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:cluster/abc123"
_OVERRIDES = checks_hyperpod.hyperpod_overrides_json()


def _ctx(
    *,
    cdk_context: dict[str, Any] | None = None,
    overrides: str = _OVERRIDES,
    regions: tuple[str, ...] = (_REGION,),
) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(run_id="run-123", hyperpod_overrides_json=overrides),
        cdk_context={"api_gateway": {"regional_api_enabled": True}, **(cdk_context or {})},
        deployment_regions=regions,
        config=SimpleNamespace(project_name=_PROJECT, global_region=_REGION),
        session=MagicMock(),
        aws_client=MagicMock(),
        checkpoint=SimpleNamespace(state={}),
        persist=MagicMock(),
    )


def _cluster(**overrides: Any) -> dict[str, Any]:
    cluster: dict[str, Any] = {
        "ClusterArn": _ARN,
        "ClusterName": _CLUSTER,
        "ClusterStatus": "InService",
        "NodeRecovery": "Automatic",
        "InstanceGroups": [
            {
                "InstanceGroupName": "gco-hyperpod",
                "InstanceType": "ml.g4dn.xlarge",
                "CurrentCount": 1,
                "TargetCount": 1,
                "Status": "InService",
            }
        ],
    }
    cluster.update(overrides)
    return cluster


def _node(status: str = "Running", **overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "InstanceId": "i-0abc",
        "InstanceGroupName": "gco-hyperpod",
        "InstanceType": "ml.g4dn.xlarge",
        "InstanceStatus": {"Status": status},
    }
    node.update(overrides)
    return node


def _sagemaker(
    ctx: SimpleNamespace, cluster: dict[str, Any], nodes: list[dict[str, Any]]
) -> MagicMock:
    client = MagicMock()
    client.describe_cluster.return_value = cluster
    client.get_paginator.return_value.paginate.return_value = [{"ClusterNodeSummaries": nodes}]
    ctx.session.client.return_value = client
    return client


def _lifecycle(capacity_type: str | None = "hyperpod") -> dict[str, Any]:
    return {
        "name": "gco-live-hyperpod-run-123",
        "status": "succeeded",
        "scheduling": {
            "node_name": "hyperpod-i-0abc",
            "node_capacity_type": capacity_type,
            "node_labels": {"gco.aws/capacity-provider": "hyperpod"},
        },
    }


# ---------------------------------------------------------------------------
# The run's override and the merged block
# ---------------------------------------------------------------------------


class TestOverridesAndConfig:
    def test_the_release_override_is_canonical_json(self) -> None:
        assert _OVERRIDES == (
            '{"enabled":true,"instance_groups":[{"instance_count":1,'
            '"instance_type":"ml.g4dn.xlarge","name":"gco-hyperpod"}]}'
        )
        assert json.loads(_OVERRIDES) == checks_hyperpod.RELEASE_HYPERPOD_OVERRIDES

    def test_effective_config_merges_cdk_json_its_overlay_and_the_override(self) -> None:
        ctx = _ctx(
            cdk_context={
                "hyperpod": {"enabled": False, "node_recovery": "None"},
                "hyperpod_regions": {_REGION: {"availability_zone": "us-east-1b"}},
            }
        )
        config = checks_hyperpod.effective_hyperpod_config(ctx, _REGION)
        assert config["enabled"] is True
        assert config["node_recovery"] == "None"
        assert config["availability_zone"] == "us-east-1b"
        # The override replaces the instance groups wholesale.
        assert [
            (g["name"], g["instance_type"], g["instance_count"]) for g in config["instance_groups"]
        ] == [("gco-hyperpod", "ml.g4dn.xlarge", 1)]
        other = checks_hyperpod.effective_hyperpod_config(ctx, "eu-west-1")
        assert other["availability_zone"] is None

    def test_without_an_override_cdk_json_decides(self) -> None:
        ctx = _ctx(cdk_context={}, overrides="")
        assert checks_hyperpod.effective_hyperpod_config(ctx, _REGION)["enabled"] is False
        assert checks_hyperpod.effective_hyperpod_enablement(ctx) == {
            "enabled": False,
            "source": "cdk.json hyperpod",
            "region": _REGION,
            "cluster_name": _CLUSTER,
            "instance_groups": [
                {"name": "accelerated", "instance_type": "ml.g5.8xlarge", "instance_count": 1}
            ],
        }
        ctx = _ctx(cdk_context={"hyperpod": {"enabled": True, "cluster_name": "hp"}}, overrides="")
        state = checks_hyperpod.effective_hyperpod_enablement(ctx)
        assert (state["enabled"], state["source"], state["cluster_name"]) == (
            True,
            "cdk.json hyperpod",
            "hp",
        )
        assert checks_hyperpod.effective_hyperpod_enablement(_ctx())["source"] == (
            "run-override (hyperpod_overrides)"
        )

    def test_an_invalid_block_is_named(self) -> None:
        ctx = _ctx(cdk_context={"hyperpod": {"node_recovery": "Sometimes"}})
        with pytest.raises(
            checks_hyperpod.HyperpodValidationError, match="us-east-1: the hyperpod block"
        ):
            checks_hyperpod.effective_hyperpod_config(ctx, _REGION)


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


class TestProbe:
    def test_a_healthy_cluster_and_a_hyperpod_placed_job_pass(self) -> None:
        ctx = _ctx()
        client = _sagemaker(ctx, _cluster(), [_node()])
        lifecycle = _lifecycle()
        with patch_live_validation_helper(
            "_run_api_transport_lifecycle", return_value=lifecycle
        ) as run:
            evidence = checks_hyperpod.probe_hyperpod(ctx)
        ctx.session.client.assert_called_once_with("sagemaker", region_name=_REGION)
        client.describe_cluster.assert_called_once_with(ClusterName=_CLUSTER)
        client.get_paginator.assert_called_once_with("list_cluster_nodes")
        client.get_paginator.return_value.paginate.assert_called_once_with(ClusterName=_CLUSTER)
        assert run.call_args.args == (ctx,)
        assert run.call_args.kwargs == {
            "manifest_filename": "hyperpod-smoke-job.yaml",
            "path": "hyperpod",
            "marker_prefix": "HYPERPOD",
        }
        assert evidence == {
            "region": _REGION,
            "cluster": {
                "name": _CLUSTER,
                "arn": _ARN,
                "status": "InService",
                "node_recovery": "Automatic",
                "instance_groups": [
                    {
                        "name": "gco-hyperpod",
                        "instance_type": "ml.g4dn.xlarge",
                        "current": 1,
                        "target": 1,
                        "status": "InService",
                    }
                ],
                "nodes": [
                    {
                        "instance_id": "i-0abc",
                        "instance_group": "gco-hyperpod",
                        "instance_type": "ml.g4dn.xlarge",
                        "status": "Running",
                    }
                ],
            },
            "job": lifecycle,
        }

    @pytest.mark.parametrize(
        ("cluster", "nodes", "fragment"),
        [
            (
                _cluster(ClusterStatus="Failed", FailureMessage="capacity"),
                [_node()],
                f"cluster {_CLUSTER} is Failed, not InService (capacity)",
            ),
            (_cluster(ClusterStatus=""), [_node()], "is unknown, not InService"),
            (
                _cluster(
                    InstanceGroups=[
                        {"InstanceGroupName": "gco-hyperpod", "CurrentCount": 0, "TargetCount": 1}
                    ]
                ),
                [],
                "instance group gco-hyperpod has 0 of 1 instances, the block asks for 1",
            ),
            (
                _cluster(
                    InstanceGroups=[
                        {"InstanceGroupName": "other", "CurrentCount": 1, "TargetCount": 1}
                    ]
                ),
                [_node()],
                "instance group other is not in the block; instance group gco-hyperpod is missing",
            ),
            (
                _cluster(),
                [_node("Pending")],
                "instance i-0abc in gco-hyperpod is Pending, not Running",
            ),
            (_cluster(), [_node(InstanceStatus={})], "is unknown, not Running"),
            (
                _cluster(),
                [_node(), _node(InstanceId="i-1")],
                "SageMaker lists 2 instance(s), the block asks for 1",
            ),
        ],
    )
    def test_an_unhealthy_cluster_fails_before_any_job(
        self, cluster: dict[str, Any], nodes: list[dict[str, Any]], fragment: str
    ) -> None:
        ctx = _ctx()
        _sagemaker(ctx, cluster, nodes)
        with (
            patch_live_validation_helper("_run_api_transport_lifecycle") as run,
            pytest.raises(checks_hyperpod.HyperpodValidationError) as excinfo,
        ):
            checks_hyperpod.probe_hyperpod(ctx)
        assert fragment in str(excinfo.value)
        assert str(excinfo.value).startswith(f"{_REGION}: ")
        run.assert_not_called()

    @pytest.mark.parametrize(
        ("capacity_type", "fragment"),
        [
            ("spot", "ran on a spot node (hyperpod-i-0abc), not a HyperPod node"),
            (None, "ran on a unknown node (hyperpod-i-0abc), not a HyperPod node"),
        ],
    )
    def test_a_job_placed_elsewhere_fails(self, capacity_type: str | None, fragment: str) -> None:
        ctx = _ctx()
        _sagemaker(ctx, _cluster(), [_node()])
        with (
            patch_live_validation_helper(
                "_run_api_transport_lifecycle", return_value=_lifecycle(capacity_type)
            ),
            pytest.raises(checks_hyperpod.HyperpodValidationError) as excinfo,
        ):
            checks_hyperpod.probe_hyperpod(ctx)
        assert fragment in str(excinfo.value)

    def test_a_lifecycle_without_scheduling_fails(self) -> None:
        ctx = _ctx()
        _sagemaker(ctx, _cluster(), [_node()])
        with (
            patch_live_validation_helper(
                "_run_api_transport_lifecycle", return_value={"status": "succeeded"}
            ),
            pytest.raises(checks_hyperpod.HyperpodValidationError, match="no node reported"),
        ):
            checks_hyperpod.probe_hyperpod(ctx)


# ---------------------------------------------------------------------------
# The action
# ---------------------------------------------------------------------------


class TestAction:
    def test_validated_with_the_override(self) -> None:
        ctx = _ctx()
        _sagemaker(ctx, _cluster(), [_node()])
        with patch_live_validation_helper(
            "_run_api_transport_lifecycle", return_value=_lifecycle()
        ):
            evidence = actions_hyperpod.action_hyperpod(ctx)
        assert evidence["enablement"]["enabled"] is True
        assert evidence["hyperpod"]["status"] == "validated"
        assert evidence["hyperpod"]["source"] == "run-override (hyperpod_overrides)"
        assert evidence["hyperpod"]["cluster"]["status"] == "InService"
        assert evidence["hyperpod"]["job"]["scheduling"]["node_capacity_type"] == "hyperpod"
        assert ctx.checkpoint.state["hyperpod_validation"] is evidence
        ctx.persist.assert_called_once_with()

    def test_skipped_without_the_override(self) -> None:
        ctx = _ctx(overrides="")
        with patch_live_validation_helper("_run_api_transport_lifecycle") as run:
            evidence = actions_hyperpod.action_hyperpod(ctx)
        run.assert_not_called()
        ctx.session.client.assert_not_called()
        assert evidence["hyperpod"] == {
            "status": "skipped",
            "reason": (
                "Disabled by cdk.json hyperpod; a release run turns SageMaker HyperPod on "
                "through the hyperpod_overrides CDK context"
            ),
        }

    def test_a_probe_failure_fails_the_action(self) -> None:
        ctx = _ctx()
        _sagemaker(ctx, _cluster(ClusterStatus="Creating"), [])
        with pytest.raises(checks_hyperpod.HyperpodValidationError, match="is Creating"):
            actions_hyperpod.action_hyperpod(ctx)
        assert "hyperpod_validation" not in ctx.checkpoint.state


# ---------------------------------------------------------------------------
# The account preflight before deploy
# ---------------------------------------------------------------------------


def _report(region: str, *checks: tuple[str, str, str]) -> HyperpodPreflightReport:
    return HyperpodPreflightReport(
        region=region,
        enabled=True,
        cluster=_CLUSTER,
        checks=[Check(*check) for check in checks],
    )


class TestPreflightStep:
    def test_every_region_with_hyperpod_on_is_checked_with_the_merged_block(self) -> None:
        ctx = _ctx(
            regions=(_REGION, "eu-west-1"),
            cdk_context={"hyperpod_regions": {"eu-west-1": {"enabled": False}}},
        )
        reports = {
            _REGION: _report(_REGION, ("agent-image", PASS, "ok"), ("cluster-name", WARN, "?")),
            "eu-west-1": _report("eu-west-1", ("agent-image", PASS, "ok")),
        }
        calls: list[tuple[str, Mapping[str, Any], str, Any]] = []

        def fake_run_preflight(region, config, *, project, client_factory):
            calls.append((region, config, project, client_factory))
            return reports[region]

        with patch_live_validation_helper("run_hyperpod_preflight", side_effect=fake_run_preflight):
            summary = actions_preflight._hyperpod_preflight(ctx)
        # The override turns HyperPod on everywhere, the overlay notwithstanding.
        assert [call[0] for call in calls] == [_REGION, "eu-west-1"]
        assert all(call[1]["enabled"] is True for call in calls)
        assert all(
            call[1]["instance_groups"][0]["instance_type"] == "ml.g4dn.xlarge" for call in calls
        )
        assert all(call[2] == _PROJECT for call in calls)
        assert all(call[3] is ctx.session.client for call in calls)
        assert summary["ok"] is True
        assert [r["region"] for r in summary["regions"]] == [_REGION, "eu-west-1"]
        assert summary["regions_without_hyperpod"] == []

    def test_regions_without_hyperpod_are_recorded_and_not_checked(self) -> None:
        ctx = _ctx(regions=(_REGION, "eu-west-1"), overrides="")
        ctx.cdk_context["hyperpod_regions"] = {_REGION: {"enabled": True}}
        with patch_live_validation_helper(
            "run_hyperpod_preflight", return_value=_report(_REGION, ("agent-image", PASS, "ok"))
        ) as run:
            summary = actions_preflight._hyperpod_preflight(ctx)
        assert run.call_count == 1
        assert summary["regions_without_hyperpod"] == ["eu-west-1"]
        ctx = _ctx(overrides="")
        with patch_live_validation_helper("run_hyperpod_preflight") as run:
            assert actions_preflight._hyperpod_preflight(ctx) == {
                "ok": True,
                "regions": [],
                "regions_without_hyperpod": [_REGION],
            }
        run.assert_not_called()

    def test_a_failure_names_the_check_and_stops_the_run(self) -> None:
        ctx = _ctx()
        report = _report(
            _REGION,
            ("agent-image", PASS, "ok"),
            ("instance-types", FAIL, "ml.g4dn.xlarge: instance groups ask for 1, the quota is 0"),
        )
        with (
            patch_live_validation_helper("run_hyperpod_preflight", return_value=report),
            pytest.raises(
                RuntimeError, match=r"(?s)HyperPod preflight failed.*instance-types: ml.g4dn.xlarge"
            ),
        ):
            actions_preflight._hyperpod_preflight(ctx)

    def test_the_pcs_preflight_is_untouched_by_the_hyperpod_patch(self) -> None:
        # The two CLI modules both export run_preflight; the harness binds the
        # HyperPod one under its own name so a test patching one cannot catch
        # the other.
        assert actions_preflight.run_preflight is not actions_preflight.run_hyperpod_preflight
        assert actions_preflight.summarize is not actions_preflight.summarize_hyperpod


# ---------------------------------------------------------------------------
# The inventory scanner
# ---------------------------------------------------------------------------


class TestHyperpodScanner:
    _OWNED = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:cluster/owned0000"
    _TAGGED = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:cluster/tagged000"
    _FOREIGN = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:cluster/foreign00"

    def _session(self, summaries: list[dict[str, Any]]) -> tuple[MagicMock, MagicMock]:
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [{"ClusterSummaries": summaries}]
        tags = {
            self._TAGGED: {"Tags": [{"Key": "gco:project", "Value": _PROJECT}]},
            self._FOREIGN: {"Tags": [{"Key": "Project", "Value": "someone-else"}]},
        }
        client.list_tags.side_effect = lambda ResourceArn: tags[ResourceArn]
        session = MagicMock()
        session.client.return_value = client
        return session, client

    def test_clusters_are_owned_by_name_or_tag(self) -> None:
        session, client = self._session(
            [
                {"ClusterName": _CLUSTER, "ClusterArn": self._OWNED},
                {"ClusterName": "research", "ClusterArn": self._TAGGED},
                {"ClusterName": "theirs", "ClusterArn": self._FOREIGN},
            ]
        )
        result = inventory_scanners._list_project_hyperpod_resources(session, _REGION, _PROJECT)
        assert result == {"hyperpod_clusters": sorted([self._OWNED, self._TAGGED])}
        session.client.assert_called_once_with("sagemaker", region_name=_REGION)
        client.get_paginator.assert_called_once_with("list_clusters")
        # The project-named cluster needs no tag lookup; the other two are read.
        assert sorted(
            call.kwargs["ResourceArn"] for call in client.list_tags.call_args_list
        ) == sorted([self._TAGGED, self._FOREIGN])

    def test_a_cluster_without_identity_fails_closed(self) -> None:
        session, _client = self._session([{"ClusterName": "", "ClusterArn": self._OWNED}])
        with pytest.raises(RuntimeError, match="HyperPod cluster without identity"):
            inventory_scanners._list_project_hyperpod_resources(session, _REGION, _PROJECT)

    def test_no_clusters(self) -> None:
        session, _client = self._session([])
        assert inventory_scanners._list_project_hyperpod_resources(session, _REGION, _PROJECT) == {
            "hyperpod_clusters": []
        }
