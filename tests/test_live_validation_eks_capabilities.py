"""Tests for the live-validation ``eks-capabilities`` action and its checks module.

Covers turning the run's ``--eks-capabilities`` selection into the
``eks_capabilities_overrides`` CDK context (types, per-type settings, the
errors for an inconsistent request), the effective-config merge the action
resolves, the AWS-side attachment check (through the same
``cli.eks_capabilities`` merge the CLI prints), the action's evidence shape and
its pass-with-a-note default, and the ``RunSettings`` identity threading.
Every AWS boundary is faked; the CDK-free import posture of the config module
is what lets the harness reuse the CLI's merge.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gco import eks_capabilities_config as caps
from scripts.live_release_validation.actions import eks_capabilities as action_module
from scripts.live_release_validation.checks import eks_capabilities as checks
from scripts.live_release_validation.models import RunSettings

REGION = "us-east-1"
PROJECT = "gco-live"
CLUSTER = f"{PROJECT}-{REGION}"
CLUSTER_ARN = f"arn:aws:eks:{REGION}:123456789012:cluster/{CLUSTER}"
ROLE_ARN = "arn:aws:iam::123456789012:role/gco-live-EksCapabilityKroRole-ABC"
SHA = "0123456789abcdef0123456789abcdef01234567"
SQS_POLICY = "arn:aws:iam::aws:policy/AmazonSQSFullAccess"


# ─── run inputs -> overrides ─────────────────────────────────────────────────


class TestBuildOverrides:
    def test_each_type_is_enabled_everywhere(self) -> None:
        overrides = checks.build_eks_capabilities_overrides(types=("kro",))
        assert overrides == {"kro": {"enabled": True}}
        # The result is a valid block for the deploy.
        assert caps.validate_eks_capabilities_config(overrides)["kro"]["enabled"] is True

    def test_canonical_order_and_per_type_settings(self) -> None:
        overrides = checks.build_eks_capabilities_overrides(
            types=("kro", "ack"), extra={"ack": {"iam_policy_arns": [SQS_POLICY]}}
        )
        assert list(overrides) == ["ack", "kro"]
        assert overrides["ack"] == {"iam_policy_arns": [SQS_POLICY], "enabled": True}
        assert caps.validate_eks_capabilities_config(overrides)["ack"]["iam_policy_arns"] == [
            SQS_POLICY
        ]

    def test_settings_never_disable_a_selected_type(self) -> None:
        overrides = checks.build_eks_capabilities_overrides(
            types=("ack",), extra={"ack": {"enabled": False}}
        )
        assert overrides["ack"]["enabled"] is True

    def test_unknown_type_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown EKS capability type"):
            checks.build_eks_capabilities_overrides(types=("argocd",))

    def test_settings_for_an_unselected_type_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="does not enable: ack"):
            checks.build_eks_capabilities_overrides(
                types=("kro",), extra={"ack": {"iam_policy_arns": [SQS_POLICY]}}
            )

    def test_overrides_json_is_canonical(self) -> None:
        text = checks.overrides_json({"kro": {"enabled": True}, "ack": {"enabled": True}})
        assert text == '{"ack":{"enabled":true},"kro":{"enabled":true}}'
        assert caps.parse_eks_capabilities_overrides(text) == {
            "ack": {"enabled": True},
            "kro": {"enabled": True},
        }


class TestCommandLine:
    """``--eks-capabilities`` through the real parser."""

    _BASE = [
        "--expected-account",
        "123456789012",
        "--expected-sha",
        SHA,
        "--expected-branch",
        "chore/test",
        "--actions",
        "preflight",
    ]

    def _settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str
    ) -> RunSettings:
        from scripts.live_release_validation import __main__ as live_main

        monkeypatch.setattr(live_main, "_repository_root", lambda _value: tmp_path)
        parser = live_main._build_parser()
        return live_main._settings_from_args(parser, parser.parse_args([*self._BASE, *extra]))

    def test_named_types_become_the_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = self._settings(tmp_path, monkeypatch, "--eks-capabilities", "kro,ack")
        assert caps.parse_eks_capabilities_overrides(settings.eks_capabilities_overrides_json) == {
            "ack": {"enabled": True},
            "kro": {"enabled": True},
        }

    def test_all_selects_every_type_and_no_request_leaves_the_overrides_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        everything = self._settings(tmp_path, monkeypatch, "--eks-capabilities", "all")
        overrides = caps.parse_eks_capabilities_overrides(
            everything.eks_capabilities_overrides_json
        )
        assert set(overrides) == set(caps.EKS_CAPABILITY_TYPES)
        assert all(block["enabled"] for block in overrides.values())
        nothing = self._settings(tmp_path, monkeypatch)
        assert nothing.eks_capabilities_overrides_json == ""

    @pytest.mark.parametrize(
        ("extra", "message"),
        [
            (["--eks-capabilities", "flux"], "--eks-capabilities accepts"),
            # Argo CD is a Helm chart now, not a capability: named, it is refused.
            (["--eks-capabilities", "argocd"], "--eks-capabilities accepts"),
            (["--eks-capabilities", "all,kro"], "cannot be combined with individual names"),
        ],
    )
    def test_bad_requests_are_parser_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        extra: list[str],
        message: str,
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            self._settings(tmp_path, monkeypatch, *extra)
        assert excinfo.value.code == 2
        assert message in capsys.readouterr().err

    def test_the_retired_argocd_flags_are_gone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit):
            self._settings(tmp_path, monkeypatch, "--argocd-gitops-repo-url", "https://x")
        assert "unrecognized arguments" in capsys.readouterr().err


# ─── fakes ───────────────────────────────────────────────────────────────────


def _context(
    *,
    overrides_json: str = "",
    cdk_context: dict[str, Any] | None = None,
    session: Any = None,
    regions: tuple[str, ...] = (REGION,),
) -> SimpleNamespace:
    settings = SimpleNamespace(
        run_id="run-123",
        poll_interval_seconds=0,
        command_timeout_seconds=30,
        repo_root=Path("/repo"),
        report_dir=Path("/private"),
        eks_capabilities_overrides_json=overrides_json,
    )
    return SimpleNamespace(
        settings=settings,
        checkpoint=SimpleNamespace(state={}),
        state_lock=threading.RLock(),
        deployment_regions=regions,
        config=SimpleNamespace(project_name=PROJECT),
        cdk_context={} if cdk_context is None else cdk_context,
        session=session,
        persist=MagicMock(),
    )


def _capability(type_name: str, *, status: str = "ACTIVE") -> dict[str, Any]:
    return {
        "capabilityName": f"{PROJECT}-{type_name}",
        "arn": f"arn:aws:eks:{REGION}:123456789012:capability/{CLUSTER}/{type_name}",
        "type": caps.CAPABILITY_TYPE_API_NAMES[type_name],
        "roleArn": ROLE_ARN,
        "status": status,
        "version": "1.2.0",
    }


class _FakeEks:
    def __init__(
        self, capabilities: list[dict[str, Any]], *, cluster_arn: str = CLUSTER_ARN
    ) -> None:
        self.capabilities = capabilities
        self.cluster_arn = cluster_arn

    def describe_cluster(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"name": CLUSTER}
        return {"cluster": {"name": CLUSTER, "arn": self.cluster_arn}}

    def get_paginator(self, operation: str) -> Any:
        assert operation == "list_capabilities"
        items = [{"capabilityName": item["capabilityName"]} for item in self.capabilities]

        class _Paginator:
            def paginate(self, **kwargs: Any) -> Any:
                assert kwargs == {"clusterName": CLUSTER}
                yield {"capabilities": items}

        return _Paginator()

    def describe_capability(self, **kwargs: Any) -> dict[str, Any]:
        for item in self.capabilities:
            if item["capabilityName"] == kwargs["capabilityName"]:
                return {"capability": item}
        raise AssertionError(f"unexpected describe of {kwargs}")


class _FakeSession:
    def __init__(self, eks: _FakeEks) -> None:
        self.eks = eks
        self.calls: list[tuple[str, str]] = []

    def client(self, service: str, *, region_name: str) -> Any:
        self.calls.append((service, region_name))
        assert service == "eks"
        return self.eks


_BOTH = '{"ack":{"enabled":true},"kro":{"enabled":true}}'


class TestEffectiveConfig:
    def test_merges_overrides_over_cdk_json(self) -> None:
        ctx = _context(
            overrides_json='{"ack":{"enabled":true}}',
            cdk_context={"eks_capabilities": {"kro": {"enabled": True}}},
        )
        config = checks.effective_eks_capabilities_config(ctx)
        assert config["kro"]["enabled"] is True
        assert config["ack"]["enabled"] is True
        assert checks.enabled_types_by_region(config, (REGION, "us-west-2")) == {
            REGION: ["ack", "kro"],
            "us-west-2": ["ack", "kro"],
        }

    def test_nothing_enabled(self) -> None:
        config = checks.effective_eks_capabilities_config(_context())
        assert config == caps.EKS_CAPABILITIES_DEFAULTS
        assert checks.enabled_types_by_region(config, (REGION,)) == {}

    def test_invalid_merge_is_a_validation_error(self) -> None:
        ctx = _context(overrides_json='{"kro": {"enabled": "yes"}}')
        with pytest.raises(
            checks.EksCapabilitiesValidationError, match=r"kro\.enabled must be a boolean"
        ):
            checks.effective_eks_capabilities_config(ctx)


# ─── AWS side ────────────────────────────────────────────────────────────────


class TestVerifyCapabilitiesAttached:
    def test_active_capabilities_pass_and_carry_the_cluster_arn(self) -> None:
        session = _FakeSession(_FakeEks([_capability("ack"), _capability("kro")]))
        ctx = _context(overrides_json=_BOTH, session=session)
        config = checks.effective_eks_capabilities_config(ctx)
        status = checks.verify_capabilities_attached(ctx, REGION, config)
        assert status["cluster_arn"] == CLUSTER_ARN
        assert status["healthy"] is True
        assert session.calls == [("eks", REGION)]

    def test_missing_capability_is_drift(self) -> None:
        session = _FakeSession(_FakeEks([_capability("ack")]))
        ctx = _context(overrides_json=_BOTH, session=session)
        with pytest.raises(
            checks.EksCapabilitiesValidationError,
            match=r"kro: configured in cdk\.json but not attached",
        ):
            checks.verify_capabilities_attached(
                ctx, REGION, checks.effective_eks_capabilities_config(ctx)
            )

    def test_non_active_status_is_drift(self) -> None:
        session = _FakeSession(_FakeEks([_capability("kro", status="CREATING")]))
        ctx = _context(overrides_json='{"kro": {"enabled": true}}', session=session)
        with pytest.raises(checks.EksCapabilitiesValidationError, match="status is CREATING"):
            checks.verify_capabilities_attached(
                ctx, REGION, checks.effective_eks_capabilities_config(ctx)
            )

    def test_cluster_without_arn_fails(self) -> None:
        session = _FakeSession(_FakeEks([_capability("kro")], cluster_arn=""))
        ctx = _context(overrides_json='{"kro": {"enabled": true}}', session=session)
        with pytest.raises(checks.EksCapabilitiesValidationError, match="no ARN"):
            checks.verify_capabilities_attached(
                ctx, REGION, checks.effective_eks_capabilities_config(ctx)
            )


class TestAction:
    def test_passes_with_a_note_when_nothing_is_enabled(self) -> None:
        ctx = _context()
        evidence = action_module.action_eks_capabilities(ctx)
        assert evidence["enabled"] is False
        assert "--eks-capabilities" in evidence["detail"]
        assert ctx.checkpoint.state["eks_capabilities_validation"] == evidence
        ctx.persist.assert_called()

    def test_ack_and_kro_end_to_end(self) -> None:
        session = _FakeSession(_FakeEks([_capability("ack"), _capability("kro")]))
        ctx = _context(overrides_json=_BOTH, session=session)
        evidence = action_module.action_eks_capabilities(ctx)
        assert evidence["enabled"] is True
        region = evidence["regions"][REGION]
        assert region["enabled_types"] == ["ack", "kro"]
        assert region["cluster_arn"] == CLUSTER_ARN
        assert [row["type"] for row in region["capabilities"]] == ["ack", "kro"]
        assert region["capabilities"][1] == {
            "type": "kro",
            "capability_name": f"{PROJECT}-kro",
            "status": "ACTIVE",
            "version": "1.2.0",
            "arn": f"arn:aws:eks:{REGION}:123456789012:capability/{CLUSTER}/kro",
            "role_arn": ROLE_ARN,
        }
        assert ctx.checkpoint.state["eks_capabilities_validation"] == evidence

    def test_drift_fails_and_checkpoints_the_partial_evidence(self) -> None:
        session = _FakeSession(_FakeEks([]))
        ctx = _context(overrides_json='{"kro": {"enabled": true}}', session=session)
        with pytest.raises(checks.EksCapabilitiesValidationError, match="not attached"):
            action_module.action_eks_capabilities(ctx)
        assert ctx.checkpoint.state["eks_capabilities_validation"]["regions"][REGION][
            "enabled_types"
        ] == ["kro"]


# ─── RunSettings threading ───────────────────────────────────────────────────


def _settings(tmp_path: Path, **overrides: Any) -> RunSettings:
    report_dir = tmp_path / "report"
    arguments: dict[str, Any] = {
        "run_id": "run-1",
        "repo_root": tmp_path,
        "report_dir": report_dir,
        "checkpoint_path": report_dir / "checkpoint.json",
        "expected_account": "123456789012",
        "expected_sha": SHA,
        "expected_branch": "feat/x",
        "profile": "configured",
        "requested_actions": ("all",),
    }
    arguments.update(overrides)
    return RunSettings(**arguments)


def test_overrides_ride_the_cdk_context_and_the_resume_identity(tmp_path: Path) -> None:
    plain = _settings(tmp_path)
    assert "eks_capabilities_overrides" not in plain.extra_cdk_context()
    with_capabilities = _settings(tmp_path, eks_capabilities_overrides_json=_BOTH)
    context = with_capabilities.extra_cdk_context()
    assert context["eks_capabilities_overrides"] == _BOTH
    assert with_capabilities.identity()["extra_cdk_context"] == context
    assert plain.identity() != with_capabilities.identity()
    # The context value round-trips through the parser the CDK app uses.
    parsed = caps.parse_eks_capabilities_overrides(context["eks_capabilities_overrides"])
    assert parsed == {"ack": {"enabled": True}, "kro": {"enabled": True}}
