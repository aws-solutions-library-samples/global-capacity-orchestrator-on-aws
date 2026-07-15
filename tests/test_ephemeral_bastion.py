"""Tests for the ephemeral SSM bastion lifecycle (cli/ephemeral_bastion.py).

The pure argv builders and parsers are the security-relevant seam (list form,
validated inputs, no shell), so they are tested directly. The runtime wrappers
are exercised with the AWS CLI shell-out mocked at the ``_run_aws`` /
``subprocess.run`` seam so nothing touches AWS. The orphan safeguards baked into
``build_run_instances_command`` (IMDSv2, shutdown-behaviour terminate, TTL
user-data, the ``gco:ephemeral`` tag set) are asserted explicitly — they are the
contract that keeps a forgotten teardown from becoming a paid orphan.
"""

from __future__ import annotations

import json

import pytest

from cli import ephemeral_bastion as eb

# ---------------------------------------------------------------------------
# Validators + user-data
# ---------------------------------------------------------------------------


class TestValidators:
    def test_validate_accepts_good_value(self) -> None:
        assert eb._validate("us-east-1", eb._REGION_RE, "region") == "us-east-1"

    @pytest.mark.parametrize(
        "value,pattern",
        [
            ("not_a_region", eb._REGION_RE),
            ("i-xyz", eb._INSTANCE_RE),
            ("vpc-nothex", eb._VPC_RE),
            ("subnet-!!", eb._SUBNET_RE),
            ("sg-", eb._SG_RE),
            ("ami-zzzz", eb._AMI_RE),
        ],
    )
    def test_validate_rejects_bad_value(self, value: str, pattern: object) -> None:
        with pytest.raises(ValueError):
            eb._validate(value, pattern, "thing")  # type: ignore[arg-type]

    def test_validate_ttl_bounds(self) -> None:
        assert eb._validate_ttl(120) == 120
        assert eb._validate_ttl(5) == 5
        assert eb._validate_ttl(1440) == 1440

    @pytest.mark.parametrize("bad", [4, 0, -1, 1441, "abc"])
    def test_validate_ttl_rejects(self, bad: object) -> None:
        with pytest.raises(ValueError):
            eb._validate_ttl(bad)  # type: ignore[arg-type]

    def test_render_user_data_schedules_shutdown(self) -> None:
        script = eb.render_user_data(90)
        assert script.startswith("#!/bin/bash")
        assert "shutdown -h +90" in script

    def test_render_user_data_validates_ttl(self) -> None:
        with pytest.raises(ValueError):
            eb.render_user_data(3)


# ---------------------------------------------------------------------------
# Pure argv builders
# ---------------------------------------------------------------------------


class TestBuilders:
    def test_get_ami_command(self) -> None:
        cmd = eb.build_get_ami_command("us-east-1")
        assert cmd[:3] == ["aws", "ssm", "get-parameter"]
        assert eb.AL2023_AMI_SSM_PARAMETER in cmd
        assert cmd[-2:] == ["--output", "text"]

    def test_describe_cluster_network_command(self) -> None:
        cmd = eb.build_describe_cluster_network_command("gco-us-east-1", "us-east-1")
        assert cmd[:3] == ["aws", "eks", "describe-cluster"]
        assert "gco-us-east-1" in cmd
        query = cmd[cmd.index("--query") + 1]
        assert "vpcId" in query and "clusterSecurityGroupId" in query and "subnetIds" in query

    def test_describe_private_cluster_subnet_command(self) -> None:
        cmd = eb.build_describe_private_cluster_subnet_command(
            ["subnet-0123456789abcdef0"], "us-east-1"
        )
        assert cmd[:3] == ["aws", "ec2", "describe-subnets"]
        assert "subnet-0123456789abcdef0" in cmd
        assert "Name=map-public-ip-on-launch,Values=false" in cmd

    def test_create_role_command_has_trust_and_tags(self) -> None:
        cmd = eb.build_create_role_command()
        assert cmd[:3] == ["aws", "iam", "create-role"]
        assert eb.BASTION_ROLE_NAME in cmd
        doc = json.loads(cmd[cmd.index("--assume-role-policy-document") + 1])
        assert doc == eb.BASTION_TRUST_POLICY
        assert doc["Statement"][0]["Principal"]["Service"] == "ec2.amazonaws.com"

    def test_attach_role_policy_command(self) -> None:
        cmd = eb.build_attach_role_policy_command()
        assert cmd[:3] == ["aws", "iam", "attach-role-policy"]
        assert eb.SSM_MANAGED_POLICY_ARN in cmd

    def test_instance_profile_commands(self) -> None:
        create = eb.build_create_instance_profile_command()
        assert create[:3] == ["aws", "iam", "create-instance-profile"]
        assert eb.BASTION_PROFILE_NAME in create
        add = eb.build_add_role_to_profile_command()
        assert add[:3] == ["aws", "iam", "add-role-to-instance-profile"]
        assert eb.BASTION_ROLE_NAME in add and eb.BASTION_PROFILE_NAME in add

    def test_run_instances_command_carries_all_safeguards(self) -> None:
        cmd = eb.build_run_instances_command(
            ami_id="ami-0123456789abcdef0",
            instance_type="t3.micro",
            subnet_id="subnet-0123456789abcdef0",
            security_group_id="sg-0123456789abcdef0",
            profile_name=eb.BASTION_PROFILE_NAME,
            region="us-east-1",
            user_data=eb.render_user_data(120),
            ttl_minutes=120,
        )
        assert cmd[:3] == ["aws", "ec2", "run-instances"]
        # IMDSv2 required.
        assert (
            cmd[cmd.index("--metadata-options") + 1] == "HttpTokens=required,HttpEndpoint=enabled"
        )
        # Shutdown terminates the instance.
        assert cmd[cmd.index("--instance-initiated-shutdown-behavior") + 1] == "terminate"
        # TTL user-data backstop.
        assert "shutdown -h +120" in cmd[cmd.index("--user-data") + 1]
        # Greppable ephemeral tags.
        tagspec = cmd[cmd.index("--tag-specifications") + 1]
        assert "ResourceType=instance" in tagspec
        assert f"Key={eb.TAG_EPHEMERAL_KEY},Value=true" in tagspec
        assert f"Key={eb.TAG_PURPOSE_KEY},Value={eb.BASTION_PURPOSE}" in tagspec
        assert f"Key={eb.TAG_PROJECT_KEY},Value={eb.DEFAULT_PROJECT_NAME}" in tagspec
        assert f"Key={eb.TAG_TTL_KEY},Value=120" in tagspec
        # Network placement.
        assert cmd[cmd.index("--iam-instance-profile") + 1] == f"Name={eb.BASTION_PROFILE_NAME}"
        assert "subnet-0123456789abcdef0" in cmd
        assert "sg-0123456789abcdef0" in cmd
        assert "--no-associate-public-ip-address" in cmd
        assert "--associate-public-ip-address" not in cmd
        assert cmd[-2:] == ["--output", "text"]

    def test_run_instances_no_public_ip_variant(self) -> None:
        cmd = eb.build_run_instances_command(
            ami_id="ami-0123456789abcdef0",
            instance_type="t3.micro",
            subnet_id="subnet-0123456789abcdef0",
            security_group_id="sg-0123456789abcdef0",
            profile_name=eb.BASTION_PROFILE_NAME,
            region="us-east-1",
            user_data="x",
        )
        assert "--no-associate-public-ip-address" in cmd
        assert "--associate-public-ip-address" not in cmd

    def test_run_instances_rejects_bad_inputs(self) -> None:
        with pytest.raises(ValueError):
            eb.build_run_instances_command(
                ami_id="not-an-ami",
                instance_type="t3.micro",
                subnet_id="subnet-0123456789abcdef0",
                security_group_id="sg-0123456789abcdef0",
                profile_name=eb.BASTION_PROFILE_NAME,
                region="us-east-1",
                user_data="x",
            )
        with pytest.raises(ValueError):
            eb.build_run_instances_command(
                ami_id="ami-0123456789abcdef0",
                instance_type="bogus_type",
                subnet_id="subnet-0123456789abcdef0",
                security_group_id="sg-0123456789abcdef0",
                profile_name=eb.BASTION_PROFILE_NAME,
                region="us-east-1",
                user_data="x",
            )

    def test_describe_ssm_ping_command(self) -> None:
        cmd = eb.build_describe_ssm_ping_command("i-0123456789abcdef0", "us-east-1")
        assert cmd[:3] == ["aws", "ssm", "describe-instance-information"]
        assert "Key=InstanceIds,Values=i-0123456789abcdef0" in cmd
        assert "InstanceInformationList[0].PingStatus" in cmd

    def test_terminate_instances_command(self) -> None:
        cmd = eb.build_terminate_instances_command("i-0123456789abcdef0", "us-east-1")
        assert cmd[:3] == ["aws", "ec2", "terminate-instances"]
        assert "i-0123456789abcdef0" in cmd

    def test_iam_teardown_order(self) -> None:
        steps = eb.build_iam_teardown_commands()
        verbs = [s[2] for s in steps]
        # Profile disassociated + deleted before the role's policy is detached
        # and the role deleted.
        assert verbs == [
            "remove-role-from-instance-profile",
            "delete-instance-profile",
            "detach-role-policy",
            "delete-role",
        ]


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


class TestParsers:
    def test_parse_cluster_network_ok(self) -> None:
        out = json.dumps(
            {"vpc": "vpc-0123456789abcdef0", "sg": "sg-0123456789abcdef0", "subnets": ["subnet-a"]}
        )
        vpc, sg, subnets = eb.parse_cluster_network(out)
        assert vpc == "vpc-0123456789abcdef0"
        assert sg == "sg-0123456789abcdef0"
        assert subnets == ["subnet-a"]

    def test_parse_cluster_network_missing_raises(self) -> None:
        with pytest.raises(RuntimeError):
            eb.parse_cluster_network(json.dumps({"vpc": "", "sg": "", "subnets": []}))

    def test_clean_scalar(self) -> None:
        assert eb._clean_scalar("  x \n") == "x"
        assert eb._clean_scalar("None") == ""
        assert eb._clean_scalar("") == ""


# ---------------------------------------------------------------------------
# Runtime wrappers (AWS CLI mocked)
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRunAws:
    def test_success_returns_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(eb.subprocess, "run", lambda *a, **k: _FakeCompleted(0, "hello", ""))
        assert eb._run_aws(["aws", "sts", "get-caller-identity"]) == "hello"

    def test_failure_raises_runtimeerror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(eb.subprocess, "run", lambda *a, **k: _FakeCompleted(255, "", "boom"))
        with pytest.raises(RuntimeError, match="boom"):
            eb._run_aws(["aws", "ec2", "run-instances"])

    def test_allow_exists_swallows_already_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            eb.subprocess,
            "run",
            lambda *a, **k: _FakeCompleted(255, "", "EntityAlreadyExists: role exists"),
        )
        # Does not raise; returns (empty) stdout.
        assert eb._run_aws(["aws", "iam", "create-role"], allow_exists=True) == ""

    def test_allow_exists_swallows_attached_role_quota_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            eb.subprocess,
            "run",
            lambda *a, **k: _FakeCompleted(
                255,
                "",
                "LimitExceeded: Cannot exceed quota for InstanceSessionsPerInstanceProfile: 1",
            ),
        )
        assert eb._run_aws(["aws", "iam", "add-role-to-instance-profile"], allow_exists=True) == ""

    def test_missing_cli_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*a: object, **k: object) -> None:
            raise FileNotFoundError

        monkeypatch.setattr(eb.subprocess, "run", _boom)
        with pytest.raises(RuntimeError, match="AWS CLI not found"):
            eb._run_aws(["aws", "sts", "get-caller-identity"])


class TestResolveHelpers:
    def test_resolve_bastion_ami(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(eb, "_run_aws", lambda cmd, **k: "ami-0fd6240f599091088\n")
        assert eb.resolve_bastion_ami("us-east-1") == "ami-0fd6240f599091088"

    def test_resolve_network_uses_private_cluster_subnet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake(cmd: list[str], **k: object) -> str:
            if "describe-cluster" in cmd:
                return json.dumps(
                    {
                        "vpc": "vpc-0123456789abcdef0",
                        "sg": "sg-0123456789abcdef0",
                        "subnets": ["subnet-0aaaaaaaaaaaaaaaa"],
                    }
                )
            if "describe-subnets" in cmd:
                return "subnet-0aaaaaaaaaaaaaaaa"
            raise AssertionError(cmd)

        monkeypatch.setattr(eb, "_run_aws", _fake)
        net = eb.resolve_bastion_network("gco-us-east-1", "us-east-1")
        assert net.subnet_id == "subnet-0aaaaaaaaaaaaaaaa"
        assert net.security_group_id == "sg-0123456789abcdef0"

    def test_resolve_network_refuses_public_only_subnets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake(cmd: list[str], **k: object) -> str:
            if "describe-cluster" in cmd:
                return json.dumps(
                    {
                        "vpc": "vpc-0123456789abcdef0",
                        "sg": "sg-0123456789abcdef0",
                        "subnets": ["subnet-0aaaaaaaaaaaaaaaa"],
                    }
                )
            return "None"

        monkeypatch.setattr(eb, "_run_aws", _fake)
        with pytest.raises(RuntimeError, match="refusing to launch a public bastion"):
            eb.resolve_bastion_network("gco-us-east-1", "us-east-1")

    def test_resolve_network_no_subnets_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake(cmd: list[str], **k: object) -> str:
            if "describe-cluster" in cmd:
                return json.dumps(
                    {"vpc": "vpc-0123456789abcdef0", "sg": "sg-0123456789abcdef0", "subnets": []}
                )
            return "None"

        monkeypatch.setattr(eb, "_run_aws", _fake)
        with pytest.raises(RuntimeError, match="cannot place a private bastion"):
            eb.resolve_bastion_network("gco-us-east-1", "us-east-1")

    def test_ensure_iam_runs_four_idempotent_steps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def _fake(cmd: list[str], **k: object) -> str:
            assert k.get("allow_exists") is True
            calls.append(cmd)
            return ""

        monkeypatch.setattr(eb, "_run_aws", _fake)
        eb.ensure_bastion_iam()
        verbs = [c[2] for c in calls]
        assert verbs == [
            "create-role",
            "attach-role-policy",
            "create-instance-profile",
            "add-role-to-instance-profile",
        ]


class TestLaunchBastion:
    _NET = None

    def _net(self) -> eb.BastionNetwork:
        return eb.BastionNetwork(
            vpc_id="vpc-0123456789abcdef0",
            subnet_id="subnet-0123456789abcdef0",
            security_group_id="sg-0123456789abcdef0",
        )

    def test_launch_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(eb, "_run_aws", lambda cmd, **k: "i-0123456789abcdef0")
        out = eb.launch_bastion(
            network=self._net(), ami_id="ami-0123456789abcdef0", region="us-east-1", ttl_minutes=120
        )
        assert out == "i-0123456789abcdef0"

    def test_launch_retries_on_profile_propagation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(eb.time, "sleep", lambda *_: None)
        state = {"n": 0}

        def _fake(cmd: list[str], **k: object) -> str:
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("Invalid IAM Instance Profile name")
            return "i-0123456789abcdef0"

        monkeypatch.setattr(eb, "_run_aws", _fake)
        out = eb.launch_bastion(
            network=self._net(), ami_id="ami-0123456789abcdef0", region="us-east-1", ttl_minutes=120
        )
        assert out == "i-0123456789abcdef0"
        assert state["n"] == 3

    def test_launch_reraises_non_propagation_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake(cmd: list[str], **k: object) -> str:
            raise RuntimeError("UnauthorizedOperation")

        monkeypatch.setattr(eb, "_run_aws", _fake)
        with pytest.raises(RuntimeError, match="UnauthorizedOperation"):
            eb.launch_bastion(
                network=self._net(),
                ami_id="ami-0123456789abcdef0",
                region="us-east-1",
                ttl_minutes=120,
            )


class TestWaitOnline:
    def test_returns_when_online(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(eb, "_run_aws", lambda cmd, **k: "Online")
        # Should return without raising.
        eb.wait_until_ssm_online(
            "i-0123456789abcdef0", "us-east-1", timeout_seconds=10, poll_interval_seconds=0
        )

    def test_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(eb, "_run_aws", lambda cmd, **k: "")
        with pytest.raises(RuntimeError, match="did not come Online"):
            eb.wait_until_ssm_online(
                "i-0123456789abcdef0", "us-east-1", timeout_seconds=0, poll_interval_seconds=0
            )


class TestCreateDestroyLifecycle:
    def test_create_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(eb, "resolve_bastion_ami", lambda region: "ami-0123456789abcdef0")
        monkeypatch.setattr(
            eb,
            "resolve_bastion_network",
            lambda c, r: eb.BastionNetwork(
                "vpc-0123456789abcdef0", "subnet-0123456789abcdef0", "sg-0123456789abcdef0"
            ),
        )
        monkeypatch.setattr(eb, "ensure_bastion_iam", lambda *a, **k: None)
        monkeypatch.setattr(eb, "launch_bastion", lambda **k: "i-0123456789abcdef0")
        monkeypatch.setattr(eb, "wait_until_ssm_online", lambda *a, **k: None)
        assert eb.create_ephemeral_bastion("gco-us-east-1", "us-east-1") == "i-0123456789abcdef0"

    def test_create_is_atomic_on_online_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(eb, "resolve_bastion_ami", lambda region: "ami-0123456789abcdef0")
        monkeypatch.setattr(
            eb,
            "resolve_bastion_network",
            lambda c, r: eb.BastionNetwork(
                "vpc-0123456789abcdef0", "subnet-0123456789abcdef0", "sg-0123456789abcdef0"
            ),
        )
        monkeypatch.setattr(eb, "ensure_bastion_iam", lambda *a, **k: None)
        monkeypatch.setattr(eb, "launch_bastion", lambda **k: "i-0123456789abcdef0")

        def _boom(*a: object, **k: object) -> None:
            raise RuntimeError("never came online")

        destroyed: list[str] = []
        monkeypatch.setattr(eb, "wait_until_ssm_online", _boom)
        monkeypatch.setattr(
            eb, "destroy_ephemeral_bastion", lambda iid, region, **k: destroyed.append(iid)
        )
        with pytest.raises(RuntimeError, match="never came online"):
            eb.create_ephemeral_bastion("gco-us-east-1", "us-east-1")
        # The instance we launched was cleaned up rather than leaked.
        assert destroyed == ["i-0123456789abcdef0"]

    def test_create_skips_wait_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(eb, "resolve_bastion_ami", lambda region: "ami-0123456789abcdef0")
        monkeypatch.setattr(
            eb,
            "resolve_bastion_network",
            lambda c, r: eb.BastionNetwork(
                "vpc-0123456789abcdef0", "subnet-0123456789abcdef0", "sg-0123456789abcdef0"
            ),
        )
        monkeypatch.setattr(eb, "ensure_bastion_iam", lambda *a, **k: None)
        monkeypatch.setattr(eb, "launch_bastion", lambda **k: "i-0123456789abcdef0")

        def _fail(*a: object, **k: object) -> None:
            raise AssertionError("wait should be skipped")

        monkeypatch.setattr(eb, "wait_until_ssm_online", _fail)
        assert (
            eb.create_ephemeral_bastion("gco-us-east-1", "us-east-1", wait_online=False)
            == "i-0123456789abcdef0"
        )

    def test_destroy_terminates_and_cleans_iam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(eb, "_run_aws", lambda cmd, **k: calls.append(cmd) or "")
        eb.destroy_ephemeral_bastion("i-0123456789abcdef0", "us-east-1")
        verbs = [c[2] for c in calls]
        assert verbs[0] == "terminate-instances"
        assert "delete-role" in verbs

    def test_destroy_skips_iam_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(eb, "_run_aws", lambda cmd, **k: calls.append(cmd) or "")
        eb.destroy_ephemeral_bastion("i-0123456789abcdef0", "us-east-1", delete_iam=False)
        assert [c[2] for c in calls] == ["terminate-instances"]

    def test_destroy_iam_failure_is_best_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake(cmd: list[str], **k: object) -> str:
            if cmd[2] == "terminate-instances":
                return "shutting-down"
            raise RuntimeError("iam boom")

        # Must not raise even though every IAM step fails.
        monkeypatch.setattr(eb, "_run_aws", _fake)
        eb.destroy_ephemeral_bastion("i-0123456789abcdef0", "us-east-1")

    def test_context_manager_tears_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        destroyed: list[str] = []
        monkeypatch.setattr(eb, "create_ephemeral_bastion", lambda c, r, **k: "i-0123456789abcdef0")
        monkeypatch.setattr(
            eb, "destroy_ephemeral_bastion", lambda iid, region, **k: destroyed.append(iid)
        )
        with eb.ephemeral_bastion("gco-us-east-1", "us-east-1") as iid:
            assert iid == "i-0123456789abcdef0"
        assert destroyed == ["i-0123456789abcdef0"]


# ---------------------------------------------------------------------------
# Project-scoped naming (role / instance-profile / Name tag)
# ---------------------------------------------------------------------------


class TestProjectScopedNaming:
    def test_default_project_matches_constants(self) -> None:
        assert eb.bastion_role_name() == eb.BASTION_ROLE_NAME == "gco-ephemeral-bastion-role"
        assert (
            eb.bastion_profile_name() == eb.BASTION_PROFILE_NAME == "gco-ephemeral-bastion-profile"
        )
        assert eb.bastion_instance_name() == eb.BASTION_NAME == "gco-ephemeral-ssm-bastion"

    def test_custom_project_scopes_names(self) -> None:
        assert eb.bastion_role_name("acme") == "acme-ephemeral-bastion-role"
        assert eb.bastion_profile_name("acme") == "acme-ephemeral-bastion-profile"
        assert eb.bastion_instance_name("acme") == "acme-ephemeral-ssm-bastion"

    @pytest.mark.parametrize("bad", ["", "-bad", "bad name", "a/b", "x" * 64])
    def test_rejects_bad_project(self, bad: str) -> None:
        with pytest.raises(ValueError):
            eb.bastion_role_name(bad)

    def test_ensure_iam_uses_project_scoped_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(eb, "_run_aws", lambda cmd, **k: calls.append(cmd) or "")
        eb.ensure_bastion_iam("acme")
        create = calls[0]  # create-role is first
        assert create[create.index("--role-name") + 1] == "acme-ephemeral-bastion-role"

    def test_run_instances_uses_project_scoped_name_tag(self) -> None:
        cmd = eb.build_run_instances_command(
            ami_id="ami-0123456789abcdef0",
            instance_type="t3.micro",
            subnet_id="subnet-0123456789abcdef0",
            security_group_id="sg-0123456789abcdef0",
            profile_name="acme-ephemeral-bastion-profile",
            region="us-east-1",
            user_data="x",
            instance_name="acme-ephemeral-ssm-bastion",
            project_name="acme",
        )
        tagspec = cmd[cmd.index("--tag-specifications") + 1]
        assert "Key=Name,Value=acme-ephemeral-ssm-bastion" in tagspec
        assert f"Key={eb.TAG_PROJECT_KEY},Value=acme" in tagspec

    def test_destroy_deletes_project_scoped_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(eb, "_run_aws", lambda cmd, **k: calls.append(cmd) or "")
        eb.destroy_ephemeral_bastion("i-0123456789abcdef0", "us-east-1", project_name="acme")
        delete_role = next(c for c in calls if c[2] == "delete-role")
        assert delete_role[delete_role.index("--role-name") + 1] == "acme-ephemeral-bastion-role"

    def test_create_threads_project_to_iam_and_launch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(eb, "resolve_bastion_ami", lambda region: "ami-0123456789abcdef0")
        monkeypatch.setattr(
            eb,
            "resolve_bastion_network",
            lambda c, r: eb.BastionNetwork(
                "vpc-0123456789abcdef0", "subnet-0123456789abcdef0", "sg-0123456789abcdef0"
            ),
        )
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            eb, "ensure_bastion_iam", lambda project: seen.__setitem__("iam", project)
        )
        monkeypatch.setattr(
            eb,
            "launch_bastion",
            lambda **k: seen.__setitem__("launch", k.get("project_name")) or "i-0123456789abcdef0",
        )
        monkeypatch.setattr(eb, "wait_until_ssm_online", lambda *a, **k: None)
        out = eb.create_ephemeral_bastion("acme-us-east-1", "us-east-1", project_name="acme")
        assert out == "i-0123456789abcdef0"
        assert seen["iam"] == "acme"
        assert seen["launch"] == "acme"
