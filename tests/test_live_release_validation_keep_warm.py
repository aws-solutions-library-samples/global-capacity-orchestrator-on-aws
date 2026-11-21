"""The live harness's ``keep-warm`` action and its probe (``checks/keep_warm.py``).

The probe runs two keep-warm GPU Jobs back to back through the shared
API-transport lifecycle (kept, not deleted, because the idle record lives on
the Job), then holds the deployment to ``docs/GPU_KEEP_WARM.md``: the second
Job on the node the first left idle, no new NodeClaim in the pool, the first
Job's interval closed as ``reused``, and after the node is reclaimed the
second Job's closed as ``reclaimed`` about one window later. These tests drive
it with a scripted Get Job, a fake kubectl and a fake clock.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from scripts.live_release_validation.actions import keep_warm as keep_warm_action
from scripts.live_release_validation.checks import jobs as checks_jobs
from scripts.live_release_validation.checks import keep_warm as probe
from tests._live_validation_patching import patch_live_validation_helper

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "live_release_validation" / "manifests" / probe.KEEP_WARM_MANIFEST
NODE = "i-node-one"


class FakeClock:
    """``time.monotonic`` and ``time.sleep`` for the probe: sleeping advances the clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _ctx(*, window: str | None = None) -> SimpleNamespace:
    cdk_context: dict[str, Any] = {}
    if window is not None:
        cdk_context["gpu_keep_warm"] = {"window": window}
    return SimpleNamespace(
        deployment_regions=("us-east-1", "us-west-2"),
        cdk_context=cdk_context,
        settings=SimpleNamespace(run_id="run-123", poll_interval_seconds=10),
        checkpoint=SimpleNamespace(state={}),
        persist=MagicMock(),
        job_manager=SimpleNamespace(
            load_manifests=lambda path: list(
                yaml.safe_load_all(Path(path).read_text(encoding="utf-8"))
            )
        ),
    )


def _lifecycle(node: str, *, pool: str = "gpu-keep-warm-pool") -> dict[str, Any]:
    return {
        "status": "succeeded",
        "scheduling": {
            "node_name": node,
            "node_instance_type": "g4dn.xlarge",
            "node_capacity_type": "on-demand",
            "node_labels": {"karpenter.sh/nodepool": pool, "gco.aws/keep-warm": "true"},
        },
    }


def _record(node: str = NODE, *, seconds: int, ended_by: str, window: int = 600) -> dict[str, Any]:
    return {
        "node": node,
        "since": "2026-11-21T18:00:00Z",
        "until": "2026-11-21T18:10:00Z",
        "seconds": seconds,
        "ended_by": ended_by,
        "window_seconds": window,
    }


class FakeKubectl:
    """NodeClaims of the pool and the node's presence, as kubectl would print them."""

    def __init__(self) -> None:
        self.claims: list[dict[str, Any]] = [
            {"metadata": {"name": "gpu-keep-warm-pool-abc"}, "status": {"nodeName": NODE}}
        ]
        self.claims_after: list[dict[str, Any]] | None = None
        self.node_reads_until_gone = 3
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *argv: str, timeout: float) -> tuple[int, str, str]:
        self.calls.append(argv)
        if argv[:2] == ("get", "nodeclaims"):
            claims_reads = sum(1 for call in self.calls if call[:2] == ("get", "nodeclaims"))
            items = (
                self.claims if claims_reads == 1 or self.claims_after is None else self.claims_after
            )
            return 0, json.dumps({"items": items}), ""
        if argv[:2] == ("get", "node"):
            self.node_reads_until_gone -= 1
            if self.node_reads_until_gone <= 0:
                return 1, "", f'Error from server (NotFound): nodes "{argv[2]}" not found'
            return 0, json.dumps({"metadata": {"name": argv[2]}}), ""
        raise AssertionError(argv)


class Scenario:
    """The patched harness helpers one probe run talks to."""

    def __init__(self) -> None:
        self.lifecycles = {"first": _lifecycle(NODE), "second": _lifecycle(NODE)}
        # Get Job keep_warm answers per Job name, consumed in order (the last repeats).
        self.answers: dict[str, list[Any]] = {
            "gco-live-keep-warm-first-run-123": [
                None,
                {"idle": [_record(seconds=25, ended_by="reused")]},
            ],
            "gco-live-keep-warm-second-run-123": [
                {"idle": [{"node": NODE, "since": "2026-11-21T18:00:00Z"}]},
                {"idle": [_record(seconds=700, ended_by="reclaimed")]},
            ],
        }
        self.missing: set[str] = set()
        self.lifecycle_calls: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def run_lifecycle(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        self.lifecycle_calls.append(kwargs)
        step = kwargs["tokens"][probe.STEP_PLACEHOLDER]
        ctx.checkpoint.state.setdefault("jobs", [])
        return self.lifecycles[step]

    def register(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return {"name": kwargs["name"], "uid": f"uid-{kwargs['path']}", "path": kwargs["path"]}

    def get_job(self, ctx: Any, record: dict[str, Any]) -> dict[str, Any] | None:
        if record["name"] in self.missing:
            return None
        queue = self.answers[record["name"]]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        return {"metadata": {"name": record["name"]}, "keep_warm": answer}

    def delete(self, ctx: Any, record: dict[str, Any]) -> dict[str, Any]:
        self.deleted.append(record["name"])
        return {"deleted": record["name"]}

    def run(self, ctx: Any, kubectl: Any, clock: FakeClock | None = None) -> dict[str, Any]:
        clock = clock or FakeClock()
        with (
            patch_live_validation_helper("_run_api_transport_lifecycle", self.run_lifecycle),
            patch_live_validation_helper("_register_job", self.register),
            patch_live_validation_helper("_get_owned_job", self.get_job),
            patch_live_validation_helper("_delete_owned_job", self.delete),
            patch.object(probe, "time", clock),
        ):
            return probe.run_keep_warm_probe(ctx, kubectl)


class TestProbe:
    def test_back_to_back_jobs_share_the_node_and_both_records_close(self) -> None:
        scenario, kubectl = Scenario(), FakeKubectl()

        evidence = scenario.run(_ctx(), kubectl)

        assert evidence["region"] == "us-east-1"
        assert evidence["window_seconds"] == 600
        assert {step: job["node"] for step, job in evidence["jobs"].items()} == {
            "first": NODE,
            "second": NODE,
        }
        assert evidence["jobs"]["first"]["instance_type"] == "g4dn.xlarge"
        assert (
            evidence["nodeclaims_before_second"]
            == evidence["nodeclaims_after_second"]
            == ["gpu-keep-warm-pool-abc"]
        )
        assert evidence["first_record"]["ended_by"] == "reused"
        assert evidence["second_record"]["seconds"] == 700
        assert evidence["reclaim_wait_seconds"] == 20
        assert scenario.deleted == [
            "gco-live-keep-warm-first-run-123",
            "gco-live-keep-warm-second-run-123",
        ]
        # Each Job ran through the shared lifecycle, kept for its record.
        assert [call["path"] for call in scenario.lifecycle_calls] == [
            "keep-warm-first",
            "keep-warm-second",
        ]
        assert all(call["delete"] is False for call in scenario.lifecycle_calls)
        assert all(
            call["manifest_filename"] == "keep-warm-smoke-job.yaml"
            and call["marker_prefix"] == "KEEP_WARM"
            for call in scenario.lifecycle_calls
        )

    def test_the_deployed_window_is_the_one_held_to(self) -> None:
        scenario = Scenario()
        scenario.answers["gco-live-keep-warm-second-run-123"] = [
            {"idle": [_record(seconds=1300, ended_by="reclaimed", window=1200)]}
        ]

        evidence = scenario.run(_ctx(window="20m"), FakeKubectl())

        assert evidence["window_seconds"] == 1200

    def test_the_second_job_on_another_node_fails(self) -> None:
        scenario = Scenario()
        scenario.lifecycles["second"] = _lifecycle("i-node-two")
        with pytest.raises(RuntimeError, match="ran on i-node-two, not on i-node-one"):
            scenario.run(_ctx(), FakeKubectl())
        assert scenario.deleted == []

    def test_a_job_off_the_keep_warm_pool_fails(self) -> None:
        scenario = Scenario()
        scenario.lifecycles["first"] = _lifecycle(NODE, pool="gpu-x86-pool")
        with pytest.raises(RuntimeError, match="is not a gpu-keep-warm-pool node"):
            scenario.run(_ctx(), FakeKubectl())

    def test_a_job_without_a_node_fails(self) -> None:
        scenario = Scenario()
        scenario.lifecycles["first"] = {"scheduling": None}
        with pytest.raises(RuntimeError, match="Get Job names no node"):
            scenario.run(_ctx(), FakeKubectl())

    def test_a_new_nodeclaim_fails(self) -> None:
        kubectl = FakeKubectl()
        kubectl.claims_after = [
            *kubectl.claims,
            {"metadata": {"name": "gpu-keep-warm-pool-new"}, "status": {}},
        ]
        with pytest.raises(RuntimeError, match=r"launched NodeClaims \['gpu-keep-warm-pool-new'\]"):
            Scenario().run(_ctx(), kubectl)

    def test_a_node_no_nodeclaim_names_fails(self) -> None:
        kubectl = FakeKubectl()
        kubectl.claims = [{"metadata": {"name": "gpu-keep-warm-pool-abc"}, "status": {}}]
        with pytest.raises(RuntimeError, match="No gpu-keep-warm-pool NodeClaim names i-node-one"):
            Scenario().run(_ctx(), kubectl)

    def test_the_first_record_closing_any_other_way_fails(self) -> None:
        scenario = Scenario()
        scenario.answers["gco-live-keep-warm-first-run-123"] = [
            {"idle": [_record(seconds=25, ended_by="reclaimed")]}
        ]
        with pytest.raises(RuntimeError, match="closed as 'reclaimed', expected 'reused'"):
            scenario.run(_ctx(), FakeKubectl())

    def test_a_first_gap_as_long_as_the_window_fails(self) -> None:
        scenario = Scenario()
        scenario.answers["gco-live-keep-warm-first-run-123"] = [
            {"idle": [_record(seconds=600, ended_by="reused")]}
        ]
        with pytest.raises(RuntimeError, match="sat idle 600s before the second Job"):
            scenario.run(_ctx(), FakeKubectl())

    def test_a_record_that_never_closes_times_out(self) -> None:
        scenario = Scenario()
        scenario.answers["gco-live-keep-warm-first-run-123"] = [None]
        with pytest.raises(TimeoutError, match="did not close as reused within 180s"):
            scenario.run(_ctx(), FakeKubectl())

    def test_a_node_that_is_never_reclaimed_times_out(self) -> None:
        kubectl = FakeKubectl()
        kubectl.node_reads_until_gone = 10_000
        with pytest.raises(TimeoutError, match="i-node-one was not reclaimed within 1500s"):
            Scenario().run(_ctx(), kubectl)

    @pytest.mark.parametrize("seconds", [560, 1501])
    def test_a_reclaim_far_from_the_window_fails(self, seconds: int) -> None:
        scenario = Scenario()
        scenario.answers["gco-live-keep-warm-second-run-123"] = [
            {"idle": [_record(seconds=seconds, ended_by="reclaimed")]}
        ]
        with pytest.raises(RuntimeError, match=f"sat idle {seconds}s before it was reclaimed"):
            scenario.run(_ctx(), FakeKubectl())

    def test_a_record_with_another_window_fails(self) -> None:
        scenario = Scenario()
        scenario.answers["gco-live-keep-warm-second-run-123"] = [
            {"idle": [_record(seconds=700, ended_by="reclaimed", window=300)]}
        ]
        with pytest.raises(RuntimeError, match="carries a 300s window, but the run deployed 600s"):
            scenario.run(_ctx(), FakeKubectl())

    def test_a_job_deleted_while_its_record_is_awaited_fails(self) -> None:
        scenario = Scenario()
        scenario.missing.add("gco-live-keep-warm-first-run-123")
        with pytest.raises(RuntimeError, match="disappeared before its idle record was read"):
            scenario.run(_ctx(), FakeKubectl())


class TestNodeClaims:
    def test_claims_without_a_name_are_skipped_and_a_missing_node_is_none(self) -> None:
        def kubectl(*argv: str, timeout: float) -> tuple[int, str, str]:
            items = [
                {"metadata": {}},
                {"metadata": {"name": "a"}, "status": {"nodeName": ""}},
                {"metadata": {"name": "b"}},
            ]
            return 0, json.dumps({"items": items}), ""

        assert probe.pool_nodeclaims(kubectl, {}) == {"a": None, "b": None}

    def test_an_absent_resource_reads_as_no_claims(self) -> None:
        def kubectl(*argv: str, timeout: float) -> tuple[int, str, str]:
            return 1, "", 'error: the server doesn\'t have a resource type "nodeclaims" (NotFound)'

        assert probe.pool_nodeclaims(kubectl, {}) == {}


class TestWindow:
    def test_the_default_window_applies_without_the_block(self) -> None:
        assert probe.deployed_window_seconds(_ctx()) == 600  # type: ignore[arg-type]

    def test_a_configured_window_is_read(self) -> None:
        assert probe.deployed_window_seconds(_ctx(window="1h30m")) == 5400  # type: ignore[arg-type]


class TestAction:
    def test_runs_the_probe_in_the_first_regions_session_and_checkpoints_it(self) -> None:
        ctx = _ctx()
        session = MagicMock()
        session.__enter__.return_value = "kubectl"
        with (
            patch.object(keep_warm_action, "cluster_kubectl", return_value=session) as opened,
            patch.object(keep_warm_action, "run_keep_warm_probe", return_value={"ok": True}) as run,
        ):
            evidence = keep_warm_action.action_keep_warm(ctx)  # type: ignore[arg-type]

        assert evidence == {"ok": True}
        opened.assert_called_once_with(ctx, "us-east-1")
        run.assert_called_once_with(ctx, "kubectl")
        assert ctx.checkpoint.state["keep_warm_validation"] == {"ok": True}
        ctx.persist.assert_called_once_with()


class TestSharedLifecycle:
    """The two ``checks/jobs.py`` options the probe relies on."""

    def test_tokens_name_each_step_and_its_path_label(self) -> None:
        ctx = _ctx()
        for step in probe.STEPS:
            manifests, name, namespace = checks_jobs._load_manifest(
                ctx,  # type: ignore[arg-type]
                probe.KEEP_WARM_MANIFEST,
                {probe.STEP_PLACEHOLDER: step},
            )
            assert (name, namespace) == (f"gco-live-keep-warm-{step}-run-123", "gco-jobs")
            labels = manifests[0]["metadata"]["labels"]
            assert labels["gco.aws/validation-path"] == f"keep-warm-{step}"
            assert "__" not in json.dumps(manifests).replace("GCO_LIVE_KEEP_WARM_", "")

    def test_a_kept_lifecycle_returns_its_evidence_without_deleting(self) -> None:
        record = {
            "name": "job",
            "namespace": "gco-jobs",
            "region": "us-east-1",
            "path": "keep-warm-first",
            "uid": "uid-1",
        }
        ctx = SimpleNamespace(state_lock=MagicMock(), persist_callback=MagicMock(), checkpoint={})
        final = {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
        with (
            patch_live_validation_helper("_wait_for_owned_job_appearance", return_value={}),
            patch_live_validation_helper("_wait_for_owned_job_terminal", return_value=(final, [])),
            patch_live_validation_helper("_owned_job_logs", return_value="GCO_LIVE_X"),
            patch_live_validation_helper("_owned_job_pod_times", return_value=[]),
            patch_live_validation_helper("_delete_owned_job") as delete,
        ):
            evidence = checks_jobs._complete_job_lifecycle(
                ctx,  # type: ignore[arg-type]
                record=record,
                marker="GCO_LIVE_X",
                delete=False,
            )
        delete.assert_not_called()
        assert "deletion" not in evidence
        assert evidence["status"] == "succeeded"
        assert record["validation_evidence"] == evidence


class TestManifest:
    def test_the_smoke_job_selects_the_pool_the_way_keep_warm_does(self) -> None:
        from gco.job_admission import check_keep_warm, check_tolerations
        from gco.keep_warm import KEEP_WARM_LABEL, keep_warm_selector_conflicts

        job = yaml.safe_load(MANIFEST.read_text(encoding="utf-8").replace("__", "x"))
        pod = job["spec"]["template"]["spec"]

        assert pod["nodeSelector"][KEEP_WARM_LABEL] == "true"
        assert keep_warm_selector_conflicts(pod["nodeSelector"]) == []
        assert check_keep_warm(job) == (True, None)
        assert check_tolerations(job) == (True, None)
        assert pod["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == 1
        # The record must outlast the window and the reclaim the action waits for.
        assert job["spec"]["ttlSecondsAfterFinished"] > 600 + probe.RECLAIM_MARGIN_SECONDS
