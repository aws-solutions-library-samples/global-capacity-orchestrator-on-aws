"""A Studio user's first sign-in with a temporary password, and their removal with the Studio profile.

``gco analytics studio login`` answers Cognito's ``NEW_PASSWORD_REQUIRED``
challenge (``--new-password``, ``$GCO_STUDIO_NEW_PASSWORD`` or a prompt on a
terminal) where it used to stop with "no AccessToken returned".
``gco analytics users remove`` deletes the user's SageMaker Studio profile,
with the spaces it owns and their apps, before the Cognito user, and only a
profile whose owner tag holds the user's Cognito ``sub``; it used to delete
only the Cognito user, so a new user of the same name got HTTP 403 at login.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli import analytics_user_mgmt as aum
from cli.commands import analytics_cmd
from cli.commands.analytics_cmd import analytics
from cli.config import GCOConfig


def _client_error(code: str, message: str = "", operation: str = "Operation") -> ClientError:
    error = {"Code": code, "Message": message} if message else {"Code": code}
    return ClientError({"Error": error}, operation)  # type: ignore[arg-type]


def _invoke(args: list[str], *, output_format: str = "table", **kwargs: Any) -> Any:
    config = GCOConfig(
        project_name="gco",
        default_region="us-east-2",
        global_region="us-east-2",
        api_gateway_region="us-east-2",
        output_format=output_format,
    )
    return CliRunner().invoke(analytics, args, obj=config, **kwargs)


# ---------------------------------------------------------------------------
# A fake Studio domain
# ---------------------------------------------------------------------------


class _Paginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return self._pages


class _Studio:
    """SageMaker double for one domain: deletions finish when time passes."""

    def __init__(self) -> None:
        self.now = 0.0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, ClientError] = {}
        self.stuck: set[str] = set()
        self.profile: dict[str, Any] | None = {
            "Status": "InService",
            "UserProfileArn": "arn:aws:sagemaker:us-east-2:000000000000:user-profile/d-1/alice",
        }
        self.tags: list[Any] = [{"Key": aum.STUDIO_PROFILE_OWNER_TAG_KEY, "Value": "sub-alice"}]
        self.spaces: dict[str, dict[str, Any]] = {}
        self.apps: list[dict[str, Any]] = []

    # -- time ------------------------------------------------------------------

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if "apps" not in self.stuck:
            for app in self.apps:
                if app["Status"] == "Deleting":
                    app["Status"] = "Deleted"
        if "spaces" not in self.stuck:
            for name in [n for n, s in self.spaces.items() if s["Status"] == "Deleting"]:
                del self.spaces[name]
        if self.profile is not None and self.profile["Status"] == "Deleting":
            self.profile = None

    # -- the client --------------------------------------------------------------

    def _call(self, operation: str, **kwargs: Any) -> None:
        self.calls.append((operation, kwargs))
        if operation in self.errors:
            raise self.errors[operation]

    def describe_user_profile(self, **kwargs: Any) -> dict[str, Any]:
        self._call("describe_user_profile", **kwargs)
        if self.profile is None:
            raise _client_error("ResourceNotFound")
        return dict(self.profile)

    def get_paginator(self, operation: str) -> _Paginator:
        if operation == "list_tags":
            return _Paginator([{"Tags": self.tags}])
        if operation == "list_spaces":
            spaces = [
                {
                    "SpaceName": name,
                    "OwnershipSettingsSummary": {"OwnerUserProfileName": s["owner"]},
                }
                for name, s in self.spaces.items()
            ]
            return _Paginator([{"Spaces": spaces[:1]}, {"Spaces": ["junk", *spaces[1:]]}, {}])
        assert operation == "list_apps"
        studio = self

        class _Apps:
            def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                if "UserProfileNameEquals" in kwargs:
                    wanted = kwargs["UserProfileNameEquals"]
                    apps = [a for a in studio.apps if a.get("UserProfileName") == wanted]
                else:
                    wanted = kwargs["SpaceNameEquals"]
                    apps = [a for a in studio.apps if a.get("SpaceName") == wanted]
                return [{"Apps": [dict(app) for app in apps]}]

        return _Apps()  # type: ignore[return-value]

    def delete_app(self, **kwargs: Any) -> None:
        apps = [
            app
            for app in self.apps
            if (app["AppType"], app["AppName"]) == (kwargs["AppType"], kwargs["AppName"])
            and app.get("SpaceName") == kwargs.get("SpaceName")
        ]
        try:
            self._call("delete_app", **kwargs)
        except ClientError as exc:
            if aum._is_missing(exc):
                for app in apps:
                    app["Status"] = "Deleted"
            raise
        for app in apps:
            app["Status"] = "Deleting"

    def delete_space(self, **kwargs: Any) -> None:
        try:
            self._call("delete_space", **kwargs)
        except ClientError as exc:
            if aum._is_missing(exc):
                self.spaces.pop(kwargs["SpaceName"], None)
            raise
        self.spaces[kwargs["SpaceName"]]["Status"] = "Deleting"

    def describe_space(self, **kwargs: Any) -> dict[str, Any]:
        self._call("describe_space", **kwargs)
        space = self.spaces.get(kwargs["SpaceName"])
        if space is None:
            raise _client_error("ResourceNotFound")
        return {"Status": space["Status"], "FailureReason": space.get("FailureReason")}

    def delete_user_profile(self, **kwargs: Any) -> None:
        try:
            self._call("delete_user_profile", **kwargs)
        except ClientError as exc:
            if aum._is_missing(exc):
                self.profile = None
            raise
        assert self.profile is not None
        self.profile["Status"] = "Deleting"

    # -- running the helper --------------------------------------------------------

    def remove(self, owner_sub: str | None = "sub-alice", **kwargs: Any) -> dict[str, Any]:
        with patch("boto3.client", return_value=self):
            return aum.delete_studio_user_profile(
                "us-east-2",
                "d-1",
                "alice",
                owner_sub,
                sleep=self.sleep,
                clock=self.clock,
                **kwargs,
            )

    def made(self, operation: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == operation]


def _populated() -> _Studio:
    studio = _Studio()
    studio.spaces = {
        "alice-space": {"owner": "alice", "Status": "InService"},
        "bob-space": {"owner": "bob", "Status": "InService"},
    }
    studio.apps = [
        # A space app: listed for the space and, with UserProfileName set, for the user.
        {
            "AppType": "JupyterLab",
            "AppName": "default",
            "SpaceName": "alice-space",
            "UserProfileName": "alice",
            "Status": "InService",
        },
        {
            "AppType": "KernelGateway",
            "AppName": "datascience",
            "UserProfileName": "alice",
            "Status": "InService",
        },
        {
            "AppType": "JupyterServer",
            "AppName": "old",
            "UserProfileName": "alice",
            "Status": "Deleted",
        },
        {
            "AppType": "CodeEditor",
            "AppName": "default",
            "SpaceName": "alice-space",
            "Status": "Deleting",
        },
        {
            "AppType": "JupyterLab",
            "AppName": "default",
            "SpaceName": "bob-space",
            "Status": "InService",
        },
    ]
    return studio


# ---------------------------------------------------------------------------
# delete_studio_user_profile
# ---------------------------------------------------------------------------


class TestDeleteStudioUserProfile:
    def test_apps_then_spaces_then_the_profile_go(self) -> None:
        studio = _populated()
        assert studio.remove() == {
            "profile": "deleted",
            "spaces": ["alice-space"],
            "apps": ["CodeEditor/default", "JupyterLab/default", "KernelGateway/datascience"],
        }
        # Each app once, by its space or by the profile; one already deleting is left to finish.
        assert studio.made("delete_app") == [
            {
                "DomainId": "d-1",
                "AppType": "JupyterLab",
                "AppName": "default",
                "SpaceName": "alice-space",
            },
            {
                "DomainId": "d-1",
                "AppType": "KernelGateway",
                "AppName": "datascience",
                "UserProfileName": "alice",
            },
        ]
        assert studio.made("delete_space") == [{"DomainId": "d-1", "SpaceName": "alice-space"}]
        assert studio.made("delete_user_profile") == [
            {"DomainId": "d-1", "UserProfileName": "alice"}
        ]
        assert "bob-space" in studio.spaces
        order = [name for name, _ in studio.calls if name.startswith("delete_")]
        assert order == ["delete_app", "delete_app", "delete_space", "delete_user_profile"]
        assert studio.profile is None

    def test_an_untagged_profile_is_deleted_for_the_user_of_its_name(self) -> None:
        studio = _Studio()
        studio.tags = []
        assert studio.remove(owner_sub=None)["profile"] == "deleted"

    def test_a_missing_profile_is_reported_absent(self) -> None:
        studio = _Studio()
        studio.profile = None
        assert studio.remove() == {"profile": "absent", "spaces": [], "apps": []}
        assert studio.made("delete_user_profile") == []

    def test_another_users_profile_is_left_in_place(self) -> None:
        """Its owner tag names the user that held the name before."""
        studio = _populated()
        studio.tags = [
            {"Key": "other", "Value": "x"},
            {"Key": aum.STUDIO_PROFILE_OWNER_TAG_KEY, "Value": "sub-old"},
        ]
        assert studio.remove() == {"profile": "owned_by_another_user", "spaces": [], "apps": []}
        assert [name for name, _ in studio.calls if name.startswith("delete_")] == []

    @pytest.mark.parametrize(
        "operation",
        [
            "describe_user_profile",
            "delete_app",
            "delete_space",
            "describe_space",
            "delete_user_profile",
        ],
    )
    def test_a_refused_call_is_raised(self, operation: str) -> None:
        studio = _populated()
        studio.errors[operation] = _client_error("AccessDeniedException")
        with pytest.raises(ClientError, match="AccessDeniedException"):
            studio.remove()

    @pytest.mark.parametrize("operation", ["delete_app", "delete_space", "delete_user_profile"])
    def test_something_already_gone_is_not_an_error(self, operation: str) -> None:
        studio = _populated()
        studio.errors[operation] = _client_error("ResourceNotFound")
        assert studio.remove()["profile"] == "deleted"

    def test_a_space_sagemaker_cannot_delete_stops_the_removal(self) -> None:
        studio = _populated()

        def fail(**kwargs: Any) -> None:
            studio.calls.append(("delete_space", kwargs))
            studio.spaces[kwargs["SpaceName"]].update(Status="Delete_Failed", FailureReason="busy")

        studio.delete_space = fail  # type: ignore[method-assign]
        with pytest.raises(
            aum.StudioProfileRemovalError,
            match="SageMaker could not delete the space 'alice-space': busy",
        ):
            studio.remove()
        assert studio.made("delete_user_profile") == []

    def test_a_profile_already_deleting_is_waited_for(self) -> None:
        studio = _Studio()
        assert studio.profile is not None
        studio.profile["Status"] = "Deleting"
        assert studio.remove()["profile"] == "deleted"
        assert studio.made("delete_user_profile") == []

    def test_deletions_that_do_not_finish_in_time_say_what_is_left(self) -> None:
        studio = _populated()
        studio.stuck.add("apps")
        with pytest.raises(
            aum.StudioProfileRemovalError,
            match=(
                r"did not finish deleting the apps of the Studio profile 'alice' within "
                r"30 seconds; run the command again to finish"
            ),
        ):
            studio.remove(timeout_seconds=30, poll_seconds=10)
        assert studio.now == 30


# ---------------------------------------------------------------------------
# admin_get_user_sub / discover_studio_domain_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        ([{"Name": "email", "Value": "a@example.com"}, {"Name": "sub", "Value": "sub-1"}], "sub-1"),
        ([{"Name": "email", "Value": "a@example.com"}], None),
        ([{"Name": "sub", "Value": 7}], None),
    ],
)
def test_the_cognito_sub_is_read_from_the_user(attributes: list[Any], expected: str | None) -> None:
    cognito = MagicMock()
    cognito.admin_get_user.return_value = {"UserAttributes": attributes}
    with patch("boto3.client", return_value=cognito):
        assert aum.admin_get_user_sub("pool", "us-east-2", "alice") == expected
    cognito.admin_get_user.assert_called_once_with(UserPoolId="pool", Username="alice")


def _domains(*domains: Any) -> MagicMock:
    sagemaker = MagicMock()
    sagemaker.get_paginator.return_value.paginate.return_value = [
        {"Domains": list(domains[:1])},
        {"Domains": ["junk", *domains[1:]]},
    ]
    return sagemaker


@pytest.mark.parametrize(
    ("outputs", "domains", "expected"),
    [
        (None, (), None),
        ([{"OutputKey": "StudioDomainId", "OutputValue": "d-new"}], (), "d-new"),
        (
            [{"OutputKey": "StudioDomainName", "OutputValue": "gco-studio-us-east-2"}],
            (
                {"DomainName": "other", "DomainId": "d-0"},
                {"DomainName": "gco-studio-us-east-2", "DomainId": "d-old"},
            ),
            "d-old",
        ),
        (
            [{"OutputKey": "StudioDomainName", "OutputValue": "gco-studio-us-east-2"}],
            ({"DomainName": "other", "DomainId": "d-0"},),
            None,
        ),
        (
            [{"OutputKey": "StudioDomainName", "OutputValue": "gco-studio-us-east-2"}],
            ({"DomainName": "gco-studio-us-east-2"},),
            None,
        ),
        ([{"OutputKey": "CognitoUserPoolId", "OutputValue": "pool"}], (), None),
    ],
    ids=["no-stack", "output", "by-name", "name-not-listed", "no-id", "no-domain-output"],
)
def test_the_studio_domain_is_found_from_the_stack(
    outputs: list[dict[str, str]] | None, domains: tuple[Any, ...], expected: str | None
) -> None:
    """A stack deployed before it published StudioDomainId is found by its domain name."""
    sagemaker = _domains(*domains)
    with (
        patch.object(aum, "_describe_stack_outputs", return_value=outputs) as describe,
        patch("boto3.client", return_value=sagemaker),
    ):
        assert aum.discover_studio_domain_id("us-east-2", "gco") == expected
    describe.assert_called_once_with("us-east-2", "gco-analytics")


# ---------------------------------------------------------------------------
# gco analytics users remove
# ---------------------------------------------------------------------------


@pytest.fixture
def remove_doubles():
    doubles = SimpleNamespace(
        sub=Mock(return_value="sub-alice"),
        domain=Mock(return_value="d-1"),
        profile=Mock(return_value={"profile": "deleted", "spaces": [], "apps": []}),
        delete=Mock(),
    )
    with (
        patch.object(analytics_cmd, "_require_cognito_pool_id", return_value=("pool", "us-east-2")),
        patch("cli.analytics_user_mgmt.admin_get_user_sub", doubles.sub),
        patch("cli.analytics_user_mgmt.discover_studio_domain_id", doubles.domain),
        patch("cli.analytics_user_mgmt.delete_studio_user_profile", doubles.profile),
        patch("cli.analytics_user_mgmt.admin_delete_user", doubles.delete),
    ):
        yield doubles


class TestUsersRemove:
    def test_keep_studio_profile_deletes_only_the_cognito_user(self, remove_doubles) -> None:
        result = _invoke(
            ["users", "remove", "--username", "alice", "--keep-studio-profile"], input="y\n"
        )
        assert result.exit_code == 0, result.output
        assert "Delete Cognito user 'alice'?" in result.output
        remove_doubles.domain.assert_not_called()
        remove_doubles.profile.assert_not_called()
        remove_doubles.delete.assert_called_once_with("pool", "us-east-2", "alice")

    def test_a_profile_with_nothing_in_it_is_reported_alone(self, remove_doubles) -> None:
        result = _invoke(["users", "remove", "--username", "alice", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Deleted the Studio profile alice" in result.output
        assert "spaces:" not in result.output
        assert "apps:" not in result.output
        remove_doubles.profile.assert_called_once_with("us-east-2", "d-1", "alice", "sub-alice")

    @pytest.mark.parametrize(
        ("profile", "line"),
        [
            ("absent", "No Studio profile named alice"),
            (
                "owned_by_another_user",
                "Left the Studio profile alice in place: its owner tag names another Cognito user",
            ),
        ],
    )
    def test_a_profile_not_deleted_says_why(self, remove_doubles, profile: str, line: str) -> None:
        remove_doubles.profile.return_value = {"profile": profile, "spaces": [], "apps": []}
        result = _invoke(["users", "remove", "--username", "alice", "--yes"])
        assert result.exit_code == 0, result.output
        assert line in result.output
        remove_doubles.delete.assert_called_once()

    @pytest.mark.parametrize("keep", [False, True])
    def test_machine_output_is_one_document(self, remove_doubles, keep: bool) -> None:
        remove_doubles.profile.return_value = {
            "profile": "deleted",
            "spaces": ["alice-space"],
            "apps": ["JupyterLab/default"],
        }
        args = ["users", "remove", "--username", "alice", "--yes"]
        if keep:
            args.append("--keep-studio-profile")
        result = _invoke(args, output_format="json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {
            "username": "alice",
            "cognito_user_deleted": True,
            "studio_profile": None if keep else remove_doubles.profile.return_value,
        }

    def test_a_user_cognito_does_not_have_is_an_error(self, remove_doubles) -> None:
        remove_doubles.sub.side_effect = _client_error("UserNotFoundException")
        result = _invoke(["users", "remove", "--username", "alice", "--yes"])
        assert result.exit_code == 1
        assert "Failed to delete user alice: UserNotFoundException" in result.output
        remove_doubles.delete.assert_not_called()

    def test_without_the_studio_domain_the_cognito_user_is_kept(self, remove_doubles) -> None:
        remove_doubles.domain.return_value = None
        result = _invoke(["users", "remove", "--username", "alice", "--yes"])
        assert result.exit_code == 1
        assert "Could not find the Studio domain of gco-analytics" in result.output
        assert "--keep-studio-profile" in result.output
        remove_doubles.delete.assert_not_called()

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (
                _client_error("AccessDeniedException"),
                "Failed to delete the Studio profile alice: AccessDeniedException. The Cognito "
                "user was kept",
            ),
            (
                aum.StudioProfileRemovalError("SageMaker could not delete the space 's': busy"),
                "SageMaker could not delete the space 's': busy. The Cognito user alice was kept.",
            ),
        ],
    )
    def test_a_profile_that_cannot_be_deleted_keeps_the_cognito_user(
        self, remove_doubles, error: Exception, message: str
    ) -> None:
        remove_doubles.profile.side_effect = error
        result = _invoke(["users", "remove", "--username", "alice", "--yes"])
        assert result.exit_code == 1
        assert message in " ".join(result.output.split())
        remove_doubles.delete.assert_not_called()


# ---------------------------------------------------------------------------
# gco analytics studio login with a temporary password
# ---------------------------------------------------------------------------

_TOKENS = {"IdToken": "id", "AccessToken": "access-after-change", "RefreshToken": "r"}


@pytest.fixture
def login_doubles():
    doubles = SimpleNamespace(
        authenticate=Mock(side_effect=aum.NewPasswordRequiredError("session-1")),
        complete=Mock(return_value=_TOKENS),
        fetch=Mock(return_value=("https://studio.example/auth", 300, "req-1")),
    )
    with (
        patch("cli.analytics_user_mgmt.discover_cognito_pool_id", return_value="pool"),
        patch("cli.analytics_user_mgmt.discover_cognito_client_id", return_value="client"),
        patch(
            "cli.analytics_user_mgmt.discover_api_endpoint", return_value="https://api.example/prod"
        ),
        patch("cli.analytics_user_mgmt.authenticate_with_password", doubles.authenticate),
        patch("cli.analytics_user_mgmt.complete_new_password_challenge", doubles.complete),
        patch("cli.analytics_user_mgmt.fetch_studio_url", doubles.fetch),
    ):
        yield doubles


_LOGIN = ["studio", "login", "--username", "alice", "--password", "Temp!Password1"]


class TestFirstSignIn:
    @pytest.mark.parametrize("source", ["option", "environment"])
    def test_the_new_password_answers_the_challenge(self, login_doubles, source: str) -> None:
        if source == "option":
            result = _invoke([*_LOGIN, "--new-password", "Chosen!Password12"])
        else:
            result = _invoke(_LOGIN, env={"GCO_STUDIO_NEW_PASSWORD": "Chosen!Password12"})
        assert result.exit_code == 0, result.output
        assert result.stdout.strip().splitlines()[-1] == "https://studio.example/auth"
        login_doubles.complete.assert_called_once_with(
            pool_id="pool",
            client_id="client",
            username="alice",
            new_password="Chosen!Password12",
            session="session-1",
            region="us-east-2",
        )
        login_doubles.fetch.assert_called_once_with(
            "https://api.example/prod", "access-after-change"
        )

    def test_without_a_terminal_or_a_new_password_it_says_what_to_do(self, login_doubles) -> None:
        result = _invoke(_LOGIN)
        assert result.exit_code == 1
        assert "still has its temporary password" in result.output
        assert "--new-password" in result.output
        login_doubles.complete.assert_not_called()

    def test_on_a_terminal_the_new_password_is_asked_for_twice(self, login_doubles) -> None:
        with patch.object(analytics_cmd, "_stdin_is_interactive", return_value=True):
            result = _invoke(_LOGIN, input="Chosen!Password12\nChosen!Password12\n")
        assert result.exit_code == 0, result.output
        assert login_doubles.complete.call_args.kwargs["new_password"] == "Chosen!Password12"
        # stdout carries only the URL; the notice and the prompts go to stderr.
        assert result.stdout.strip() == "https://studio.example/auth"
        assert "choose a new one" in result.stderr

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (
                _client_error("InvalidPasswordException", "Password not long enough"),
                "Cognito refused the new password: InvalidPasswordException "
                "(Password not long enough)",
            ),
            (
                _client_error("CodeMismatchException"),
                "Cognito refused the new password: CodeMismatchException",
            ),
            (ValueError("Cognito asked for the MFA_SETUP challenge"), "MFA_SETUP challenge"),
        ],
    )
    def test_a_refused_new_password_is_named(
        self, login_doubles, error: Exception, message: str
    ) -> None:
        login_doubles.complete.side_effect = error
        result = _invoke([*_LOGIN, "--new-password", "short"])
        assert result.exit_code == 1
        assert message in result.output
        login_doubles.fetch.assert_not_called()

    def test_a_challenge_gco_cannot_answer_is_named(self, login_doubles) -> None:
        login_doubles.authenticate.side_effect = ValueError(
            "Cognito asked for the SMS_MFA challenge, which gco cannot answer"
        )
        result = _invoke(_LOGIN)
        assert result.exit_code == 1
        assert "Cognito authentication failed: Cognito asked for the SMS_MFA challenge" in (
            result.output
        )


@pytest.mark.parametrize(
    ("stdin", "expected"),
    [
        (None, False),
        (SimpleNamespace(isatty=lambda: False), False),
        (SimpleNamespace(isatty=lambda: True), True),
    ],
)
def test_a_prompt_needs_a_terminal_on_stdin(
    monkeypatch: pytest.MonkeyPatch, stdin: Any, expected: bool
) -> None:
    monkeypatch.setattr(analytics_cmd.sys, "stdin", stdin)
    assert analytics_cmd._stdin_is_interactive() is expected
