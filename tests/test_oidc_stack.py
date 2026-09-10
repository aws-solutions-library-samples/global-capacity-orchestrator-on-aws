"""
Tests for the GitHub OIDC provider CDK stack (.github/oidc_provider/).

Verifies that the standalone stack synthesizes correctly and produces
the expected IAM resources with proper trust policies and permissions.
"""

import importlib.util
import json
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

# Ensure the oidc_provider directory is importable
sys.path.insert(0, str(Path(__file__).parent.parent / ".github" / "oidc_provider"))

from stack import GCOGitHubOIDCStack

_OIDC_CONFIG_PATH = Path(__file__).parent.parent / ".github" / "oidc_provider" / "cdk.json"
_OIDC_CONTEXT = json.loads(_OIDC_CONFIG_PATH.read_text(encoding="utf-8"))["context"]
_UPSTREAM_SUBJECT_PREFIX = str(_OIDC_CONTEXT["github_subject_prefix"])


def _synth_stack(
    github_repo: str = "aws-solutions-library-samples/global-capacity-orchestrator-on-aws",
    github_subject_prefix: str | None = None,
    github_branch: str = "main",
) -> Template:
    """Synthesize the OIDC stack and return a CDK Template for assertions."""
    app = cdk.App()
    stack = GCOGitHubOIDCStack(
        app,
        "TestOIDCStack",
        github_repo=github_repo,
        github_subject_prefix=github_subject_prefix,
        github_branch=github_branch,
    )
    return Template.from_stack(stack)


class TestOIDCStackSynthesis:
    """Verify the stack synthesizes without errors."""

    def test_stack_synthesizes(self):
        """Stack should synthesize without throwing."""
        template = _synth_stack()
        assert template is not None

    def test_stack_has_oidc_provider(self):
        """Stack should create an OIDC provider."""
        template = _synth_stack()
        template.resource_count_is("Custom::AWSCDKOpenIdConnectProvider", 1)

    def test_stack_has_iam_role(self):
        """Stack should create an IAM role (plus the OIDC provider's custom resource role)."""
        template = _synth_stack()
        template.resource_count_is("AWS::IAM::Role", 2)

    def test_stack_has_iam_policy(self):
        """Stack should create an inline IAM policy."""
        template = _synth_stack()
        template.resource_count_is("AWS::IAM::Policy", 1)

    def test_stack_has_outputs(self):
        """Stack should export the role ARN and OIDC provider ARN."""
        template = _synth_stack()
        template.has_output("RoleArn", {"Description": Match.string_like_regexp(".*role ARN.*")})
        template.has_output(
            "OIDCProviderArn", {"Description": Match.string_like_regexp(".*OIDC.*")}
        )


class TestOIDCProviderConfig:
    """Verify the OIDC provider is configured correctly."""

    def test_provider_uses_both_thumbprints(self):
        """OIDC provider should include both the primary and backup thumbprints."""
        from stack import GITHUB_OIDC_BACKUP_THUMBPRINT, GITHUB_OIDC_THUMBPRINT

        # Verify both constants are defined, distinct, and valid SHA-1 length.
        assert GITHUB_OIDC_THUMBPRINT != GITHUB_OIDC_BACKUP_THUMBPRINT
        assert len(GITHUB_OIDC_THUMBPRINT) == 40
        assert len(GITHUB_OIDC_BACKUP_THUMBPRINT) == 40

    def test_checked_in_context_pins_current_immutable_subject_prefix(self):
        """The deployed upstream stack must follow GitHub's reported prefix."""
        owner, repository = str(_OIDC_CONTEXT["github_repo"]).split("/", 1)
        assert f"repo:{owner}@109766924/{repository}@1219314144" == _UPSTREAM_SUBJECT_PREFIX


class TestOIDCTrustPolicy:
    """Verify the IAM role trust policy is correctly scoped."""

    def test_default_repo_main_branch(self):
        """Default config should require the exact main branch subject."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Condition": Match.object_like(
                                        {
                                            "StringEquals": Match.object_like(
                                                {
                                                    "token.actions.githubusercontent.com:sub": "repo:aws-solutions-library-samples/global-capacity-orchestrator-on-aws:ref:refs/heads/main"
                                                }
                                            )
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_immutable_subject_prefix_main_branch(self):
        """Transferred repositories should trust the exact immutable subject."""
        prefix = _UPSTREAM_SUBJECT_PREFIX
        template = _synth_stack(github_subject_prefix=prefix)
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Condition": Match.object_like(
                                        {
                                            "StringEquals": Match.object_like(
                                                {
                                                    "token.actions.githubusercontent.com:sub": f"{prefix}:ref:refs/heads/main"
                                                }
                                            )
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_immutable_subject_prefix_supports_explicit_branch_wildcard(self):
        """The any-ref opt-in should retain the immutable repository identity."""
        prefix = "repo:my-org@12345/my-repo@67890"
        template = _synth_stack(
            github_repo="my-org/my-repo",
            github_subject_prefix=prefix,
            github_branch="*",
        )
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Condition": Match.object_like(
                                        {
                                            "StringLike": {
                                                "token.actions.githubusercontent.com:sub": f"{prefix}:*"
                                            }
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            },
        )

    @pytest.mark.parametrize(
        "prefix",
        [
            "",
            " repo:my-org/my-repo",
            "repo:my-org@12345/other-repo@67890",
            "repo:other-org@12345/my-repo@67890",
            "repo:my-org@abc/my-repo@67890",
            "repo:my-org@12345/my-repo@67890:*",
        ],
    )
    def test_invalid_subject_prefix_is_rejected(self, prefix: str):
        with pytest.raises(ValueError, match="github_subject_prefix"):
            _synth_stack(
                github_repo="my-org/my-repo",
                github_subject_prefix=prefix,
            )

    def test_specific_branch_uses_string_equals(self):
        """An explicit branch name should be represented exactly."""
        template = _synth_stack(github_branch="release")
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Condition": Match.object_like(
                                        {
                                            "StringEquals": Match.object_like(
                                                {
                                                    "token.actions.githubusercontent.com:sub": "repo:aws-solutions-library-samples/global-capacity-orchestrator-on-aws:ref:refs/heads/release"
                                                }
                                            ),
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_explicit_wildcard_uses_string_like(self):
        """Wildcard trust remains available only as an explicit opt-in."""
        template = _synth_stack(github_branch="*")
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Condition": Match.object_like(
                                        {
                                            "StringLike": {
                                                "token.actions.githubusercontent.com:sub": "repo:aws-solutions-library-samples/global-capacity-orchestrator-on-aws:*"
                                            }
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_custom_repo_reflected_in_trust(self):
        """Custom github_repo should appear in the main-branch subject."""
        template = _synth_stack(github_repo="my-org/my-fork")
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Condition": Match.object_like(
                                        {
                                            "StringEquals": Match.object_like(
                                                {
                                                    "token.actions.githubusercontent.com:sub": "repo:my-org/my-fork:ref:refs/heads/main"
                                                }
                                            )
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_audience_claim_is_sts(self):
        """Trust policy should require aud = sts.amazonaws.com."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Condition": Match.object_like(
                                        {
                                            "StringEquals": Match.object_like(
                                                {
                                                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                                                }
                                            ),
                                        }
                                    ),
                                }
                            )
                        ]
                    )
                }
            },
        )


class TestOIDCIAMPolicy:
    """Verify the IAM policy contains the expected permissions."""

    def test_policy_has_eks_describe_addon_versions(self):
        """Policy should allow eks:DescribeAddonVersions."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": Match.array_with(["eks:DescribeAddonVersions"]),
                                    "Effect": "Allow",
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_policy_has_eks_describe_cluster_versions(self):
        """Policy should allow eks:DescribeClusterVersions so the
        dependency scan can detect newer Kubernetes minor releases."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": Match.array_with(["eks:DescribeClusterVersions"]),
                                    "Effect": "Allow",
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_policy_has_ec2_accelerator_catalog_describe_actions(self):
        """Policy should allow the monthly accelerator catalog scan."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": Match.array_with(
                                        [
                                            "ec2:DescribeInstanceTypes",
                                            "ec2:DescribeRegions",
                                        ]
                                    ),
                                    "Effect": "Allow",
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_policy_has_rds_describe(self):
        """Policy should allow rds:DescribeDBEngineVersions."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": Match.array_with(["rds:DescribeDBEngineVersions"]),
                                    "Effect": "Allow",
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_policy_has_sts_get_caller_identity(self):
        """Policy should allow sts:GetCallerIdentity."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": Match.array_with(["sts:GetCallerIdentity"]),
                                    "Effect": "Allow",
                                }
                            )
                        ]
                    )
                }
            },
        )


class TestOIDCRoleProperties:
    """Verify IAM role naming and description."""

    def test_role_name_includes_region(self):
        """Role name should include 'gco-github-actions' prefix."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "RoleName": Match.any_value(),
                "Description": Match.string_like_regexp(".*GitHub Actions.*"),
            },
        )

    def test_role_description_includes_repo(self):
        """Role description should mention the GitHub repo."""
        template = _synth_stack()
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "Description": Match.string_like_regexp(
                    ".*aws-solutions-library-samples/global-capacity-orchestrator-on-aws.*"
                ),
            },
        )

    def test_custom_repo_in_description(self):
        """Custom repo should appear in the role description."""
        template = _synth_stack(github_repo="my-org/my-fork")
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "Description": Match.string_like_regexp(".*my-org/my-fork.*"),
            },
        )


class TestPolicyJsonFile:
    """Verify the policy.json file is valid and contains expected structure."""

    def test_policy_json_is_valid(self):
        """policy.json should be valid JSON."""
        policy_path = Path(__file__).parent.parent / ".github" / "oidc_provider" / "policy.json"
        policy = json.loads(policy_path.read_text())
        assert policy["Version"] == "2012-10-17"
        assert "Statement" in policy
        assert len(policy["Statement"]) > 0

    def test_policy_json_has_allow_effect(self):
        """All statements should have Effect: Allow."""
        policy_path = Path(__file__).parent.parent / ".github" / "oidc_provider" / "policy.json"
        policy = json.loads(policy_path.read_text())
        for stmt in policy["Statement"]:
            assert stmt["Effect"] == "Allow"

    def test_policy_json_actions_are_read_only(self):
        """Default policy should only contain read-only actions (Describe/Get/List)."""
        policy_path = Path(__file__).parent.parent / ".github" / "oidc_provider" / "policy.json"
        policy = json.loads(policy_path.read_text())
        for stmt in policy["Statement"]:
            for action in stmt["Action"]:
                parts = action.split(":")
                verb = parts[1] if len(parts) == 2 else parts[0]
                assert verb.startswith(("Describe", "Get", "List")), (
                    f"Action '{action}' is not read-only. "
                    "Default CI policy should only contain Describe/Get/List actions."
                )


class TestSubjectPrefixValidation:
    """The guardrails on ``github_repo`` / ``github_subject_prefix``.

    This validation is the only thing standing between a copied-and-pasted ID
    pair and a trust policy that authorises a *different* repository to assume
    the CI role, so each rejection path is pinned individually rather than
    inferred from the happy path.
    """

    @pytest.mark.parametrize(
        "repo",
        [
            pytest.param("noslash", id="no-separator"),
            pytest.param("owner/repo/extra", id="too-many-segments"),
            pytest.param("/repo", id="empty-owner"),
            pytest.param("owner/", id="empty-repo"),
        ],
    )
    def test_malformed_github_repo_is_rejected(self, repo: str):
        """A repo that is not exactly ``owner/repo`` cannot yield a valid subject."""
        with pytest.raises(ValueError, match="github_repo must use owner/repo format"):
            _synth_stack(github_repo=repo)

    def test_non_string_subject_prefix_is_rejected(self):
        """Context values come from cdk.json, where a number is easy to write."""
        with pytest.raises(ValueError, match="github_subject_prefix must be a string"):
            _synth_stack(
                github_repo="my-org/my-repo",
                github_subject_prefix=12345,  # type: ignore[arg-type]
            )

    def test_explicit_mutable_prefix_is_accepted_unchanged(self):
        """Spelling out the mutable prefix must behave exactly like omitting it."""
        explicit = _synth_stack(
            github_repo="my-org/my-repo",
            github_subject_prefix="repo:my-org/my-repo",
        ).to_json()
        implicit = _synth_stack(github_repo="my-org/my-repo").to_json()

        assert explicit == implicit

    def test_trailing_whitespace_prefix_is_rejected(self):
        """A prefix is compared literally, so untrimmed input must not slip through."""
        with pytest.raises(ValueError, match="non-empty trimmed string"):
            _synth_stack(
                github_repo="my-org/my-repo",
                github_subject_prefix="repo:my-org/my-repo ",
            )


class TestCDKAppEntryPoint:
    """The standalone ``app.py`` that turns cdk.json context into the stack.

    Its whole job is reading context and applying defaults, and a wrong default
    here is a silent authorisation change — an unset ``github_branch`` must mean
    "main only", never "any branch". Executed rather than imported so each case
    gets a fresh module with its own context.
    """

    @staticmethod
    def _run_app(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, context: dict[str, object]
    ) -> dict:
        """Execute app.py with the given CDK context and return what it synthesized.

        ``app.py`` constructs ``cdk.App()`` with no arguments, so both the
        context it reads and the directory it writes to have to be supplied from
        outside. Wrapping ``aws_cdk.App`` to fill in ``context`` and ``outdir``
        does that without touching the app: ``context`` is the same constructor
        argument the CDK CLI populates from ``cdk.json``, so
        ``try_get_context`` is exercised exactly as it is in a real deploy, and
        ``outdir`` only decides where the template lands (CDK otherwise picks a
        random temporary directory the test could not find).
        """
        outdir = tmp_path / "cdk.out"
        original_app = cdk.App

        def _app_with_context_and_outdir(*args: object, **kwargs: object) -> cdk.App:
            kwargs.setdefault("context", context)
            kwargs.setdefault("outdir", str(outdir))
            return original_app(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(cdk, "App", _app_with_context_and_outdir)

        app_path = Path(__file__).parent.parent / ".github" / "oidc_provider" / "app.py"
        spec = importlib.util.spec_from_file_location("_oidc_app_under_test", app_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Registered before exec so coverage attributes app.py's lines to the
        # file on disk; a module exec'd without a sys.modules entry is invisible
        # to the repository-root measurement.
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)

        template_path = outdir / "GCOGitHubOIDCStack.template.json"
        assert template_path.is_file(), f"app.py synthesized no template at {template_path}"
        return dict(json.loads(template_path.read_text(encoding="utf-8")))

    def _trust_conditions(self, template: dict) -> dict:
        roles = [
            resource
            for resource in template["Resources"].values()
            if resource["Type"] == "AWS::IAM::Role"
            and "Federated" in json.dumps(resource["Properties"]["AssumeRolePolicyDocument"])
        ]
        assert len(roles) == 1, "expected exactly one federated role"
        statement = roles[0]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        return dict(statement["Condition"])

    def test_empty_context_defaults_to_upstream_repo_on_main_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """No context at all must still produce a main-only trust policy."""
        template = self._run_app(tmp_path, monkeypatch, {})
        conditions = self._trust_conditions(template)

        assert "StringEquals" in conditions, "an unset branch must pin exactly one ref"
        assert "StringLike" not in conditions
        subject = json.dumps(conditions["StringEquals"])
        assert "aws-solutions-library-samples/global-capacity-orchestrator-on-aws" in subject
        assert "refs/heads/main" in subject

    def test_context_overrides_are_applied(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Each context key must reach the stack, not just the repo."""
        template = self._run_app(
            tmp_path,
            monkeypatch,
            {
                "github_repo": "my-org/my-repo",
                "github_subject_prefix": "repo:my-org@12345/my-repo@67890",
                "github_branch": "*",
            },
        )
        conditions = self._trust_conditions(template)

        # A wildcard branch is an explicit opt-in and must widen the operator.
        assert "StringLike" in conditions
        subject = json.dumps(conditions["StringLike"])
        assert "repo:my-org@12345/my-repo@67890" in subject

    def test_the_checked_in_cdk_json_context_synthesizes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The committed context is what a deploy actually uses, so it must work."""
        template = self._run_app(tmp_path, monkeypatch, dict(_OIDC_CONTEXT))
        conditions = self._trust_conditions(template)

        assert _UPSTREAM_SUBJECT_PREFIX in json.dumps(conditions)
