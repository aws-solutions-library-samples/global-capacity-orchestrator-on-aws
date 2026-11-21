"""Tests for cli/ecr_replication.py and its wiring into StackManager deploy and destroy.

The global stack's ``AWS::ECR::ReplicationConfiguration`` is the registry's
whole replication configuration: CloudFormation's create handler fails when
the registry has any rule, its update writes GCO's rule over every rule, and
its delete clears them all, using the DeletionPolicy of the template the
resource was last deployed with. These tests pin the retain policy on the
resource, the classification of GCO's own rule, every refusal the deploy and
destroy guards make, and that a destroy keeps the registry's other rules.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from botocore.exceptions import ClientError

from cli import ecr_replication as replication

ACCOUNT = "123456789012"
REGION = "us-east-2"


def _rule(filter_: str, *destinations: tuple[str, str]) -> dict[str, Any]:
    return {
        "destinations": [
            {"region": region, "registryId": registry} for region, registry in destinations
        ],
        "repositoryFilters": [{"filter": filter_, "filterType": "PREFIX_MATCH"}],
    }


GCO_RULE = _rule("gco/", ("us-east-1", ACCOUNT))
OTHER_RULE = _rule("team-a/", ("eu-west-1", ACCOUNT))
CROSS_ACCOUNT_GCO_PREFIX = _rule("gco/", ("us-east-1", "210987654321"))
CONTEXT = {"deployment_regions": {"global": REGION, "regional": ["us-east-1"]}}


class FakeEcr:
    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.rules = rules
        self.puts: list[dict[str, Any]] = []

    def describe_registry(self) -> dict[str, Any]:
        return {"registryId": ACCOUNT, "replicationConfiguration": {"rules": self.rules}}

    def put_replication_configuration(self, *, replicationConfiguration: dict[str, Any]) -> None:
        self.puts.append(replicationConfiguration)
        self.rules = replicationConfiguration["rules"]


class FakeCloudFormation:
    def __init__(self, template: Any = None, error: ClientError | None = None) -> None:
        self.template = template
        self.error = error

    def get_template(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"StackName": "gco-global", "TemplateStage": "Original"}
        if self.error is not None:
            raise self.error
        return {"TemplateBody": self.template}


def _template(policy: str | None) -> dict[str, Any]:
    resource: dict[str, Any] = {"Type": replication.REPLICATION_RESOURCE_TYPE}
    if policy is not None:
        resource["DeletionPolicy"] = policy
    return {"Resources": {"Queue": {"Type": "AWS::SQS::Queue"}, "Repl": resource}}


def _client_error(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "GetTemplate")


# ─── the stack ───────────────────────────────────────────────────────────────


def test_global_stack_retains_the_registry_replication_configuration() -> None:
    from gco.stacks.global_stack import GCOGlobalStack
    from tests.test_regional_stack import MockConfigLoader

    template = assertions.Template.from_stack(
        GCOGlobalStack(cdk.App(), "gco-global", config=MockConfigLoader())
    )
    resources = template.find_resources(replication.REPLICATION_RESOURCE_TYPE)
    ((logical_id, resource),) = resources.items()
    assert logical_id.startswith("GcoImageReplicationConfig"), "keep the logical id"
    assert resource["DeletionPolicy"] == "Retain"
    assert resource["UpdateReplacePolicy"] == "Retain"


# ─── rule classification ─────────────────────────────────────────────────────


def test_gco_rule_is_the_project_prefix_with_same_account_destinations() -> None:
    assert replication.is_gco_rule(GCO_RULE, project_name="gco", account_id=ACCOUNT)
    assert not replication.is_gco_rule(OTHER_RULE, project_name="gco", account_id=ACCOUNT)
    assert not replication.is_gco_rule(GCO_RULE, project_name="acme", account_id=ACCOUNT)
    assert not replication.is_gco_rule(
        CROSS_ACCOUNT_GCO_PREFIX, project_name="gco", account_id=ACCOUNT
    )
    assert not replication.is_gco_rule(
        {"repositoryFilters": GCO_RULE["repositoryFilters"], "destinations": []},
        project_name="gco",
        account_id=ACCOUNT,
    )
    two_filters = {**GCO_RULE, "repositoryFilters": GCO_RULE["repositoryFilters"] * 2}
    assert not replication.is_gco_rule(two_filters, project_name="gco", account_id=ACCOUNT)
    gco, other = replication.split_rules(
        [OTHER_RULE, GCO_RULE, CROSS_ACCOUNT_GCO_PREFIX], project_name="gco", account_id=ACCOUNT
    )
    assert gco == [GCO_RULE]
    assert other == [OTHER_RULE, CROSS_ACCOUNT_GCO_PREFIX]


@pytest.mark.parametrize(
    ("context", "expected_regions"),
    [
        (CONTEXT, ["us-east-1"]),
        ({"deployment_regions": {"global": REGION, "regional": "us-east-1"}}, ["us-east-1"]),
        (
            {
                **CONTEXT,
                "images": {"replication": {"destinations": [REGION, "eu-west-1", "us-west-2"]}},
            },
            ["eu-west-1", "us-west-2"],
        ),
        ({**CONTEXT, "images": {"replication": {"enabled": False}}}, None),
        ({"deployment_regions": {"global": REGION, "regional": [REGION]}}, None),
        ({**CONTEXT, "images": ["not", "a", "mapping"]}, None),
        ({**CONTEXT, "images": {"replication": "on"}}, None),
        ({}, None),
    ],
)
def test_desired_rule_mirrors_the_global_stack(
    context: dict[str, Any], expected_regions: list[str] | None
) -> None:
    desired = replication.desired_gco_rule(context, project_name="gco", account_id=ACCOUNT)
    if expected_regions is None:
        assert desired is None
        return
    assert desired == {
        "destinations": [{"region": region, "registryId": ACCOUNT} for region in expected_regions],
        "repositoryFilters": [{"filter": "gco/", "filterType": "PREFIX_MATCH"}],
    }


def test_read_registry_tolerates_a_registry_without_configuration() -> None:
    assert replication.read_registry(FakeEcr([GCO_RULE])) == (ACCOUNT, [GCO_RULE])
    empty = MagicMock()
    empty.describe_registry.return_value = {"registryId": ACCOUNT}
    assert replication.read_registry(empty) == (ACCOUNT, [])


# ─── deployed state ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        (_template("Retain"), "retained"),
        (_template(None), "deletes"),
        (_template("Delete"), "deletes"),
        (json.dumps(_template("Retain")), "retained"),
        ({"Resources": {"Queue": {"Type": "AWS::SQS::Queue"}, "Junk": "x"}}, "absent"),
        (None, "absent"),
    ],
)
def test_deployed_state_reads_the_last_deployed_deletion_policy(
    template: Any, expected: str
) -> None:
    assert replication.deployed_state(FakeCloudFormation(template), "gco-global") == expected


def test_deployed_state_treats_only_a_missing_stack_as_absent() -> None:
    missing = _client_error("ValidationError", "Stack with id gco-global does not exist")
    assert replication.deployed_state(FakeCloudFormation(error=missing), "gco-global") == "absent"
    denied = _client_error("AccessDenied", "not authorized")
    with pytest.raises(ClientError):
        replication.deployed_state(FakeCloudFormation(error=denied), "gco-global")
    other_validation = _client_error("ValidationError", "Template format error")
    with pytest.raises(ClientError):
        replication.deployed_state(FakeCloudFormation(error=other_validation), "gco-global")


# ─── deploy and destroy decisions ────────────────────────────────────────────


def _deploy(rules: list[dict[str, Any]], *, desired: Any, state: str) -> str | None:
    return replication.deploy_conflict(
        rules,
        desired=desired,
        state=state,  # type: ignore[arg-type]
        project_name="gco",
        account_id=ACCOUNT,
        region=REGION,
    )


@pytest.mark.parametrize("state", ["absent", "retained", "deletes"])
def test_deploy_never_refuses_a_registry_with_only_gco_rules_it_owns(state: str) -> None:
    assert _deploy([], desired=GCO_RULE, state=state) is None
    assert _deploy([], desired=None, state=state) is None
    if state != "absent":
        assert _deploy([GCO_RULE], desired=GCO_RULE, state=state) is None
    assert _deploy([GCO_RULE], desired=None, state=state) is None


def test_deploy_refuses_a_stale_gco_rule_the_create_would_trip_over() -> None:
    message = _deploy([GCO_RULE], desired=GCO_RULE, state="absent")
    assert message is not None
    assert "no longer exists" in message
    assert "aws ecr put-replication-configuration --region us-east-2" in message
    assert """'{"rules": []}'""" in message


def test_deploy_refuses_to_create_the_configuration_over_other_rules() -> None:
    message = _deploy([OTHER_RULE], desired=GCO_RULE, state="absent")
    assert message is not None
    assert "[team-a/ -> 123456789012/eu-west-1]" in message
    assert "images.replication.enabled to false" in message


def test_deploy_refuses_a_rule_change_that_would_rewrite_other_rules() -> None:
    changed = _rule("gco/", ("us-east-1", ACCOUNT), ("us-west-2", ACCOUNT))
    message = _deploy([GCO_RULE, OTHER_RULE], desired=changed, state="retained")
    assert message is not None and "rewrites the registry's whole" in message
    # An unchanged rule is not rewritten (CloudFormation updates changed properties only).
    assert _deploy([OTHER_RULE, GCO_RULE], desired=GCO_RULE, state="retained") is None
    assert _deploy([OTHER_RULE, GCO_RULE], desired=GCO_RULE, state="deletes") is None


def test_deploy_refuses_removing_an_unretained_configuration_over_other_rules() -> None:
    message = _deploy([GCO_RULE, OTHER_RULE], desired=None, state="deletes")
    assert message is not None and "delete every rule in the registry" in message
    assert _deploy([GCO_RULE, OTHER_RULE], desired=None, state="retained") is None
    assert _deploy([OTHER_RULE], desired=None, state="absent") is None


def test_destroy_refuses_only_an_unretained_configuration_over_other_rules() -> None:
    def destroy(rules: list[dict[str, Any]], state: str) -> str | None:
        return replication.destroy_conflict(
            rules,
            state=state,  # type: ignore[arg-type]
            project_name="gco",
            account_id=ACCOUNT,
            region=REGION,
        )

    message = destroy([GCO_RULE, OTHER_RULE], "deletes")
    assert message is not None and "gco stacks deploy gco-global" in message
    assert destroy([GCO_RULE], "deletes") is None
    assert destroy([GCO_RULE, OTHER_RULE], "retained") is None
    assert destroy([OTHER_RULE], "absent") is None


def test_remove_gco_rules_keeps_every_other_rule() -> None:
    ecr = FakeEcr([OTHER_RULE, GCO_RULE, CROSS_ACCOUNT_GCO_PREFIX])
    assert replication.remove_gco_rules(ecr, project_name="gco") == 1
    assert ecr.puts == [{"rules": [OTHER_RULE, CROSS_ACCOUNT_GCO_PREFIX]}]
    assert replication.remove_gco_rules(ecr, project_name="gco") == 0
    assert len(ecr.puts) == 1, "nothing to remove means no write"
    only_gco = FakeEcr([GCO_RULE])
    assert replication.remove_gco_rules(only_gco, project_name="gco") == 1
    assert only_gco.puts == [{"rules": []}]


# ─── StackManager wiring ─────────────────────────────────────────────────────


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from cli.stacks import StackManager

    (tmp_path / "cdk.json").write_text(json.dumps({"context": CONTEXT}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config = MagicMock()
    config.project_name = "gco"
    config.global_region = REGION
    with patch.object(StackManager, "_ensure_lambda_build", lambda self: None):
        return StackManager(config, project_root=tmp_path)


def _clients(cloudformation: Any, ecr: Any) -> Any:
    def client(service: str, region_name: str) -> Any:
        assert region_name == REGION
        return {"cloudformation": cloudformation, "ecr": ecr}[service]

    return patch("boto3.client", side_effect=client)


def test_context_reader_handles_a_missing_or_odd_cdk_json(manager: Any, tmp_path: Path) -> None:
    assert manager._ecr_replication_context() == CONTEXT
    (tmp_path / "cdk.json").write_text(json.dumps({"context": ["odd"]}), encoding="utf-8")
    assert manager._ecr_replication_context() == {}
    with patch("cli.stacks._find_cdk_json", return_value=None):
        assert manager._ecr_replication_context() == {}


def test_deploy_preflight_reads_the_stack_and_the_registry(manager: Any) -> None:
    ecr = FakeEcr([OTHER_RULE])
    with _clients(
        FakeCloudFormation(error=_client_error("ValidationError", "does not exist")), ecr
    ):
        message = manager._ecr_replication_deploy_preflight()
    assert message is not None and "only in a registry without rules" in message
    with _clients(FakeCloudFormation(_template("Retain")), FakeEcr([OTHER_RULE, GCO_RULE])):
        assert manager._ecr_replication_deploy_preflight() is None


def test_deploy_preflight_skips_the_registry_when_gco_cannot_touch_it(
    manager: Any, tmp_path: Path
) -> None:
    disabled = {**CONTEXT, "images": {"replication": {"enabled": False}}}
    (tmp_path / "cdk.json").write_text(json.dumps({"context": disabled}), encoding="utf-8")
    ecr = MagicMock()
    with _clients(FakeCloudFormation(_template("Retain")), ecr):
        assert manager._ecr_replication_deploy_preflight() is None
    ecr.describe_registry.assert_not_called()
    with _clients(FakeCloudFormation(_template(None)), FakeEcr([GCO_RULE, OTHER_RULE])):
        message = manager._ecr_replication_deploy_preflight()
    assert message is not None and "Deploy once with images.replication.enabled" in message


def test_destroy_preflight_and_cleanup(manager: Any) -> None:
    ecr = MagicMock()
    with _clients(FakeCloudFormation(_template("Retain")), ecr):
        assert manager._ecr_replication_destroy_preflight() is None
    ecr.describe_registry.assert_not_called()
    with _clients(FakeCloudFormation(_template(None)), FakeEcr([OTHER_RULE])):
        message = manager._ecr_replication_destroy_preflight()
    assert message is not None and "Redeploy gco-global" in message
    registry = FakeEcr([GCO_RULE, OTHER_RULE])
    with _clients(FakeCloudFormation(), registry):
        assert manager._remove_gco_replication_rules() == 1
    assert registry.rules == [OTHER_RULE]


@pytest.mark.parametrize(
    ("kwargs", "checked"),
    [
        ({"stack_name": "gco-global", "exclusively": True}, True),
        ({"stack_name": "gco-us-east-1", "exclusively": False}, True),
        ({"all_stacks": True, "exclusively": True}, True),
        ({"stack_name": None, "exclusively": True}, True),
        ({"stack_name": "gco-us-east-1", "exclusively": True}, False),
    ],
)
def test_deploy_runs_the_guard_whenever_cdk_can_reach_the_global_stack(
    manager: Any, kwargs: dict[str, Any], checked: bool
) -> None:
    sentinel = RuntimeError("stop after the guard")
    with (
        patch.object(manager, "ensure_asset_sources_readable"),
        patch.object(manager, "_ecr_replication_deploy_preflight", return_value=None) as guard,
        patch.object(manager, "_sync_lambda_sources", side_effect=sentinel),
        pytest.raises(RuntimeError, match="stop after the guard"),
    ):
        manager.deploy(**kwargs)
    assert guard.called is checked


def test_deploy_raises_the_refusal_before_building_anything(manager: Any) -> None:
    with (
        patch.object(manager, "ensure_asset_sources_readable"),
        patch.object(manager, "_ecr_replication_deploy_preflight", return_value="refused: why"),
        patch.object(manager, "_sync_lambda_sources") as sync,
        pytest.raises(replication.ReplicationConflictError, match="refused: why"),
    ):
        manager.deploy(stack_name="gco-global")
    sync.assert_not_called()


def test_destroy_refuses_then_cleans_up_after_a_global_delete(
    manager: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch.object(manager, "_ecr_replication_destroy_preflight", return_value="refused: why"),
        patch.object(manager, "_destroy") as destroy,
    ):
        assert manager.destroy(stack_name="gco-global", force=True) is False
    destroy.assert_not_called()
    assert "refused: why" in capsys.readouterr().out

    with (
        patch.object(manager, "_ecr_replication_destroy_preflight", return_value=None),
        patch.object(manager, "_destroy", return_value=True),
        patch.object(manager, "_remove_gco_replication_rules", return_value=1) as cleanup,
    ):
        assert manager.destroy(stack_name="gco-global", force=True) is True
    cleanup.assert_called_once_with()
    assert "other rules were kept" in capsys.readouterr().out

    for outcome, removed in ((False, 1), (True, 0)):
        with (
            patch.object(manager, "_ecr_replication_destroy_preflight", return_value=None),
            patch.object(manager, "_destroy", return_value=outcome),
            patch.object(manager, "_remove_gco_replication_rules", return_value=removed) as cleanup,
        ):
            assert manager.destroy(stack_name="gco-global", force=True) is outcome
        assert cleanup.called is outcome
        assert "other rules were kept" not in capsys.readouterr().out

    with (
        patch.object(manager, "_ecr_replication_destroy_preflight") as guard,
        patch.object(manager, "_destroy", return_value=True),
        patch.object(manager, "_remove_gco_replication_rules") as cleanup,
    ):
        assert manager.destroy(stack_name="gco-us-east-1", force=True) is True
    guard.assert_not_called()
    cleanup.assert_not_called()
