"""``gco stacks capabilities argocd bootstrap-identity``: Identity Center wiring for Argo CD.

* ``cli/argocd_identity.py`` — instance discovery across Regions (account and
  organization instances), account-instance creation with the ACTIVE wait and
  the operator-facing refusals, group ensure / member add / delete against
  in-memory ``sso-admin`` and ``identitystore`` fakes, and the bootstrap
  decision table (existing identities, org instance without write access,
  create-if-missing).
* ``cli/managed_config.py`` — the ``eks_capabilities.argocd`` writers
  (``set_argocd_identity_center``, ``ensure_argocd_role_mapping``) that put the
  result into cdk.json idempotently and validated.
* The Click command through ``CliRunner`` in table and JSON modes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli import argocd_identity as ident
from cli import managed_config
from cli.main import cli

_ACCOUNT = "123456789012"
_OTHER_ACCOUNT = "999999999999"
_REGION = "us-east-1"
_OTHER_REGION = "us-west-2"
_PROJECT = "gco"
_ARN = "arn:aws:sso:::instance/ssoins-1234567890abcdef"
_ORG_ARN = "arn:aws:sso:::instance/ssoins-org000000000001"
#: Captured before the autouse fixture below patches the module attribute.
_REAL_IDENTITY_CENTER_REGIONS = ident.identity_center_regions


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeSsoAdmin:
    """Per-Region instance registry; ``create_instance`` becomes ACTIVE after N describes."""

    def __init__(self, region: str, instances: list[dict[str, Any]], *, activation_polls: int = 1):
        self.region = region
        self.instances = instances
        self.activation_polls = activation_polls
        self._describe_calls = 0
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.create_error: str | None = None

    def get_paginator(self, operation: str) -> Any:
        assert operation == "list_instances"
        fake = self

        class _Paginator:
            def paginate(self) -> list[dict[str, Any]]:
                return [{"Instances": list(fake.instances)}]

        return _Paginator()

    def create_instance(self, **request: Any) -> dict[str, Any]:
        if self.create_error:
            raise _client_error(self.create_error, "CreateInstance")
        self.created.append(request)
        instance = {
            "InstanceArn": _ARN,
            "IdentityStoreId": "d-9067000000",
            "OwnerAccountId": _ACCOUNT,
            "Name": request["Name"],
            "Status": "CREATE_IN_PROGRESS",
        }
        self.instances.append(instance)
        return {"InstanceArn": _ARN}

    def describe_instance(self, *, InstanceArn: str) -> dict[str, Any]:
        self._describe_calls += 1
        for instance in self.instances:
            if instance["InstanceArn"] == InstanceArn:
                if self._describe_calls >= self.activation_polls:
                    instance["Status"] = "ACTIVE"
                return dict(instance)
        raise _client_error("ResourceNotFoundException", "DescribeInstance")

    def delete_instance(self, *, InstanceArn: str) -> dict[str, Any]:
        if not any(item["InstanceArn"] == InstanceArn for item in self.instances):
            raise _client_error("ResourceNotFoundException", "DeleteInstance")
        self.instances[:] = [item for item in self.instances if item["InstanceArn"] != InstanceArn]
        self.deleted.append(InstanceArn)
        return {}


class FakeIdentityStore:
    def __init__(self, *, users: dict[str, str] | None = None, deny_writes: bool = False):
        self.groups: dict[str, str] = {}  # display name -> id
        self.memberships: set[tuple[str, str]] = set()
        self.users = users or {}
        self.deny_writes = deny_writes
        self._counter = 0

    def get_group_id(self, *, IdentityStoreId: str, AlternateIdentifier: dict[str, Any]) -> dict:
        name = AlternateIdentifier["UniqueAttribute"]["AttributeValue"]
        assert AlternateIdentifier["UniqueAttribute"]["AttributePath"] == "displayName"
        if name not in self.groups:
            raise _client_error("ResourceNotFoundException", "GetGroupId")
        return {"GroupId": self.groups[name]}

    def create_group(self, *, IdentityStoreId: str, DisplayName: str, Description: str) -> dict:
        if self.deny_writes:
            raise _client_error("AccessDeniedException", "CreateGroup")
        if DisplayName in self.groups:
            raise _client_error("ConflictException", "CreateGroup")
        self._counter += 1
        group_id = f"g-{self._counter:04}"
        self.groups[DisplayName] = group_id
        return {"GroupId": group_id}

    def get_user_id(self, *, IdentityStoreId: str, AlternateIdentifier: dict[str, Any]) -> dict:
        name = AlternateIdentifier["UniqueAttribute"]["AttributeValue"]
        assert AlternateIdentifier["UniqueAttribute"]["AttributePath"] == "userName"
        if name not in self.users:
            raise _client_error("ResourceNotFoundException", "GetUserId")
        return {"UserId": self.users[name]}

    def create_group_membership(
        self, *, IdentityStoreId: str, GroupId: str, MemberId: dict
    ) -> dict:
        key = (GroupId, MemberId["UserId"])
        if key in self.memberships:
            raise _client_error("ConflictException", "CreateGroupMembership")
        self.memberships.add(key)
        return {"MembershipId": "m-1"}

    def delete_group(self, *, IdentityStoreId: str, GroupId: str) -> dict:
        for name, group_id in list(self.groups.items()):
            if group_id == GroupId:
                del self.groups[name]
                return {}
        raise _client_error("ResourceNotFoundException", "DeleteGroup")


def _account_instance(region_owner: str = _ACCOUNT, arn: str = _ARN) -> dict[str, Any]:
    return {
        "InstanceArn": arn,
        "IdentityStoreId": "d-9067000000",
        "OwnerAccountId": region_owner,
        "Name": "existing",
        "Status": "ACTIVE",
    }


class Fakes:
    """A client factory over per-Region sso-admin fakes and one identity store."""

    def __init__(self, sso: dict[str, FakeSsoAdmin], store: FakeIdentityStore | None = None):
        self.sso = sso
        self.store = store or FakeIdentityStore()
        self.calls: list[tuple[str, str]] = []

    def __call__(self, service: str, region: str) -> Any:
        self.calls.append((service, region))
        if service == "sso-admin":
            return self.sso.setdefault(region, FakeSsoAdmin(region, []))
        if service == "identitystore":
            return self.store
        raise AssertionError(service)


@pytest.fixture(autouse=True)
def _two_identity_center_regions() -> Any:
    with patch.object(ident, "identity_center_regions", return_value=[_REGION, _OTHER_REGION]):
        yield


# ─── discovery and creation ──────────────────────────────────────────────────


class TestDiscovery:
    def test_prefers_the_requested_region_then_sweeps(self) -> None:
        fakes = Fakes({_OTHER_REGION: FakeSsoAdmin(_OTHER_REGION, [_account_instance()])})
        found = ident.discover_instance(fakes, preferred_region=_REGION)
        assert found is not None and found.region == _OTHER_REGION
        assert found.identity_store_id == "d-9067000000"
        assert fakes.calls == [("sso-admin", _REGION), ("sso-admin", _OTHER_REGION)]

    def test_no_sweep_stays_in_the_preferred_region(self) -> None:
        fakes = Fakes({_OTHER_REGION: FakeSsoAdmin(_OTHER_REGION, [_account_instance()])})
        assert ident.discover_instance(fakes, preferred_region=_REGION, sweep_regions=False) is None

    def test_by_arn_ignores_other_instances(self) -> None:
        fakes = Fakes(
            {_REGION: FakeSsoAdmin(_REGION, [_account_instance(arn=_ORG_ARN), _account_instance()])}
        )
        found = ident.discover_instance(fakes, preferred_region=_REGION, instance_arn=_ARN)
        assert found is not None and found.instance_arn == _ARN
        # The same filter applies to what the sweep of the other Regions returns.
        swept = Fakes(
            {
                _OTHER_REGION: FakeSsoAdmin(
                    _OTHER_REGION, [_account_instance(arn=_ORG_ARN), _account_instance()]
                )
            }
        )
        found = ident.discover_instance(swept, preferred_region=_REGION, instance_arn=_ARN)
        assert found is not None and found.instance_arn == _ARN and found.region == _OTHER_REGION
        assert (
            ident.discover_instance(swept, preferred_region=_REGION, instance_arn="arn:x") is None
        )

    def test_unavailable_region_reads_as_none(self) -> None:
        class Broken(FakeSsoAdmin):
            def get_paginator(self, operation: str) -> Any:
                raise _client_error("AccessDeniedException", "ListInstances")

        fakes = Fakes({_REGION: Broken(_REGION, [])})
        assert ident.list_instances(fakes, _REGION) == []

    def test_a_region_that_never_answers_cannot_stall_the_sweep(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The first live run sat in a Region whose sso endpoint connect-timed
        out under the full retry budget. The sweep asks every other Region at
        once under one deadline; a Region still silent when it passes reads as
        holding no instance and is named in a warning, while the instances the
        answering Regions returned are still found."""
        import threading

        release = threading.Event()

        class Silent(FakeSsoAdmin):
            def get_paginator(self, operation: str) -> Any:
                release.wait(timeout=30)  # a hung connection, released by the test
                return super().get_paginator(operation)

        silent_region = "me-south-1"
        answering = FakeSsoAdmin(_OTHER_REGION, [_account_instance()])
        fakes = Fakes({silent_region: Silent(silent_region, []), _OTHER_REGION: answering})
        try:
            with (
                patch.object(
                    ident,
                    "identity_center_regions",
                    return_value=[_REGION, silent_region, _OTHER_REGION],
                ),
                caplog.at_level("WARNING", logger=ident.__name__),
            ):
                started = time.monotonic()
                found = ident.discover_instance(
                    fakes, preferred_region=_REGION, sweep_timeout_seconds=0.5
                )
            assert time.monotonic() - started < 5, "the deadline must bound the sweep"
            assert found is not None and found.region == _OTHER_REGION
            # Clients are built on the calling thread, preferred Region first.
            assert fakes.calls == [
                ("sso-admin", _REGION),
                ("sso-admin", silent_region),
                ("sso-admin", _OTHER_REGION),
            ]
            assert any(
                silent_region in record.getMessage() and "did not answer" in record.getMessage()
                for record in caplog.records
            ), caplog.text
        finally:
            release.set()

    def test_sweep_reports_the_unanswered_regions_and_keeps_region_order(self) -> None:
        import threading

        release = threading.Event()

        class Silent(FakeSsoAdmin):
            def get_paginator(self, operation: str) -> Any:
                release.wait(timeout=30)
                return super().get_paginator(operation)

        first = FakeSsoAdmin("eu-west-1", [_account_instance(arn=_ORG_ARN)])
        second = FakeSsoAdmin(_OTHER_REGION, [_account_instance()])
        fakes = Fakes(
            {"eu-west-1": first, "ap-south-1": Silent("ap-south-1", []), _OTHER_REGION: second}
        )
        try:
            instances, unanswered = ident.sweep_instances(
                fakes, ["eu-west-1", "ap-south-1", _OTHER_REGION], timeout_seconds=0.5
            )
        finally:
            release.set()
        assert [instance.instance_arn for instance in instances] == [_ORG_ARN, _ARN]
        assert unanswered == ["ap-south-1"]
        # Every Region answering means nothing is reported unanswered.
        instances, unanswered = ident.sweep_instances(fakes, ["eu-west-1", _OTHER_REGION])
        assert len(instances) == 2 and unanswered == []
        assert ident.sweep_instances(fakes, []) == ([], [])

    def test_create_account_instance_waits_for_active(self) -> None:
        sso = FakeSsoAdmin(_REGION, [], activation_polls=3)
        fakes = Fakes({_REGION: sso})
        sleeps: list[float] = []
        instance = ident.create_account_instance(
            fakes,
            region=_REGION,
            name="gco-identity-center",
            tags={"gco:project": "gco"},
            sleep=sleeps.append,
        )
        assert instance.status == "ACTIVE" and instance.instance_arn == _ARN
        assert sso.created[0]["Name"] == "gco-identity-center"
        assert sso.created[0]["Tags"] == [{"Key": "gco:project", "Value": "gco"}]
        assert "ClientToken" in sso.created[0]
        assert len(sleeps) == 2

    @pytest.mark.parametrize(
        "code", ["AccessDeniedException", "ValidationException", "ConflictException"]
    )
    def test_create_refusals_become_guidance(self, code: str) -> None:
        sso = FakeSsoAdmin(_REGION, [])
        sso.create_error = code
        with pytest.raises(
            ident.ArgoCdIdentityError, match="refused to create an account instance"
        ):
            ident.create_account_instance(Fakes({_REGION: sso}), region=_REGION, name="x")

    def test_create_times_out(self) -> None:
        sso = FakeSsoAdmin(_REGION, [], activation_polls=999)
        with (
            patch.object(ident.time, "monotonic", side_effect=[0.0, 1000.0, 2000.0]),
            pytest.raises(ident.ArgoCdIdentityError, match="did not become ACTIVE"),
        ):
            ident.create_account_instance(
                Fakes({_REGION: sso}), region=_REGION, name="x", sleep=lambda _s: None
            )

    def test_delete_account_instance(self) -> None:
        sso = FakeSsoAdmin(_REGION, [_account_instance()])
        fakes = Fakes({_REGION: sso})
        assert ident.delete_account_instance(fakes, _REGION, _ARN) is True
        assert ident.delete_account_instance(fakes, _REGION, _ARN) is False


class TestGroups:
    def test_ensure_group_creates_then_reuses(self) -> None:
        store = FakeIdentityStore()
        first = ident.ensure_group(
            store, identity_store_id="d-1", display_name="gco-argocd-admins", description="d"
        )
        second = ident.ensure_group(
            store, identity_store_id="d-1", display_name="gco-argocd-admins", description="d"
        )
        assert first == ("g-0001", True)
        assert second == ("g-0001", False)

    def test_ensure_group_denied_explains_org_instances(self) -> None:
        store = FakeIdentityStore(deny_writes=True)
        with pytest.raises(
            ident.ArgoCdIdentityError, match="only be written in an instance this account owns"
        ):
            ident.ensure_group(store, identity_store_id="d-1", display_name="g", description="d")

    def test_members_added_once_and_unknown_users_named(self) -> None:
        store = FakeIdentityStore(users={"alice": "u-alice", "bob": "u-bob"})
        group_id, _ = ident.ensure_group(
            store, identity_store_id="d-1", display_name="g", description="d"
        )
        assert ident.add_group_members(
            store, identity_store_id="d-1", group_id=group_id, user_names=("alice", "bob")
        ) == (("alice", "bob"), ())
        assert ident.add_group_members(
            store, identity_store_id="d-1", group_id=group_id, user_names=("alice",)
        ) == ((), ("alice",))
        with pytest.raises(ident.ArgoCdIdentityError, match="user 'carol' does not exist"):
            ident.add_group_members(
                store, identity_store_id="d-1", group_id=group_id, user_names=("carol",)
            )

    def test_delete_group(self) -> None:
        store = FakeIdentityStore()
        group_id, _ = ident.ensure_group(
            store, identity_store_id="d-1", display_name="g", description="d"
        )
        assert ident.delete_group(store, identity_store_id="d-1", group_id=group_id) is True
        assert ident.delete_group(store, identity_store_id="d-1", group_id=group_id) is False

    def test_ensure_group_conflict_from_a_racing_creator_resolves_to_the_existing_group(
        self,
    ) -> None:
        """A ConflictException whose group can then be looked up is a reuse, not an error."""

        class Racing(FakeIdentityStore):
            def __init__(self) -> None:
                super().__init__()
                self.lookups = 0

            def get_group_id(self, **kwargs: Any) -> dict:
                self.lookups += 1
                if self.lookups == 1:
                    # Not there yet when we look ...
                    raise _client_error("ResourceNotFoundException", "GetGroupId")
                return {"GroupId": "g-raced"}

            def create_group(self, **kwargs: Any) -> dict:
                # ... but someone else created it between the lookup and the create.
                raise _client_error("ConflictException", "CreateGroup")

        assert ident.ensure_group(
            Racing(), identity_store_id="d-1", display_name="g", description="d"
        ) == ("g-raced", False)

    def test_ensure_group_conflict_that_never_resolves_propagates(self) -> None:
        class Ghost(FakeIdentityStore):
            def get_group_id(self, **kwargs: Any) -> dict:
                raise _client_error("ResourceNotFoundException", "GetGroupId")

            def create_group(self, **kwargs: Any) -> dict:
                raise _client_error("ConflictException", "CreateGroup")

        with pytest.raises(ClientError, match="ConflictException"):
            ident.ensure_group(Ghost(), identity_store_id="d-1", display_name="g", description="d")


class TestUnexpectedErrorsPropagate:
    """Only the documented refusal codes are translated; anything else surfaces as-is.

    Each helper narrows on one or two error codes (``ResourceNotFoundException``,
    ``ConflictException``, ``AccessDeniedException``); a throttle or an internal
    error must not be mistaken for "already gone" or "already present".
    """

    def test_client_error_code_reads_only_botocore_shaped_errors(self) -> None:
        assert ident._client_error_code(_client_error("ThrottlingException", "Op")) == (
            "ThrottlingException"
        )
        assert ident._client_error_code(RuntimeError("no response attribute")) == ""

    def test_create_account_instance_other_errors(self) -> None:
        sso = FakeSsoAdmin(_REGION, [])
        sso.create_error = "ThrottlingException"
        with pytest.raises(ClientError, match="ThrottlingException"):
            ident.create_account_instance(Fakes({_REGION: sso}), region=_REGION, name="x")

    def test_delete_account_instance_other_errors(self) -> None:
        class Throttled(FakeSsoAdmin):
            def delete_instance(self, **kwargs: Any) -> dict[str, Any]:
                raise _client_error("ThrottlingException", "DeleteInstance")

        with pytest.raises(ClientError, match="ThrottlingException"):
            ident.delete_account_instance(Fakes({_REGION: Throttled(_REGION, [])}), _REGION, _ARN)

    def test_group_lookup_other_errors(self) -> None:
        class Throttled(FakeIdentityStore):
            def get_group_id(self, **kwargs: Any) -> dict:
                raise _client_error("ThrottlingException", "GetGroupId")

        with pytest.raises(ClientError, match="ThrottlingException"):
            ident.find_group_id(Throttled(), "d-1", "g")

    def test_user_lookup_other_errors(self) -> None:
        class Throttled(FakeIdentityStore):
            def get_user_id(self, **kwargs: Any) -> dict:
                raise _client_error("ThrottlingException", "GetUserId")

        with pytest.raises(ClientError, match="ThrottlingException"):
            ident.find_user_id(Throttled(), "d-1", "alice")

    def test_membership_other_errors(self) -> None:
        class Throttled(FakeIdentityStore):
            def create_group_membership(self, **kwargs: Any) -> dict:
                raise _client_error("ThrottlingException", "CreateGroupMembership")

        store = Throttled(users={"alice": "u-alice"})
        with pytest.raises(ClientError, match="ThrottlingException"):
            ident.add_group_members(
                store, identity_store_id="d-1", group_id="g-1", user_names=("alice",)
            )

    def test_delete_group_other_errors(self) -> None:
        class Throttled(FakeIdentityStore):
            def delete_group(self, **kwargs: Any) -> dict:
                raise _client_error("ThrottlingException", "DeleteGroup")

        with pytest.raises(ClientError, match="ThrottlingException"):
            ident.delete_group(Throttled(), identity_store_id="d-1", group_id="g-1")


class TestDefaultWiring:
    """The boto3-backed defaults: a plain client factory and the Region list."""

    def test_default_client_factory_builds_a_regional_boto3_client(self) -> None:
        import boto3

        with patch.object(boto3, "client", return_value="client") as client:
            assert ident.default_client_factory("sso-admin", _OTHER_REGION) == "client"
        client.assert_called_once_with("sso-admin", region_name=_OTHER_REGION)

    def test_identity_center_regions_come_from_the_partition_endpoint_list(self) -> None:
        """The sweep list is the partition's sso-admin endpoints, de-duplicated and sorted."""
        import boto3

        class FakeSession:
            def get_partition_for_region(self, region: str) -> str:
                assert region == "cn-north-1"
                return "aws-cn"

            def get_available_regions(self, service: str, partition_name: str) -> list[str]:
                assert (service, partition_name) == ("sso-admin", "aws-cn")
                return ["cn-northwest-1", "cn-north-1", "cn-north-1"]

        # The autouse fixture patches ident.identity_center_regions for every
        # test; the module-level alias captured at import time is the real one.
        with patch.object(boto3.session, "Session", FakeSession):
            regions = _REAL_IDENTITY_CENTER_REGIONS(Fakes({}), "cn-north-1")
        assert regions == ["cn-north-1", "cn-northwest-1"]


# ─── the bootstrap decision table ────────────────────────────────────────────


class TestBootstrap:
    def _bootstrap(self, fakes: Fakes, **overrides: Any) -> ident.BootstrapResult:
        kwargs: dict[str, Any] = {
            "account_id": _ACCOUNT,
            "project_name": _PROJECT,
            "preferred_region": _REGION,
            "client_factory": fakes,
            "sleep": lambda _s: None,
        }
        kwargs.update(overrides)
        return ident.bootstrap_argocd_identity(**kwargs)

    def test_existing_account_instance_gets_a_mapped_group(self) -> None:
        fakes = Fakes(
            {_REGION: FakeSsoAdmin(_REGION, [_account_instance()])},
            FakeIdentityStore(users={"alice": "u-alice"}),
        )
        result = self._bootstrap(fakes, user_names=("alice",))
        assert not result.instance_created
        assert result.group_name == "gco-argocd-admins" and result.group_created
        assert result.members_added == ("alice",)
        assert result.role_mapping() == {
            "role": "ADMIN",
            "identities": [{"id": result.group_id, "type": "SSO_GROUP"}],
        }
        assert result.cdk_json_fragment() == {
            "idc_instance_arn": _ARN,
            "idc_region": _REGION,
            "rbac_role_mappings": [result.role_mapping()],
        }
        json.dumps(result.to_dict())

    def test_no_instance_without_opt_in_stops_with_guidance(self) -> None:
        with pytest.raises(ident.ArgoCdIdentityError, match="--create-account-instance"):
            self._bootstrap(Fakes({}))

    def test_no_instance_with_opt_in_creates_one(self) -> None:
        sso = FakeSsoAdmin(_REGION, [])
        fakes = Fakes({_REGION: sso})
        result = self._bootstrap(
            fakes, create_account_instance_if_missing=True, instance_tags={"gco:project": _PROJECT}
        )
        assert result.instance_created and result.instance.instance_arn == _ARN
        assert sso.created[0]["Name"] == "gco-identity-center"
        assert result.group_created and result.identities[0]["type"] == "SSO_GROUP"

    def test_explicit_identities_skip_group_creation_even_on_an_org_instance(self) -> None:
        fakes = Fakes(
            {_REGION: FakeSsoAdmin(_REGION, [_account_instance(_OTHER_ACCOUNT, _ORG_ARN)])}
        )
        result = self._bootstrap(
            fakes, identities=({"id": "g-org", "type": "SSO_GROUP"},), role="VIEWER"
        )
        assert result.group_id is None and not result.group_created
        assert result.identities == ({"id": "g-org", "type": "SSO_GROUP"},)
        assert result.role == "VIEWER"
        assert ("identitystore", _REGION) not in fakes.calls

    def test_org_instance_without_identities_is_refused(self) -> None:
        fakes = Fakes(
            {_REGION: FakeSsoAdmin(_REGION, [_account_instance(_OTHER_ACCOUNT, _ORG_ARN)])}
        )
        with pytest.raises(ident.ArgoCdIdentityError, match="belongs to account 999999999999"):
            self._bootstrap(fakes)

    def test_unknown_instance_arn(self) -> None:
        fakes = Fakes({_REGION: FakeSsoAdmin(_REGION, [_account_instance()])})
        with pytest.raises(ident.ArgoCdIdentityError, match="was not found from this account"):
            self._bootstrap(fakes, instance_arn=_ORG_ARN)

    def test_bad_role(self) -> None:
        with pytest.raises(ident.ArgoCdIdentityError, match="role must be one of"):
            self._bootstrap(Fakes({}), role="ROOT")


# ─── cdk.json writers ────────────────────────────────────────────────────────


class TestManagedConfigWriters:
    @pytest.fixture
    def cdk_json(self, tmp_path: Path) -> Path:
        path = tmp_path / "cdk.json"
        path.write_text(
            json.dumps({"context": {"deployment_regions": {"regional": [_REGION]}}}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_identity_center_scalars(self, cdk_json: Path) -> None:
        arn_report, region_report = managed_config.set_argocd_identity_center(
            _ARN, _REGION, config_path=cdk_json
        )
        assert arn_report.changed and region_report.changed
        again = managed_config.set_argocd_identity_center(_ARN, _REGION, config_path=cdk_json)
        assert not again[0].changed and not again[1].changed
        argocd = json.loads(cdk_json.read_text())["context"]["eks_capabilities"]["argocd"]
        assert argocd == {"idc_instance_arn": _ARN, "idc_region": _REGION}
        with pytest.raises(
            managed_config.ManagedConfigError, match="not an IAM Identity Center instance ARN"
        ):
            managed_config.set_argocd_identity_center(
                "arn:aws:iam::1:role/x", _REGION, config_path=cdk_json
            )
        with pytest.raises(managed_config.ManagedConfigError, match="not an AWS Region name"):
            managed_config.set_argocd_identity_center(_ARN, "nowhere", config_path=cdk_json)

    def test_role_mapping_is_idempotent_and_role_scoped(self, cdk_json: Path) -> None:
        assert managed_config.ensure_argocd_role_mapping(
            "ADMIN", "g-1", "SSO_GROUP", config_path=cdk_json
        ).changed
        assert not managed_config.ensure_argocd_role_mapping(
            "ADMIN", "g-1", "SSO_GROUP", config_path=cdk_json
        ).changed
        assert managed_config.ensure_argocd_role_mapping(
            "ADMIN", "u-2", "SSO_USER", config_path=cdk_json
        ).changed
        assert managed_config.ensure_argocd_role_mapping(
            "VIEWER", "g-3", "SSO_GROUP", config_path=cdk_json
        ).changed
        mappings = json.loads(cdk_json.read_text())["context"]["eks_capabilities"]["argocd"][
            "rbac_role_mappings"
        ]
        assert mappings == [
            {
                "role": "ADMIN",
                "identities": [
                    {"id": "g-1", "type": "SSO_GROUP"},
                    {"id": "u-2", "type": "SSO_USER"},
                ],
            },
            {"role": "VIEWER", "identities": [{"id": "g-3", "type": "SSO_GROUP"}]},
        ]

    def test_role_mapping_rejects_bad_inputs_and_bad_documents(self, cdk_json: Path) -> None:
        with pytest.raises(managed_config.ManagedConfigError, match="role must be one of"):
            managed_config.ensure_argocd_role_mapping(
                "ROOT", "g", "SSO_GROUP", config_path=cdk_json
            )
        with pytest.raises(managed_config.ManagedConfigError, match="identity type must be one of"):
            managed_config.ensure_argocd_role_mapping("ADMIN", "g", "GROUP", config_path=cdk_json)
        with pytest.raises(
            managed_config.ManagedConfigError, match="identity id must not be empty"
        ):
            managed_config.ensure_argocd_role_mapping(
                "ADMIN", " ", "SSO_GROUP", config_path=cdk_json
            )
        cdk_json.write_text(
            json.dumps({"context": {"eks_capabilities": {"argocd": {"rbac_role_mappings": "x"}}}})
        )
        with pytest.raises(managed_config.ManagedConfigError, match="must be a JSON array"):
            managed_config.ensure_argocd_role_mapping(
                "ADMIN", "g", "SSO_GROUP", config_path=cdk_json
            )

    @pytest.mark.parametrize(
        ("capabilities", "message"),
        [
            ("nope", "context.eks_capabilities must be a JSON object"),
            ({"argocd": []}, r"context\.eks_capabilities\.argocd must be a JSON object"),
            (
                {"argocd": {"rbac_role_mappings": [{"role": "ADMIN", "identities": "u-1"}]}},
                "identities of role ADMIN must be a JSON array",
            ),
            (
                # A hand-edited mapping for another role that synth would reject:
                # the writer refuses rather than committing an invalid block.
                {"argocd": {"rbac_role_mappings": [{"role": "OWNER", "identities": []}]}},
                r"refusing to update eks_capabilities\.argocd\.rbac_role_mappings",
            ),
        ],
    )
    def test_role_mapping_refuses_hand_edited_shapes_it_cannot_extend(
        self, cdk_json: Path, capabilities: Any, message: str
    ) -> None:
        cdk_json.write_text(json.dumps({"context": {"eks_capabilities": capabilities}}) + "\n")
        before = cdk_json.read_text()
        with pytest.raises(managed_config.ManagedConfigError, match=message):
            managed_config.ensure_argocd_role_mapping(
                "ADMIN", "g-1", "SSO_GROUP", config_path=cdk_json
            )
        assert cdk_json.read_text() == before


# ─── the Click command ───────────────────────────────────────────────────────


class _Sts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": _ACCOUNT}


class TestBootstrapIdentityCommand:
    def _invoke(self, args: list[str], fakes: Fakes) -> Any:
        with (
            # bootstrap_argocd_identity resolves the factory at call time.
            patch("cli.argocd_identity.default_client_factory", fakes),
            patch("cli.commands.stacks_cmd._project_name", return_value=_PROJECT),
            patch("cli.commands.stacks_cmd._load_cdk_json", return_value={"regional": [_REGION]}),
            patch("boto3.client", return_value=_Sts()),
        ):
            return CliRunner().invoke(cli, args)

    def test_help(self) -> None:
        result = CliRunner().invoke(
            cli, ["stacks", "capabilities", "argocd", "bootstrap-identity", "--help"]
        )
        assert result.exit_code == 0
        assert "--create-account-instance" in result.output
        assert "--write-cdk-json" in result.output

    def test_prints_the_fragment_without_writing(self) -> None:
        fakes = Fakes(
            {_REGION: FakeSsoAdmin(_REGION, [_account_instance()])},
            FakeIdentityStore(users={"alice": "u-alice"}),
        )
        result = self._invoke(
            ["stacks", "capabilities", "argocd", "bootstrap-identity", "--user", "alice"], fakes
        )
        assert result.exit_code == 0, result.output
        assert f"Using Identity Center instance {_ARN}" in result.output
        assert "Created group gco-argocd-admins" in result.output
        assert "Added alice" in result.output
        assert '"idc_instance_arn"' in result.output
        assert "Identity Center password" in result.output

    def test_json_document(self) -> None:
        fakes = Fakes({_REGION: FakeSsoAdmin(_REGION, [_account_instance()])})
        result = self._invoke(
            ["--output", "json", "stacks", "capabilities", "argocd", "bootstrap-identity"], fakes
        )
        assert result.exit_code == 0, result.output
        document = json.loads(result.output)
        assert document["instance"]["instance_arn"] == _ARN
        assert document["group_name"] == "gco-argocd-admins"
        assert document["role_mapping"]["role"] == "ADMIN"
        assert document["cdk_json_written"] is False
        assert document["region"] == _REGION

    def test_writes_cdk_json(self, tmp_path: Path) -> None:
        cdk_json = tmp_path / "cdk.json"
        cdk_json.write_text(json.dumps({"context": {}}) + "\n")
        fakes = Fakes({_REGION: FakeSsoAdmin(_REGION, [_account_instance()])})
        result = self._invoke(
            [
                "stacks",
                "capabilities",
                "argocd",
                "bootstrap-identity",
                "--write-cdk-json",
                "--config-path",
                str(cdk_json),
                "-y",
            ],
            fakes,
        )
        assert result.exit_code == 0, result.output
        argocd = json.loads(cdk_json.read_text())["context"]["eks_capabilities"]["argocd"]
        assert argocd["idc_instance_arn"] == _ARN
        assert argocd["idc_region"] == _REGION
        assert argocd["rbac_role_mappings"] == [
            {"role": "ADMIN", "identities": [{"id": "g-0001", "type": "SSO_GROUP"}]}
        ]
        assert "cdk.json updated" in result.output

    def test_create_account_instance_confirms_unless_yes(self) -> None:
        sso = FakeSsoAdmin(_REGION, [])
        result = self._invoke(
            ["stacks", "capabilities", "argocd", "bootstrap-identity", "--create-account-instance"],
            Fakes({_REGION: sso}),
        )
        assert result.exit_code != 0  # confirmation aborted (no input)
        assert sso.created == []
        result = self._invoke(
            [
                "stacks",
                "capabilities",
                "argocd",
                "bootstrap-identity",
                "--create-account-instance",
                "-y",
            ],
            Fakes({_REGION: sso}),
        )
        assert result.exit_code == 0, result.output
        assert sso.created[0]["Name"] == "gco-identity-center"
        assert f"Created Identity Center instance {_ARN}" in result.output

    def test_existing_identities_bypass_group_creation(self) -> None:
        fakes = Fakes(
            {_REGION: FakeSsoAdmin(_REGION, [_account_instance(_OTHER_ACCOUNT, _ORG_ARN)])}
        )
        result = self._invoke(
            [
                "stacks",
                "capabilities",
                "argocd",
                "bootstrap-identity",
                "--identity",
                "SSO_GROUP:g-org",
                "--role",
                "VIEWER",
            ],
            fakes,
        )
        assert result.exit_code == 0, result.output
        assert "Mapping 1 existing identity to Argo CD VIEWER" in result.output

    def test_bad_identity_and_conflicting_options(self) -> None:
        fakes = Fakes({})
        result = self._invoke(
            ["stacks", "capabilities", "argocd", "bootstrap-identity", "--identity", "group:g"],
            fakes,
        )
        assert result.exit_code == 1 and "expects SSO_USER:<id> or SSO_GROUP:<id>" in result.output
        result = self._invoke(
            [
                "stacks",
                "capabilities",
                "argocd",
                "bootstrap-identity",
                "--identity",
                "SSO_USER:u",
                "--user",
                "alice",
            ],
            fakes,
        )
        assert result.exit_code == 1 and "drop --user/--group" in result.output

    def test_missing_instance_is_reported(self) -> None:
        result = self._invoke(["stacks", "capabilities", "argocd", "bootstrap-identity"], Fakes({}))
        assert result.exit_code == 1
        assert "--create-account-instance" in result.output

    def test_members_already_in_the_group_are_reported_as_such(self) -> None:
        store = FakeIdentityStore(users={"alice": "u-alice", "bob": "u-bob"})
        fakes = Fakes({_REGION: FakeSsoAdmin(_REGION, [_account_instance()])}, store)
        args = ["stacks", "capabilities", "argocd", "bootstrap-identity", "--user", "alice"]
        assert self._invoke(args, fakes).exit_code == 0
        result = self._invoke([*args, "--user", "bob"], fakes)
        assert result.exit_code == 0, result.output
        assert "Using group gco-argocd-admins" in result.output
        assert "alice was already in gco-argocd-admins" in result.output
        assert "Added bob to gco-argocd-admins" in result.output

    def test_unexpected_aws_errors_are_reported_not_traced(self) -> None:
        class Throttled(FakeSsoAdmin):
            def get_paginator(self, operation: str) -> Any:
                raise RuntimeError("Endpoint request timed out")

        result = self._invoke(
            ["stacks", "capabilities", "argocd", "bootstrap-identity"],
            Fakes({_REGION: Throttled(_REGION, [])}),
        )
        assert result.exit_code == 1
        assert "Identity Center bootstrap failed: Endpoint request timed out" in result.output
        assert "Traceback" not in result.output

    def test_write_cdk_json_refusal_exits_nonzero(self, tmp_path: Path) -> None:
        """A cdk.json the managed-config engine cannot update fails the command, after AWS."""
        cdk_json = tmp_path / "cdk.json"
        cdk_json.write_text(json.dumps({"context": {"eks_capabilities": "not-an-object"}}) + "\n")
        fakes = Fakes({_REGION: FakeSsoAdmin(_REGION, [_account_instance()])})
        result = self._invoke(
            [
                "stacks",
                "capabilities",
                "argocd",
                "bootstrap-identity",
                "--write-cdk-json",
                "--config-path",
                str(cdk_json),
            ],
            fakes,
        )
        assert result.exit_code == 1
        assert "context.eks_capabilities must be a JSON object" in result.output
        # The group was still ensured: the refusal is about the file, not AWS.
        assert fakes.store.groups == {"gco-argocd-admins": "g-0001"}
