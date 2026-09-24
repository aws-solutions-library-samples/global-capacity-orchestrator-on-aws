"""Tests for the live-validation ``eks-capabilities`` action and its checks module.

Covers turning the run's command-line inputs into the ``eks_capabilities_overrides``
CDK context (types, the Argo CD Identity Center inputs, the GitOps hand-off
defaults and the errors for an incomplete request), the Git-remote
normalization the repository URL default relies on, the effective-config merge
the action resolves, the AWS-side attachment check (through the same
``cli.eks_capabilities`` merge the CLI prints), the cluster-side Argo CD checks
against a scripted kubectl (local-cluster Secret, access-entry RBAC, the
AppProject fence, polling the root Application to Synced/Healthy at the run's
commit, terminal error conditions, the fixture ConfigMap's tracking label), the
action's evidence shape and its pass-with-a-note default, and the
``RunSettings`` identity threading. Every AWS, kubectl, tunnel and clock
boundary is faked; the CDK-free import posture of the config module is what
lets the harness reuse the CLI's merge.
"""

from __future__ import annotations

import base64
import json
import threading
from contextlib import nullcontext
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
ROLE_ARN = "arn:aws:iam::123456789012:role/gco-live-EksCapabilityArgoCdRole-ABC"
IDC_ARN = "arn:aws:sso:::instance/ssoins-1234567890abcdef"
SERVER_URL = "https://a1b2c3d4.argocd.us-east-1.eks.amazonaws.com"
REPO_URL = "https://github.com/example/gco.git"
SHA = "0123456789abcdef0123456789abcdef01234567"


# ─── run inputs -> overrides ─────────────────────────────────────────────────


class TestBuildOverrides:
    def test_kro_alone_needs_nothing_else(self) -> None:
        overrides = checks.build_eks_capabilities_overrides(
            types=("kro",),
            idc_instance_arn=None,
            idc_region=None,
            identities=(),
            gitops=True,
            repo_url=None,
            revision=None,
            path=checks.GITOPS_FIXTURE_PATH,
            sync_policy="automated",
        )
        assert overrides == {"kro": {"enabled": True}}
        # The result is a valid block for the deploy.
        assert caps.validate_eks_capabilities_config(overrides)["kro"]["enabled"] is True

    def test_argocd_with_gitops_defaults(self) -> None:
        overrides = checks.build_eks_capabilities_overrides(
            types=("argocd", "ack"),
            idc_instance_arn=IDC_ARN,
            idc_region="us-east-2",
            identities=("SSO_USER:u-1", "SSO_GROUP:g-1"),
            gitops=True,
            repo_url=REPO_URL,
            revision=SHA,
            path=checks.GITOPS_FIXTURE_PATH,
            sync_policy="automated",
        )
        assert overrides["ack"] == {"enabled": True}
        assert overrides["argocd"] == {
            "enabled": True,
            "idc_instance_arn": IDC_ARN,
            "idc_region": "us-east-2",
            "rbac_role_mappings": [
                {
                    "role": "ADMIN",
                    "identities": [
                        {"id": "u-1", "type": "SSO_USER"},
                        {"id": "g-1", "type": "SSO_GROUP"},
                    ],
                }
            ],
            "gitops": {
                "enabled": True,
                "repo_url": REPO_URL,
                "revision": SHA,
                "path": "examples/gitops/tenant-smoke",
                "destination_namespaces": ["gco-jobs"],
                "sync_policy": "automated",
            },
        }
        config = caps.validate_eks_capabilities_config(overrides, [REGION])
        assert caps.gitops_enabled_in_region(config, REGION)

    def test_argocd_without_gitops(self) -> None:
        overrides = checks.build_eks_capabilities_overrides(
            types=("argocd",),
            idc_instance_arn=IDC_ARN,
            idc_region=None,
            identities=("SSO_USER:u-1",),
            gitops=False,
            repo_url=None,
            revision=None,
            path=checks.GITOPS_FIXTURE_PATH,
            sync_policy="manual",
        )
        assert "gitops" not in overrides["argocd"]
        assert "idc_region" not in overrides["argocd"]

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"idc_instance_arn": None}, "--argocd-idc-instance-arn"),
            ({"idc_instance_arn": "ssoins-1"}, "--argocd-idc-instance-arn"),
            ({"identities": ()}, "--argocd-identity"),
            ({"identities": ("USER:u-1",)}, "expected TYPE:ID"),
            ({"identities": ("SSO_USER:",)}, "expected TYPE:ID"),
            ({"repo_url": None}, "--argocd-gitops-repo-url"),
            ({"revision": ""}, "--argocd-gitops-revision"),
        ],
    )
    def test_incomplete_argocd_requests_are_rejected(
        self, kwargs: dict[str, Any], match: str
    ) -> None:
        arguments: dict[str, Any] = {
            "types": ("argocd",),
            "idc_instance_arn": IDC_ARN,
            "idc_region": None,
            "identities": ("SSO_USER:u-1",),
            "gitops": True,
            "repo_url": REPO_URL,
            "revision": SHA,
            "path": checks.GITOPS_FIXTURE_PATH,
            "sync_policy": "automated",
        }
        arguments.update(kwargs)
        with pytest.raises(ValueError, match=match):
            checks.build_eks_capabilities_overrides(**arguments)

    def test_unknown_type_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown EKS capability type"):
            checks.build_eks_capabilities_overrides(
                types=("flux",),
                idc_instance_arn=None,
                idc_region=None,
                identities=(),
                gitops=False,
                repo_url=None,
                revision=None,
                path="x",
                sync_policy="manual",
            )

    def test_overrides_json_is_canonical(self) -> None:
        text = checks.overrides_json({"kro": {"enabled": True}, "ack": {"enabled": True}})
        assert text == '{"ack":{"enabled":true},"kro":{"enabled":true}}'
        assert caps.parse_eks_capabilities_overrides(text) == {
            "ack": {"enabled": True},
            "kro": {"enabled": True},
        }


class TestRepositoryUrl:
    @pytest.mark.parametrize(
        ("remote", "expected"),
        [
            ("git@github.com:example/gco.git", "https://github.com/example/gco.git"),
            ("ssh://git@github.com/example/gco.git", "https://github.com/example/gco.git"),
            ("https://github.com/example/gco.git", "https://github.com/example/gco.git"),
            (" https://github.com/example/gco\n", "https://github.com/example/gco"),
        ],
    )
    def test_https_repository_url(self, remote: str, expected: str) -> None:
        assert checks.https_repository_url(remote) == expected

    def test_default_reads_origin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        completed = SimpleNamespace(returncode=0, stdout="git@github.com:example/gco.git\n")
        run = MagicMock(return_value=completed)
        monkeypatch.setattr(checks.subprocess, "run", run)
        assert (
            checks.default_gitops_repository_url(tmp_path) == "https://github.com/example/gco.git"
        )
        assert run.call_args.args[0][:3] == ["git", "-C", str(tmp_path)]

    def test_default_is_none_without_a_remote(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            checks.subprocess,
            "run",
            MagicMock(return_value=SimpleNamespace(returncode=1, stdout="")),
        )
        assert checks.default_gitops_repository_url(tmp_path) is None


# ─── context and fakes ───────────────────────────────────────────────────────


def _argocd_overrides(*, gitops: bool = True) -> str:
    return checks.overrides_json(
        checks.build_eks_capabilities_overrides(
            types=("argocd",),
            idc_instance_arn=IDC_ARN,
            idc_region=None,
            identities=("SSO_USER:u-1",),
            gitops=gitops,
            repo_url=REPO_URL,
            revision=SHA,
            path=checks.GITOPS_FIXTURE_PATH,
            sync_policy="automated",
        )
    )


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
        kubeconfig_path=Path("/private/kubeconfig"),
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


def _capability(
    type_name: str, *, status: str = "ACTIVE", server_url: str | None = None
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "capabilityName": f"{PROJECT}-{type_name}",
        "arn": f"arn:aws:eks:{REGION}:123456789012:capability/{CLUSTER}/{type_name}",
        "type": caps.CAPABILITY_TYPE_API_NAMES[type_name],
        "roleArn": ROLE_ARN,
        "status": status,
        "version": "3.1.0",
    }
    if server_url:
        detail["configuration"] = {"argoCd": {"serverUrl": server_url}}
    return detail


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


def _secret(server: str = CLUSTER_ARN, *, labelled: bool = True) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": "local-cluster"}
    if labelled:
        metadata["labels"] = {"argocd.argoproj.io/secret-type": "cluster"}
    return {"metadata": metadata, "data": {"server": base64.b64encode(server.encode()).decode()}}


def _binding(group: str = f"eks-access-entry:{ROLE_ARN}") -> dict[str, Any]:
    return {"subjects": [{"kind": "Group", "name": group}]}


def _project(repos: list[str] | None = None) -> dict[str, Any]:
    return {"spec": {"sourceRepos": [REPO_URL] if repos is None else repos}}


def _application(
    *,
    sync: str = "Synced",
    health: str = "Healthy",
    revision: str = SHA,
    conditions: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "status": {
            "sync": {"status": sync, "revision": revision},
            "health": {"status": health},
            "operationState": {"phase": "Succeeded", "message": "successfully synced"},
            "conditions": conditions or [],
        }
    }


def _configmap(labelled: bool = True) -> dict[str, Any]:
    labels = {"app.kubernetes.io/instance": "gco-gitops-root"} if labelled else {}
    return {"metadata": {"name": "gco-gitops-tenant-smoke", "labels": labels}, "data": {"x": "y"}}


class _ScriptedKubectl:
    """kubectl double keyed on ``(kind, name)``; a list value is consumed per call."""

    def __init__(self, objects: dict[tuple[str, str], Any]) -> None:
        self.objects = dict(objects)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *argv: str, timeout: float | None = None, **_: Any) -> tuple[int, str, str]:
        self.calls.append(argv)
        assert argv[0] == "get"
        key = (argv[1], argv[2])
        value = self.objects.get(key, "absent")
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if value == "absent":
            return 1, "", f'Error from server (NotFound): {argv[1]} "{argv[2]}" not found'
        if isinstance(value, Exception):
            return 1, "", str(value)
        return 0, json.dumps(value), ""


def _all_objects(overrides: dict[tuple[str, str], Any] | None = None) -> dict[tuple[str, str], Any]:
    objects: dict[tuple[str, str], Any] = {
        ("secret", "local-cluster"): _secret(),
        ("clusterrolebinding", "gco-argocd-read-all"): _binding(),
        ("rolebinding", "gco-argocd-deploy"): {"kind": "RoleBinding"},
        ("appproject", "gco-tenants"): _project(),
        ("application", "gco-gitops-root"): _application(),
        ("configmap", "gco-gitops-tenant-smoke"): _configmap(),
    }
    objects.update(overrides or {})
    return objects


@pytest.fixture
def instant_clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    clock = SimpleNamespace(now=1000.0, sleeps=[])

    def monotonic() -> float:
        return clock.now

    def sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += max(float(seconds), 1.0)

    monkeypatch.setattr(checks.time, "monotonic", monotonic)
    monkeypatch.setattr(checks.time, "sleep", sleep)
    return clock


# ─── effective configuration ─────────────────────────────────────────────────


class TestEffectiveConfig:
    def test_merges_overrides_over_cdk_json(self) -> None:
        ctx = _context(
            overrides_json=_argocd_overrides(),
            cdk_context={"eks_capabilities": {"kro": {"enabled": True}}},
        )
        config = checks.effective_eks_capabilities_config(ctx)
        assert config["kro"]["enabled"] is True
        assert config["argocd"]["enabled"] is True
        assert checks.enabled_types_by_region(config, (REGION, "us-west-2")) == {
            REGION: ["argocd", "kro"],
            "us-west-2": ["argocd", "kro"],
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

    def test_gitops_expectations(self) -> None:
        config = checks.effective_eks_capabilities_config(
            _context(overrides_json=_argocd_overrides())
        )
        assert checks.gitops_expectations(config, REGION) == {
            "repo_url": REPO_URL,
            "revision": SHA,
            "path": "examples/gitops/tenant-smoke",
        }
        assert checks.gitops_expectations(caps.EKS_CAPABILITIES_DEFAULTS, REGION) is None


# ─── AWS side ────────────────────────────────────────────────────────────────


class TestVerifyCapabilitiesAttached:
    def test_active_capabilities_pass_and_carry_the_cluster_arn(self) -> None:
        session = _FakeSession(_FakeEks([_capability("argocd", server_url=SERVER_URL)]))
        ctx = _context(overrides_json=_argocd_overrides(), session=session)
        config = checks.effective_eks_capabilities_config(ctx)
        status = checks.verify_capabilities_attached(ctx, REGION, config)
        assert status["cluster_arn"] == CLUSTER_ARN
        assert status["healthy"] is True
        assert status["capabilities"][0]["argocd_server_url"] == SERVER_URL
        assert session.calls == [("eks", REGION)]

    def test_missing_capability_is_drift(self) -> None:
        session = _FakeSession(_FakeEks([]))
        ctx = _context(overrides_json=_argocd_overrides(), session=session)
        with pytest.raises(
            checks.EksCapabilitiesValidationError,
            match=r"argocd: configured in cdk\.json but not attached",
        ):
            checks.verify_capabilities_attached(
                ctx, REGION, checks.effective_eks_capabilities_config(ctx)
            )

    def test_non_active_status_is_drift(self) -> None:
        session = _FakeSession(
            _FakeEks([_capability("argocd", status="CREATING", server_url=SERVER_URL)])
        )
        ctx = _context(overrides_json=_argocd_overrides(), session=session)
        with pytest.raises(checks.EksCapabilitiesValidationError, match="status is CREATING"):
            checks.verify_capabilities_attached(
                ctx, REGION, checks.effective_eks_capabilities_config(ctx)
            )

    def test_active_argocd_without_a_server_url_fails(self) -> None:
        session = _FakeSession(_FakeEks([_capability("argocd")]))
        ctx = _context(overrides_json=_argocd_overrides(), session=session)
        with pytest.raises(checks.EksCapabilitiesValidationError, match="no server URL"):
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


# ─── cluster side ────────────────────────────────────────────────────────────


class TestClusterAccess:
    def _verify(self, objects: dict[tuple[str, str], Any]) -> dict[str, Any]:
        kubectl = _ScriptedKubectl(objects)
        return checks.verify_argocd_cluster_access(
            kubectl, {}, cluster_arn=CLUSTER_ARN, role_arn=ROLE_ARN, timeout=30
        )

    def test_registered_cluster_and_bound_group(self) -> None:
        assert self._verify(_all_objects()) == {
            "server": CLUSTER_ARN,
            "access_entry_group": f"eks-access-entry:{ROLE_ARN}",
        }

    def test_missing_secret(self) -> None:
        with pytest.raises(checks.EksCapabilitiesValidationError, match="Secret is absent"):
            self._verify(_all_objects({("secret", "local-cluster"): "absent"}))

    def test_secret_registering_another_server(self) -> None:
        objects = _all_objects()
        objects[("secret", "local-cluster")] = _secret("https://kubernetes.default.svc")
        with pytest.raises(checks.EksCapabilitiesValidationError, match="expected the cluster ARN"):
            self._verify(objects)

    def test_secret_without_the_cluster_label(self) -> None:
        objects = _all_objects()
        objects[("secret", "local-cluster")] = _secret(labelled=False)
        with pytest.raises(
            checks.EksCapabilitiesValidationError, match="secret-type=cluster label"
        ):
            self._verify(objects)

    def test_binding_to_the_wrong_group(self) -> None:
        objects = _all_objects()
        objects[("clusterrolebinding", "gco-argocd-read-all")] = _binding(
            "eks-access-entry:arn:aws:iam::1:role/other"
        )
        with pytest.raises(checks.EksCapabilitiesValidationError, match="access-entry group"):
            self._verify(objects)

    def test_missing_tenant_rolebinding(self) -> None:
        objects = _all_objects()
        objects[("rolebinding", "gco-argocd-deploy")] = "absent"
        with pytest.raises(
            checks.EksCapabilitiesValidationError,
            match="gco-jobs/gco-argocd-deploy RoleBinding is absent",
        ):
            self._verify(objects)


class TestGitOpsSync:
    def _wait(
        self, objects: dict[tuple[str, str], Any], *, revision: str = SHA, deadline: float = 100
    ) -> dict[str, Any]:
        kubectl = _ScriptedKubectl(objects)
        record: dict[str, Any] = {}
        state = checks.wait_for_gitops_sync(
            kubectl,
            record,
            expected_revision=revision,
            expected_repo_url=REPO_URL,
            poll_interval=5,
            timeout=30,
            deadline_seconds=deadline,
        )
        return {"state": state, "record": record, "kubectl": kubectl}

    def test_synced_healthy_at_the_pinned_commit(self, instant_clock: Any) -> None:
        result = self._wait(_all_objects())
        assert result["state"]["sync_status"] == "Synced"
        assert result["state"]["revision"] == SHA
        assert result["record"]["gitops_samples"][-1]["health_status"] == "Healthy"
        assert instant_clock.sleeps == []

    def test_polls_until_synced(self, instant_clock: Any) -> None:
        objects = _all_objects()
        objects[("application", "gco-gitops-root")] = [
            _application(sync="OutOfSync", health="Missing", revision=""),
            _application(sync="Synced", health="Progressing"),
            _application(),
        ]
        result = self._wait(objects)
        assert result["state"]["health_status"] == "Healthy"
        assert instant_clock.sleeps == [5, 5]
        assert len(result["record"]["gitops_samples"]) == 3

    def test_a_branch_revision_only_requires_synced_and_healthy(self, instant_clock: Any) -> None:
        objects = _all_objects()
        objects[("application", "gco-gitops-root")] = _application(revision="deadbeef" * 5)
        assert self._wait(objects, revision="main")["state"]["sync_status"] == "Synced"

    def test_synced_at_another_commit_keeps_polling_to_the_deadline(
        self, instant_clock: Any
    ) -> None:
        objects = _all_objects()
        objects[("application", "gco-gitops-root")] = _application(revision="f" * 40)
        with pytest.raises(
            checks.EksCapabilitiesValidationError, match="did not reach Synced/Healthy"
        ):
            self._wait(objects, deadline=12)
        assert instant_clock.sleeps

    def test_error_conditions_fail_immediately(self, instant_clock: Any) -> None:
        objects = _all_objects()
        objects[("application", "gco-gitops-root")] = _application(
            sync="Unknown",
            health="Unknown",
            conditions=[{"type": "ComparisonError", "message": "repository not found"}],
        )
        with pytest.raises(
            checks.EksCapabilitiesValidationError, match="ComparisonError: repository not found"
        ):
            self._wait(objects)
        assert instant_clock.sleeps == []

    def test_project_must_allow_the_repository(self, instant_clock: Any) -> None:
        objects = _all_objects()
        objects[("appproject", "gco-tenants")] = _project(["https://github.com/other/repo.git"])
        with pytest.raises(checks.EksCapabilitiesValidationError, match="not the run's repository"):
            self._wait(objects)

    def test_missing_project_or_application(self, instant_clock: Any) -> None:
        with pytest.raises(
            checks.EksCapabilitiesValidationError, match="AppProject gco-tenants is absent"
        ):
            self._wait(_all_objects({("appproject", "gco-tenants"): "absent"}))
        with pytest.raises(
            checks.EksCapabilitiesValidationError, match="Application gco-gitops-root is absent"
        ):
            self._wait(_all_objects({("application", "gco-gitops-root"): "absent"}))


class TestFixture:
    def test_tracked_configmap(self) -> None:
        result = checks.verify_gitops_fixture(_ScriptedKubectl(_all_objects()), {}, timeout=30)
        assert result["labels"]["app.kubernetes.io/instance"] == "gco-gitops-root"
        assert result["data"] == {"x": "y"}

    def test_absent_configmap(self) -> None:
        objects = _all_objects({("configmap", "gco-gitops-tenant-smoke"): "absent"})
        with pytest.raises(checks.EksCapabilitiesValidationError, match="is absent although"):
            checks.verify_gitops_fixture(_ScriptedKubectl(objects), {}, timeout=30)

    def test_untracked_configmap(self) -> None:
        objects = _all_objects(
            {("configmap", "gco-gitops-tenant-smoke"): _configmap(labelled=False)}
        )
        with pytest.raises(checks.EksCapabilitiesValidationError, match="tracking label"):
            checks.verify_gitops_fixture(_ScriptedKubectl(objects), {}, timeout=30)

    def test_fixture_file_matches_the_check(self) -> None:
        """The checked-in fixture is the object the harness looks for, namespace-less."""
        import yaml

        root = Path(__file__).resolve().parents[1]
        documents = list(
            yaml.safe_load_all(
                (root / checks.GITOPS_FIXTURE_PATH / "configmap.yaml").read_text(encoding="utf-8")
            )
        )
        assert len(documents) == 1
        configmap = documents[0]
        assert configmap["kind"] == "ConfigMap"
        assert configmap["metadata"]["name"] == checks.GITOPS_FIXTURE_CONFIGMAP
        assert "namespace" not in configmap["metadata"]
        assert caps.GITOPS_TENANT_NAMESPACES[0] == checks.GITOPS_FIXTURE_NAMESPACE


# ─── the action ──────────────────────────────────────────────────────────────


class TestAction:
    def test_passes_with_a_note_when_nothing_is_enabled(self) -> None:
        ctx = _context()
        evidence = action_module.action_eks_capabilities(ctx)
        assert evidence["enabled"] is False
        assert "--eks-capabilities" in evidence["detail"]
        assert ctx.checkpoint.state["eks_capabilities_validation"] == evidence
        ctx.persist.assert_called()

    def test_argocd_with_gitops_end_to_end(
        self, instant_clock: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession(_FakeEks([_capability("argocd", server_url=SERVER_URL)]))
        ctx = _context(overrides_json=_argocd_overrides(), session=session)
        kubectl = _ScriptedKubectl(_all_objects())
        sessions: list[tuple[Any, str]] = []

        class _Session:
            def __init__(self, context: Any, region: str) -> None:
                sessions.append((context, region))

            def __enter__(self) -> _ScriptedKubectl:
                return kubectl

            def __exit__(self, *exc: Any) -> None:
                return None

        monkeypatch.setattr(action_module, "cluster_kubectl", _Session)
        evidence = action_module.action_eks_capabilities(ctx)

        assert evidence["enabled"] is True
        region = evidence["regions"][REGION]
        assert region["enabled_types"] == ["argocd"]
        assert region["capabilities"] == [
            {
                "type": "argocd",
                "capability_name": f"{PROJECT}-argocd",
                "status": "ACTIVE",
                "version": "3.1.0",
                "arn": f"arn:aws:eks:{REGION}:123456789012:capability/{CLUSTER}/argocd",
                "role_arn": ROLE_ARN,
            }
        ]
        assert region["argocd_server_url"] == SERVER_URL
        assert region["cluster_access"]["server"] == CLUSTER_ARN
        assert region["gitops"]["enabled"] is True
        assert region["gitops"]["revision"] == SHA
        assert region["gitops"]["path"] == "examples/gitops/tenant-smoke"
        assert region["gitops"]["application"]["sync_status"] == "Synced"
        assert (
            region["gitops"]["fixture"]["labels"]["app.kubernetes.io/instance"] == "gco-gitops-root"
        )
        assert sessions == [(ctx, REGION)]
        assert ctx.checkpoint.state["eks_capabilities_validation"] == evidence

    def test_kro_alone_never_opens_a_tunnel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = _FakeSession(_FakeEks([_capability("kro")]))
        ctx = _context(overrides_json='{"kro": {"enabled": true}}', session=session)
        tunnel = MagicMock(side_effect=AssertionError("no kubectl needed for kro"))
        monkeypatch.setattr(action_module, "cluster_kubectl", tunnel)
        evidence = action_module.action_eks_capabilities(ctx)
        assert evidence["regions"][REGION]["enabled_types"] == ["kro"]
        assert "argocd_server_url" not in evidence["regions"][REGION]
        tunnel.assert_not_called()

    def test_argocd_without_gitops_records_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = _FakeSession(_FakeEks([_capability("argocd", server_url=SERVER_URL)]))
        ctx = _context(overrides_json=_argocd_overrides(gitops=False), session=session)
        kubectl = _ScriptedKubectl(_all_objects())
        monkeypatch.setattr(action_module, "cluster_kubectl", lambda *_: nullcontext(kubectl))
        evidence = action_module.action_eks_capabilities(ctx)
        assert evidence["regions"][REGION]["gitops"] == {"enabled": False}
        assert all(call[1] != "application" for call in kubectl.calls)

    def test_drift_fails_before_any_tunnel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = _FakeSession(_FakeEks([]))
        ctx = _context(overrides_json=_argocd_overrides(), session=session)
        tunnel = MagicMock()
        monkeypatch.setattr(action_module, "cluster_kubectl", tunnel)
        with pytest.raises(checks.EksCapabilitiesValidationError, match="not attached"):
            action_module.action_eks_capabilities(ctx)
        tunnel.assert_not_called()
        # The partial evidence is checkpointed for the report.
        assert ctx.checkpoint.state["eks_capabilities_validation"]["regions"][REGION][
            "enabled_types"
        ] == ["argocd"]


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
    with_capabilities = _settings(tmp_path, eks_capabilities_overrides_json=_argocd_overrides())
    context = with_capabilities.extra_cdk_context()
    assert context["eks_capabilities_overrides"] == _argocd_overrides()
    assert with_capabilities.identity()["extra_cdk_context"] == context
    assert plain.identity() != with_capabilities.identity()
    # The context value round-trips through the parser the CDK app uses.
    parsed = caps.parse_eks_capabilities_overrides(context["eks_capabilities_overrides"])
    assert parsed["argocd"]["gitops"]["path"] == "examples/gitops/tenant-smoke"
