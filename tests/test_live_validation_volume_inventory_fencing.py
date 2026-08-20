"""Fencing, durability, and lockstep contracts for the pre-destroy volume inventory.

Sibling of ``test_live_validation_volume_inventory.py`` (which owns record
building and the recorded-inventory happy path) and
``test_live_validation_volume_scenario.py`` (which owns the pure case/identity
contract). This module closes the boundary between them, offline:

* the strict authorization failures that reach the action itself — drifted
  checkpoint identity, an unverified or foreign account, branch drift, and
  refused CloudFormation stack ownership — each block the Region before any
  cluster or EC2 request and still leave durable failure evidence;
* exact cluster-tag evidence: a volume is recorded only for the exact tag key,
  and its tag value is recorded verbatim rather than normalized towards
  ``owned``;
* per-Region durability: each Region's evidence reaches the real owner-only
  atomic checkpoint before the next Region is read, and a resumed run reads it
  back from disk;
* resume identity: a changed fixture-cleanup authorization, a disabled
  scenario, and the sibling lifecycle's checkpoint are all refused; and
* the registry/export/docs lockstep for this one action.

Every AWS, kubectl, and git boundary is mocked; nothing here touches live
infrastructure or deletes anything.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.live_release_validation import registry as registry_module
from scripts.live_release_validation.actions import volume_inventory
from scripts.live_release_validation.models import (
    RunCheckpoint,
    RunContext,
    RunSettings,
    ValidationReport,
)
from scripts.live_release_validation.ownership import volumes as ownership_volumes

_ACCOUNT = "123456789012"
_CALLER_ARN = f"arn:aws:iam::{_ACCOUNT}:role/live-validation"
_BRANCH = "chore/volumes"
_PROJECT = "gco-live"
_PRIMARY = "us-east-1"
_SECONDARY = "us-west-2"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNBOOK = _REPO_ROOT / "docs" / "LIVE_RELEASE_VALIDATION.md"
_HARNESS_README = _REPO_ROOT / "scripts" / "live_release_validation" / "README.md"

#: The runbook row for the ``volume-inventory`` action, e.g.
#: ``| `volume-inventory` | ... |``. Compiled at module level rather than
#: calling ``re.match`` inline, which Python 3.15 soft-deprecates
#: (see ``tests/test_no_python_315_deprecation_surface.py``).
_RUNBOOK_VOLUME_INVENTORY_ROW = re.compile(r"^\|\s*`volume-inventory`\s*\|")


def _stack_name(region: str) -> str:
    return f"{_PROJECT}-{region}"


def _stack_id(region: str) -> str:
    return f"arn:aws:cloudformation:{region}:{_ACCOUNT}:stack/{_stack_name(region)}/abc"


def _cluster_tag_key(region: str) -> str:
    return f"kubernetes.io/cluster/{_stack_name(region)}"


def _owned_stacks(*regions: str) -> dict[str, Any]:
    return {region: {_stack_name(region): {"stack_id": _stack_id(region)}} for region in regions}


def _pvc(*, name: str, uid: str, volume_name: str) -> dict[str, Any]:
    return {
        "metadata": {"namespace": "gco-monitoring", "name": name, "uid": uid},
        "spec": {
            "storageClassName": "gp3",
            "volumeName": volume_name,
            "resources": {"requests": {"storage": "50Gi"}},
        },
        "status": {"phase": "Bound"},
    }


def _pv(*, name: str, uid: str, handle: str) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "uid": uid},
        "spec": {"csi": {"driver": "ebs.csi.aws.com", "volumeHandle": handle}},
    }


def _cluster_objects(volume_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One bound EBS-backed PVC per cluster, enough to drive a full record."""
    return (
        [_pvc(name="prometheus-db", uid=f"pvc-{volume_id}", volume_name=f"pv-{volume_id}")],
        [_pv(name=f"pv-{volume_id}", uid=f"pv-uid-{volume_id}", handle=volume_id)],
    )


def _volume_dto(
    *,
    volume_id: str,
    region: str,
    tags: dict[str, str] | None = None,
    state: str = "available",
) -> dict[str, Any]:
    return {
        "VolumeId": volume_id,
        "AvailabilityZone": f"{region}a",
        "Size": 50,
        "State": state,
        "Tags": [
            {"Key": key, "Value": value}
            for key, value in (tags or {_cluster_tag_key(region): "owned"}).items()
        ],
        "Attachments": [],
    }


def _settings(tmp_path: Path, **overrides: Any) -> RunSettings:
    base = RunSettings(
        run_id="run-123",
        repo_root=tmp_path,
        report_dir=tmp_path / "report",
        checkpoint_path=tmp_path / "report" / "checkpoint.json",
        expected_account=_ACCOUNT,
        expected_sha="a" * 40,
        expected_branch=_BRANCH,
        profile="configured",
        requested_actions=("all",),
        volume_scenario_case="retain-override",
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _runner_for(settings: RunSettings) -> Any:
    """A runner instance with nothing but settings, for its real checkpoint I/O."""
    from scripts.live_release_validation.runner import LiveValidationRunner

    instance = object.__new__(LiveValidationRunner)
    instance.settings = settings
    return instance


def _context(
    settings: RunSettings,
    *,
    session: Any,
    regions: tuple[str, ...] = (_PRIMARY,),
    persist: Callable[[RunCheckpoint], None] | None = None,
    **state: Any,
) -> RunContext:
    checkpoint = RunCheckpoint(identity=settings.identity())
    checkpoint.state.update(
        {
            "account_arn": _CALLER_ARN,
            "owned_stacks": _owned_stacks(*regions),
            **state,
        }
    )
    report = ValidationReport(
        run_id=settings.run_id,
        identity=settings.identity(),
        selected_actions=list(settings.requested_actions),
        started_at="2026-07-17T00:00:00+00:00",
    )
    return RunContext(
        settings=settings,
        checkpoint=checkpoint,
        report=report,
        # Observability sizing has its own module; keep these cases on identity.
        cdk_context={"cluster_observability": {"enabled": False}},
        deployment_regions=regions,
        config=SimpleNamespace(project_name=_PROJECT, global_region=_PRIMARY),
        session=session,
        stack_manager=MagicMock(),
        aws_client=MagicMock(),
        job_manager=MagicMock(),
        persist_callback=MagicMock() if persist is None else persist,
    )


class _Boundary:
    """Records every cluster and EC2 boundary the action crosses, in order."""

    def __init__(self, volumes: dict[str, list[dict[str, Any]]]) -> None:
        self.volumes = volumes
        self.calls: list[str] = []
        self.session = MagicMock()
        self.session.client.side_effect = self._client

    def _client(self, service_name: str, *, region_name: str) -> Any:
        self.calls.append(f"ec2:{region_name}")
        assert service_name == "ec2"
        client = MagicMock()
        client.describe_volumes.side_effect = lambda *, VolumeIds: {  # noqa: N803
            "Volumes": [
                volume
                for volume in self.volumes.get(region_name, [])
                if volume["VolumeId"] in VolumeIds
            ]
        }
        return client

    @contextmanager
    def kubectl(self, cluster_name: str, region: str) -> Iterator[Any]:
        self.calls.append(f"kubectl:{region}")
        assert cluster_name == _stack_name(region)
        volume_ids = [volume["VolumeId"] for volume in self.volumes.get(region, [])]
        pvcs: list[dict[str, Any]] = []
        pvs: list[dict[str, Any]] = []
        for volume_id in volume_ids or [f"vol-0{region[-1]}"]:
            claims, persistent = _cluster_objects(volume_id)
            pvcs.extend(claims)
            pvs.extend(persistent)

        def runner(*args: str, timeout: int = 120) -> tuple[int, str, str]:
            items = pvcs if args[1] == "persistentvolumeclaims" else pvs
            return 0, json.dumps({"items": items}), ""

        yield runner


def _run(
    ctx: RunContext,
    boundary: _Boundary,
    *,
    branch: str = _BRANCH,
    stack_authorization: Exception | None = None,
) -> dict[str, Any]:
    """Run the action with kubectl, git, and stack ownership mocked."""
    with (
        patch.object(volume_inventory, "cluster_kubectl", boundary.kubectl),
        patch.object(ownership_volumes, "_resolve_branch", return_value=branch),
        patch.object(
            ownership_volumes,
            "_authorize_owned_stack",
            side_effect=stack_authorization,
        ),
    ):
        return volume_inventory.action_volume_inventory(ctx)


def _recorded(ctx: RunContext) -> dict[str, Any]:
    state = ctx.checkpoint.state["volume_scenario"]
    inventory = state[volume_inventory.PRE_DESTROY_INVENTORY_KEY]
    assert isinstance(inventory, dict)
    return inventory


class TestStrictAuthorizationFailuresReachTheAction:
    """Every Region's authorization runs before its first cluster/EC2 request."""

    def test_drifted_checkpoint_identity_blocks_the_region(self, tmp_path: Path) -> None:
        boundary = _Boundary({_PRIMARY: [_volume_dto(volume_id="vol-0aaa", region=_PRIMARY)]})
        ctx = _context(
            _settings(tmp_path, confirm_ebs_fixture_cleanup=True),
            session=boundary.session,
        )
        ctx.checkpoint.identity["confirm_ebs_fixture_cleanup"] = False

        with pytest.raises(RuntimeError, match="Checkpoint identity does not match"):
            _run(ctx, boundary)

        assert boundary.calls == []
        assert _recorded(ctx)["status"] == "failed"

    def test_unverified_caller_identity_blocks_the_region(self, tmp_path: Path) -> None:
        boundary = _Boundary({})
        ctx = _context(_settings(tmp_path), session=boundary.session, account_arn="")

        with pytest.raises(RuntimeError, match="requires a checkpointed caller identity"):
            _run(ctx, boundary)

        assert boundary.calls == []

    def test_foreign_account_blocks_the_region(self, tmp_path: Path) -> None:
        boundary = _Boundary({})
        ctx = _context(
            _settings(tmp_path),
            session=boundary.session,
            account_arn="arn:aws:iam::210987654321:role/other",
        )

        with pytest.raises(RuntimeError, match="does not match expected account"):
            _run(ctx, boundary)

        assert boundary.calls == []

    def test_branch_drift_blocks_the_region(self, tmp_path: Path) -> None:
        boundary = _Boundary({})
        ctx = _context(_settings(tmp_path), session=boundary.session)

        with pytest.raises(RuntimeError, match="does not match expected branch"):
            _run(ctx, boundary, branch="main")

        assert boundary.calls == []

    def test_refused_stack_ownership_blocks_the_region(self, tmp_path: Path) -> None:
        boundary = _Boundary({_PRIMARY: [_volume_dto(volume_id="vol-0aaa", region=_PRIMARY)]})
        ctx = _context(_settings(tmp_path), session=boundary.session)

        with pytest.raises(RuntimeError, match="not owned by this run"):
            _run(
                ctx,
                boundary,
                stack_authorization=RuntimeError(
                    f"Stack {_PRIMARY}:{_stack_name(_PRIMARY)} is not owned by this run"
                ),
            )

        assert boundary.calls == []
        evidence = _recorded(ctx)["regions"][_PRIMARY]
        assert evidence["result"] == "failed"
        assert "not owned by this run" in evidence["error"]

    def test_a_blocked_region_records_no_volume_evidence(self, tmp_path: Path) -> None:
        boundary = _Boundary({_PRIMARY: [_volume_dto(volume_id="vol-0aaa", region=_PRIMARY)]})
        ctx = _context(_settings(tmp_path), session=boundary.session)

        with pytest.raises(RuntimeError):
            _run(ctx, boundary, branch="main")

        evidence = _recorded(ctx)["regions"][_PRIMARY]
        assert set(evidence) == {"region", "result", "error"}
        assert "volumes" not in evidence


class TestExactClusterTagEvidence:
    """The exact tag key selects the volume; its value is recorded verbatim."""

    def _evidence(self, tmp_path: Path, tags: dict[str, str]) -> dict[str, Any]:
        boundary = _Boundary(
            {_PRIMARY: [_volume_dto(volume_id="vol-0aaa", region=_PRIMARY, tags=tags)]}
        )
        ctx = _context(_settings(tmp_path), session=boundary.session)

        result = _run(ctx, boundary)

        assert result["status"] == "recorded"
        evidence = result["regions"][_PRIMARY]
        assert isinstance(evidence, dict)
        return evidence

    @pytest.mark.parametrize("tag_value", ["owned", "shared", "Owned", ""])
    def test_exact_tag_value_is_recorded_without_normalization(
        self,
        tmp_path: Path,
        tag_value: str,
    ) -> None:
        evidence = self._evidence(tmp_path, {_cluster_tag_key(_PRIMARY): tag_value})

        assert evidence["volume_ids"] == ["vol-0aaa"]
        assert evidence["cluster_tag_key"] == _cluster_tag_key(_PRIMARY)
        assert evidence["volumes"][0]["cluster_tag_key"] == _cluster_tag_key(_PRIMARY)
        assert evidence["volumes"][0]["cluster_tag_value"] == tag_value

    def test_another_clusters_tag_does_not_change_the_recorded_value(
        self,
        tmp_path: Path,
    ) -> None:
        evidence = self._evidence(
            tmp_path,
            {
                _cluster_tag_key(_PRIMARY): "owned",
                _cluster_tag_key(_SECONDARY): "shared",
                "Name": "prometheus",
            },
        )

        assert evidence["volumes"][0]["cluster_tag_value"] == "owned"

    @pytest.mark.parametrize(
        "tag_key",
        [
            f"{_cluster_tag_key(_PRIMARY)}-extra",
            f"kubernetes.io/cluster/{_stack_name(_PRIMARY)} ",
            f"Kubernetes.io/cluster/{_stack_name(_PRIMARY)}",
            "kubernetes.io/cluster/gco-live",
        ],
    )
    def test_near_miss_tag_keys_are_recorded_as_out_of_scope(
        self,
        tmp_path: Path,
        tag_key: str,
    ) -> None:
        evidence = self._evidence(tmp_path, {tag_key: "owned"})

        assert evidence["volume_ids"] == []
        assert evidence["volumes"] == []
        assert [item["reason_code"] for item in evidence["non_participating"]] == [
            "ebs-volume-outside-target-scope"
        ]
        assert _cluster_tag_key(_PRIMARY) in evidence["non_participating"][0]["reason"]


class TestPerRegionDurability:
    """Evidence reaches the real owner-only checkpoint as it is observed."""

    def _context_with_real_persistence(
        self,
        settings: RunSettings,
        boundary: _Boundary,
        *,
        regions: tuple[str, ...],
        owned: tuple[str, ...],
    ) -> RunContext:
        runner = _runner_for(settings)
        ctx = _context(
            settings,
            session=boundary.session,
            regions=regions,
            persist=runner._persist_checkpoint,
            owned_stacks=_owned_stacks(*owned),
        )
        runner._persist_checkpoint(ctx.checkpoint)
        return ctx

    def _persisted(self, settings: RunSettings) -> dict[str, Any]:
        checkpoint = RunCheckpoint.from_path(settings.checkpoint_path)
        inventory = checkpoint.state["volume_scenario"][volume_inventory.PRE_DESTROY_INVENTORY_KEY]
        assert isinstance(inventory, dict)
        return inventory

    def test_each_region_is_durable_before_the_next_is_read(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        boundary = _Boundary(
            {
                _PRIMARY: [_volume_dto(volume_id="vol-0aaa", region=_PRIMARY)],
                _SECONDARY: [_volume_dto(volume_id="vol-0bbb", region=_SECONDARY)],
            }
        )
        ctx = self._context_with_real_persistence(
            settings,
            boundary,
            regions=(_PRIMARY, _SECONDARY),
            owned=(_PRIMARY,),
        )

        with pytest.raises(RuntimeError, match="No checkpointed CloudFormation identity"):
            _run(ctx, boundary)

        # The second Region never became a cluster or EC2 request.
        assert boundary.calls == [f"kubectl:{_PRIMARY}", f"ec2:{_PRIMARY}"]
        persisted = self._persisted(settings)
        assert persisted["status"] == "failed"
        assert persisted["regions"][_PRIMARY]["result"] == "recorded"
        assert persisted["regions"][_PRIMARY]["volume_ids"] == ["vol-0aaa"]
        assert persisted["regions"][_PRIMARY]["cluster_tag_key"] == _cluster_tag_key(_PRIMARY)
        assert persisted["regions"][_SECONDARY]["result"] == "failed"

    def test_every_region_records_its_own_exact_identity(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        boundary = _Boundary(
            {
                _PRIMARY: [_volume_dto(volume_id="vol-0aaa", region=_PRIMARY)],
                _SECONDARY: [_volume_dto(volume_id="vol-0bbb", region=_SECONDARY)],
            }
        )
        ctx = self._context_with_real_persistence(
            settings,
            boundary,
            regions=(_PRIMARY, _SECONDARY),
            owned=(_PRIMARY, _SECONDARY),
        )

        result = _run(ctx, boundary)

        assert result["status"] == "recorded"
        assert boundary.calls == [
            f"kubectl:{_PRIMARY}",
            f"ec2:{_PRIMARY}",
            f"kubectl:{_SECONDARY}",
            f"ec2:{_SECONDARY}",
        ]
        persisted = self._persisted(settings)
        for region, volume_id in ((_PRIMARY, "vol-0aaa"), (_SECONDARY, "vol-0bbb")):
            evidence = persisted["regions"][region]
            assert evidence["stack_id"] == _stack_id(region)
            assert evidence["cluster_name"] == _stack_name(region)
            assert evidence["cluster_tag_key"] == _cluster_tag_key(region)
            assert evidence["volume_ids"] == [volume_id]
            assert evidence["volumes"][0]["region"] == region

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission model only")
    def test_persisted_inventory_stays_owner_only(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        boundary = _Boundary({_PRIMARY: [_volume_dto(volume_id="vol-0aaa", region=_PRIMARY)]})
        ctx = self._context_with_real_persistence(
            settings,
            boundary,
            regions=(_PRIMARY,),
            owned=(_PRIMARY,),
        )

        _run(ctx, boundary)

        assert stat.S_IMODE(settings.report_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(settings.checkpoint_path.stat().st_mode) == 0o600
        leftovers = [
            path.name for path in settings.report_dir.iterdir() if path.name != "checkpoint.json"
        ]
        assert leftovers == []

    def test_a_resumed_run_reads_the_recorded_inventory_from_disk(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        boundary = _Boundary({_PRIMARY: [_volume_dto(volume_id="vol-0aaa", region=_PRIMARY)]})
        ctx = self._context_with_real_persistence(
            settings,
            boundary,
            regions=(_PRIMARY,),
            owned=(_PRIMARY,),
        )

        result = _run(ctx, boundary)

        resumed = _runner_for(dataclasses.replace(settings, resume=True))._load_checkpoint()
        assert resumed.identity == settings.identity()
        scenario = resumed.state["volume_scenario"]
        assert scenario["case"] == "retain-override"
        assert scenario[volume_inventory.PRE_DESTROY_INVENTORY_KEY] == json.loads(
            json.dumps(result)
        )
        assert scenario["authorizations"][f"pre-destroy-inventory:{_PRIMARY}"]["target"] == {
            "stack_name": _stack_name(_PRIMARY),
            "region": _PRIMARY,
            "stack_id": _stack_id(_PRIMARY),
        }


class TestResumeIdentityMismatch:
    """A checkpoint belongs to exactly one lifecycle and one authorization."""

    def _write(self, settings: RunSettings) -> None:
        _runner_for(settings)._load_checkpoint()

    def test_changed_fixture_cleanup_authorization_is_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        self._write(settings)

        authorized = dataclasses.replace(
            settings,
            confirm_ebs_fixture_cleanup=True,
            resume=True,
        )
        with pytest.raises(ValueError, match="Checkpoint identity does not match"):
            self._write(authorized)

    def test_resuming_a_scenario_run_as_a_disabled_run_is_refused(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        self._write(settings)

        disabled = dataclasses.replace(settings, volume_scenario_case="disabled", resume=True)
        with pytest.raises(ValueError, match="Checkpoint identity does not match"):
            self._write(disabled)

    def test_the_sibling_lifecycle_cannot_resume_this_checkpoint(self, tmp_path: Path) -> None:
        from scripts.live_release_validation.volume_scenario import volume_scenario_run_id

        retain = _settings(
            tmp_path,
            run_id=volume_scenario_run_id("run-123", "retain-override"),
        )
        self._write(retain)

        # Same checkpoint file, the other case's run identity: refused on both
        # the case and the run ID.
        delete = dataclasses.replace(
            retain,
            run_id=volume_scenario_run_id("run-123", "delete"),
            volume_scenario_case="delete",
            resume=True,
        )
        with pytest.raises(ValueError, match="Checkpoint identity does not match"):
            self._write(delete)

    def test_a_fresh_run_never_overwrites_another_cases_checkpoint(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        self._write(settings)
        original = settings.checkpoint_path.read_text(encoding="utf-8")

        fresh = dataclasses.replace(settings, volume_scenario_case="delete")
        with pytest.raises(ValueError, match="Checkpoint already exists"):
            self._write(fresh)

        assert settings.checkpoint_path.read_text(encoding="utf-8") == original


class TestRegistryExportAndDocsLockstep:
    """The action, its export, and its operator documentation move together."""

    def test_the_handler_is_exported_under_its_registry_name(self) -> None:
        from scripts.live_release_validation import actions

        definition = registry_module.build_action_registry()["volume-inventory"]

        assert definition.handler.__name__ in actions.__all__
        assert getattr(actions, definition.handler.__name__) is definition.handler
        assert definition.handler is volume_inventory.action_volume_inventory

    def test_the_runbook_documents_exactly_one_matching_row(self) -> None:
        definition = registry_module.build_action_registry()["volume-inventory"]
        rows = [
            line.strip()
            for line in _RUNBOOK.read_text(encoding="utf-8").splitlines()
            if _RUNBOOK_VOLUME_INVENTORY_ROW.match(line.strip())
        ]

        assert len(rows) == 1, f"expected one volume-inventory runbook row, found {len(rows)}"
        dependency_cell = rows[0].split("|")[2]
        assert set(re.findall(r"`([a-z-]+)`", dependency_cell)) == set(definition.dependencies)
        for documented in ("--volume-scenario", "PVC", "volumeHandle", "cluster tag"):
            assert documented in rows[0], f"runbook row does not mention {documented!r}"

    def test_the_harness_readme_documents_the_owning_modules(self) -> None:
        readme = _HARNESS_README.read_text(encoding="utf-8")

        for owned in (
            "actions/volume_inventory.py",
            "checks/volumes.py",
            "ownership/volumes.py",
            "--volume-scenario",
        ):
            assert owned in readme, f"harness README does not document {owned}"
