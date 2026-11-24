"""GCO analytics environment command group.

Provides the ``gco analytics`` sub-commands:

* ``enable`` / ``disable`` / ``status`` — flip the
  ``analytics_environment.enabled`` toggle in ``cdk.json``.
* ``users add`` / ``users list`` / ``users remove`` — manage Cognito
  users against the auto-discovered pool id from ``gco-analytics``;
  ``remove`` also deletes the user's SageMaker Studio profile.
* ``studio login`` — sign in to Cognito (``AdminInitiateAuth`` with
  ``ADMIN_USER_PASSWORD_AUTH``, answering ``NEW_PASSWORD_REQUIRED`` for
  an account that still has a temporary password) and fetch a SageMaker
  Studio presigned URL from ``/studio/login`` on the existing
  ``gco-api-gateway``.
* ``doctor`` — pre-flight checks before ``gco stacks deploy
  gco-analytics``.

The Click wiring mirrors ``stacks_cmd.py::fsx_cmd`` exactly. Every
command delegates to helpers in :mod:`cli.analytics_user_mgmt` so the
command layer stays thin and testable via ``click.testing.CliRunner``.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
from typing import Any

import click

from ..config import GCOConfig
from ..output import confirm, emit_structured_document, get_output_formatter, prompt

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


def _stack_missing_message(project_name: str) -> str:
    """Error text when the analytics stack isn't deployed (#139 project-scoped)."""
    stack = f"{project_name}-analytics"
    return (
        f"{stack} stack not deployed — run `gco analytics enable` then `gco stacks deploy {stack}`"
    )


#: The rule ``users add`` enforces and an ``UnsupportedUsername`` login reports.
_STUDIO_USERNAME_RULE = (
    "SageMaker Studio names each user's profile after the username, and allows "
    "only letters, digits and hyphens there, starting and ending with a letter "
    "or digit, 63 characters at most"
)

#: What ``studio login`` prints when API Gateway itself refuses the request.
_STUDIO_LOGIN_UNAUTHORIZED_HINT = (
    "API Gateway refused the Cognito access token. This CLI sends an access token, "
    "which /studio/login accepts from this release on; an API deployed with an "
    "earlier release accepts only an ID token. Redeploy it with "
    "`gco stacks deploy {project}-api-gateway`, then sign in again."
)

#: What ``studio login`` prints when Cognito wants a new password and cannot ask for one.
_FIRST_SIGN_IN_HINT = (
    "This account still has its temporary password, and Cognito wants a new one "
    "before it signs the user in. Choose it with --new-password (or "
    "$GCO_STUDIO_NEW_PASSWORD), run the command on a terminal to be prompted, or ask "
    "an administrator to set one with `gco analytics users set-password`."
)


def _stdin_is_interactive() -> bool:
    """Whether a person on a terminal can answer a prompt."""
    return sys.stdin is not None and sys.stdin.isatty()


def _error_code(exc: Any) -> str:
    return str(exc.response.get("Error", {}).get("Code", "Unknown"))


#: What ``studio login`` prints for the presigned-URL Lambda's error tokens.
_STUDIO_LOGIN_ERROR_HINTS = {
    "UnsupportedUsername": (
        "{username!r} cannot be used in SageMaker Studio: "
        + _STUDIO_USERNAME_RULE
        + ". Ask an administrator for a user created with `gco analytics users add`."
    ),
    "UserProfileOwnedByAnotherUser": (
        "the Studio user profile named {username!r} belongs to another Cognito user. "
        "SageMaker matches profile names without regard to case, so this happens when "
        "two usernames differ only in case, or when a user was removed and a new one "
        "created with the same name. Ask an administrator to delete the old profile "
        "(`aws sagemaker delete-user-profile`) or to give you a different username."
    ),
}


@click.group()
@pass_config
def analytics(config: Any) -> None:
    """Manage the GCO analytics (SageMaker Studio + EMR) environment."""


# ---------------------------------------------------------------------------
# Toggle commands — enable / disable / status
# ---------------------------------------------------------------------------


@analytics.command("status")
@pass_config
def analytics_status(config: Any) -> None:
    """Show the current analytics environment toggle state from cdk.json."""
    from ..stacks import get_analytics_config

    formatter = get_output_formatter(config)
    try:
        current = get_analytics_config()
        formatter.print_info("Analytics environment config:")
        formatter.print(current)
    except Exception as exc:  # surface every loader error
        formatter.print_error(f"Failed to read analytics config: {exc}")
        sys.exit(1)


@analytics.command("enable")
@click.option(
    "--hyperpod",
    is_flag=True,
    help=(
        "Deprecated, changes nothing: sets analytics_environment.hyperpod.enabled, "
        "which the stack ignores."
    ),
)
@click.option(
    "--canvas",
    is_flag=True,
    help="Also enable the SageMaker Canvas no-code ML app.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
@pass_config
def analytics_enable(config: Any, hyperpod: bool, canvas: bool, yes: bool) -> None:
    """Enable the analytics environment in cdk.json.

    Flips ``analytics_environment.enabled`` to ``true``, and ``--canvas``
    flips ``analytics_environment.canvas.enabled`` (which attaches
    ``AmazonSageMakerCanvasFullAccess`` to the SageMaker execution role).
    ``--hyperpod`` still sets ``analytics_environment.hyperpod.enabled``
    but is deprecated: the stack ignores that key. Prints the follow-up
    ``gco stacks deploy gco-analytics`` command — does not deploy
    automatically.
    """
    from ..stacks import get_analytics_config, update_analytics_config

    formatter = get_output_formatter(config)

    if hyperpod:
        formatter.print_warning(
            "--hyperpod is deprecated and changes nothing: the SageMaker execution "
            "role's AmazonSageMakerFullAccess policy already allows SageMaker training "
            "jobs, and HyperPod work goes through the gco-hyperpod queue of a Region "
            "with the cdk.json hyperpod block on (docs/ANALYTICS.md section (g))."
        )

    if not yes:
        formatter.print_info("Analytics environment will be enabled in cdk.json.")
        if canvas:
            formatter.print_info("  Canvas sub-toggle will also be enabled.")
        confirm("\nEnable the analytics environment?", abort=True)

    try:
        current = get_analytics_config()
        # Preserve everything the operator has set under ``hyperpod`` /
        # ``canvas`` — the underlying helper replaces nested blocks
        # wholesale, so we rebuild each sub-dict with only the field we own.
        hyperpod_block = dict(current.get("hyperpod") or {})
        if hyperpod:
            hyperpod_block["enabled"] = True
        hyperpod_block.setdefault("enabled", False)

        canvas_block = dict(current.get("canvas") or {})
        if canvas:
            canvas_block["enabled"] = True
        canvas_block.setdefault("enabled", False)

        update_analytics_config(
            {
                "enabled": True,
                "hyperpod": hyperpod_block,
                "canvas": canvas_block,
            }
        )
        formatter.print_success("Analytics environment enabled in cdk.json")
        formatter.print_info(
            f"Run `gco stacks deploy {config.project_name}-analytics` to apply changes"
        )
    except Exception as exc:  # user-facing error from file I/O
        formatter.print_error(f"Failed to enable analytics environment: {exc}")
        sys.exit(1)


@analytics.command("disable")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
@pass_config
def analytics_disable(config: Any, yes: bool) -> None:
    """Disable the analytics environment in cdk.json.

    Only flips ``analytics_environment.enabled`` to ``false``; the
    ``hyperpod`` / ``canvas`` / ``cognito`` / ``efs`` sub-blocks are
    left untouched so the operator's existing preferences survive a
    disable/enable cycle.
    """
    from ..stacks import update_analytics_config

    formatter = get_output_formatter(config)

    if not yes:
        formatter.print_warning("This will disable the analytics environment.")
        formatter.print_warning(
            "Existing SageMaker Studio / Cognito / EMR resources will be destroyed on next deploy."
        )
        confirm("Are you sure?", abort=True)

    try:
        update_analytics_config({"enabled": False})
        formatter.print_success("Analytics environment disabled in cdk.json")
        formatter.print_info(
            f"Run `gco stacks destroy {config.project_name}-analytics` to tear down resources"
        )
    except Exception as exc:  # user-facing error from file I/O
        formatter.print_error(f"Failed to disable analytics environment: {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Users subgroup
# ---------------------------------------------------------------------------


@analytics.group("users")
@pass_config
def users_cmd(config: Any) -> None:
    """Manage Cognito users who can sign in to SageMaker Studio."""


def _require_cognito_pool_id(config: Any) -> tuple[str, str]:
    """Return ``(pool_id, region)`` or exit with the documented error message."""
    from ..analytics_user_mgmt import discover_cognito_pool_id

    formatter = get_output_formatter(config)
    region = config.api_gateway_region
    pool_id = discover_cognito_pool_id(region, config.project_name)
    if not pool_id:
        formatter.print_error(_stack_missing_message(config.project_name))
        sys.exit(1)
    return pool_id, region


@users_cmd.command("add")
@click.option("--username", required=True, help="Cognito username to create.")
@click.option("--email", help="Email address for the new user (optional).")
@click.option(
    "--no-email",
    is_flag=True,
    help="Suppress the Cognito welcome email (MessageAction=SUPPRESS).",
)
@click.option(
    "--password",
    envvar="GCO_STUDIO_PASSWORD",
    help=(
        "Set a permanent password via admin_set_user_password (also read "
        "from $GCO_STUDIO_PASSWORD). Mutually exclusive with --generate-password."
    ),
)
@click.option(
    "--generate-password",
    is_flag=True,
    help=(
        "Generate a strong random password, set it as permanent via "
        "admin_set_user_password, and print it once. Mutually exclusive "
        "with --password."
    ),
)
@pass_config
def users_add(
    config: Any,
    username: str,
    email: str | None,
    no_email: bool,
    password: str | None,
    generate_password: bool,
) -> None:
    """Create a Cognito user and print the temporary password exactly once.

    The CLI generates the temporary password, and Cognito emails it to
    --email in its invitation unless --no-email is given; the user chooses a
    new password at the first ``gco analytics studio login``. When
    ``--password`` or ``--generate-password`` is passed, the user is
    created without an invitation and then has a permanent password set via
    ``admin_set_user_password`` — this skips the ``NEW_PASSWORD_REQUIRED``
    challenge on first login, so the resulting credentials work directly
    with ``gco analytics studio login``.
    """
    from botocore.exceptions import ClientError

    from ..analytics_user_mgmt import (
        admin_create_user,
        admin_set_user_password,
        find_user_ignoring_case,
        generate_strong_password,
        is_studio_username,
    )

    formatter = get_output_formatter(config)

    if password and generate_password:
        formatter.print_error("--password and --generate-password are mutually exclusive")
        sys.exit(1)

    if not is_studio_username(username):
        formatter.print_error(f"Cannot create user {username!r}: {_STUDIO_USERNAME_RULE}.")
        sys.exit(1)

    pool_id, region = _require_cognito_pool_id(config)

    # The pool's usernames are case-sensitive and SageMaker's profile names
    # are not: a second user that differs only in case would get the first
    # one's Studio profile name, which the presigned-URL Lambda refuses.
    try:
        existing = find_user_ignoring_case(pool_id, region, username)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        formatter.print_error(f"Failed to check the existing users: {error_code}")
        sys.exit(1)
    if existing == username:
        formatter.print_error(f"Cognito user {username} already exists")
        sys.exit(1)
    if existing is not None:
        formatter.print_error(
            f"Cannot create user {username!r}: Cognito user {existing!r} already exists, "
            "and SageMaker Studio matches profile names without regard to case, so the "
            "two would share one profile name"
        )
        sys.exit(1)

    permanent = bool(password or generate_password)
    # Cognito never returns the temporary password it generates, so the CLI
    # makes it, to print it; a permanent password replaces it at once.
    temporary_password = generate_strong_password()
    invitation_emailed = bool(email) and not no_email and not permanent
    try:
        admin_create_user(
            pool_id=pool_id,
            region=region,
            username=username,
            email=email,
            # The invitation carries the temporary password, which a
            # permanent one is about to replace.
            suppress_email=no_email or permanent,
            temporary_password=None if permanent else temporary_password,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        formatter.print_error(f"Failed to create user {username}: {error_code}")
        sys.exit(1)

    if config.output_format == "table":
        formatter.print_success(f"Created Cognito user: {username}")

    # Password path — explicit or generated — takes precedence over the
    # temporary-password path so the resulting credentials don't get
    # blocked by NEW_PASSWORD_REQUIRED on first sign-in.
    if permanent:
        final_password = password or generate_strong_password()
        try:
            admin_set_user_password(
                pool_id=pool_id,
                region=region,
                username=username,
                password=final_password,
                permanent=True,
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "Unknown")
            formatter.print_error(
                f"User {username} created, but setting the password "
                f"failed: {error_code}. Retry with "
                "`aws cognito-idp admin-set-user-password --permanent`."
            )
            sys.exit(1)

        if config.output_format == "table":
            if generate_password:
                formatter.print_info(f"Generated password (printed exactly once): {final_password}")
            else:
                formatter.print_info(f"Password set (permanent) for {username}")
        else:
            result: dict[str, Any] = {
                "created": True,
                "username": username,
                "email": email,
                "user_pool_id": pool_id,
                "region": region,
                "password_state": "permanent",  # nosec B105  # state label, not a credential
                "password_generated": generate_password,
                "password_source": "generated" if generate_password else "provided",
                "password_permanent": True,  # nosec B105  # boolean flag, not a credential
                "invitation_emailed": False,
            }
            if generate_password:
                result["password"] = final_password
            formatter.print(result)
        return

    if config.output_format == "table":
        formatter.print_info(f"Temporary password (printed exactly once): {temporary_password}")
        if invitation_emailed:
            formatter.print_info(f"Cognito also emailed it to {email} in its invitation.")
        formatter.print_info(
            "The user chooses a new password at the first `gco analytics studio login`."
        )
    else:
        result = {
            "created": True,
            "username": username,
            "email": email,
            "user_pool_id": pool_id,
            "region": region,
            "password_state": "temporary",  # nosec B105  # state label, not a credential
            "password_generated": True,  # nosec B105  # boolean flag, not a credential
            "password_source": "generated",  # nosec B105  # source label, not a credential
            "password_permanent": False,  # nosec B105  # boolean flag, not a credential
            "invitation_emailed": invitation_emailed,
            "password": temporary_password,
        }
        formatter.print(result)


@users_cmd.command("list")
@click.option("--as-json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@pass_config
def users_list(config: Any, as_json: bool) -> None:
    """List Cognito users in the analytics user pool."""
    from botocore.exceptions import ClientError

    from ..analytics_user_mgmt import list_users as _list_users

    formatter = get_output_formatter(config)
    pool_id, region = _require_cognito_pool_id(config)

    try:
        users = _list_users(pool_id, region)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        formatter.print_error(f"Failed to list users: {error_code}")
        sys.exit(1)

    if as_json:
        emit_structured_document(
            users,
            output_format="json",
            rendered=json.dumps(users, indent=2),
        )
        return
    formatter.print(users)


def _studio_removal_lines(username: str, studio: dict[str, Any]) -> list[str]:
    """What ``users remove`` did to the user's Studio profile, for the table."""
    if studio["profile"] == "absent":
        return [f"No Studio profile named {username}"]
    if studio["profile"] == "owned_by_another_user":
        return [
            f"Left the Studio profile {username} in place: its owner tag names another Cognito user"
        ]
    lines = [f"Deleted the Studio profile {username}"]
    if studio["spaces"]:
        lines.append(f"  spaces: {', '.join(studio['spaces'])}")
    if studio["apps"]:
        lines.append(f"  apps: {', '.join(studio['apps'])}")
    return lines


@users_cmd.command("remove")
@click.option("--username", required=True, help="Cognito username to remove.")
@click.option(
    "--keep-studio-profile",
    is_flag=True,
    help="Delete only the Cognito user; leave the user's Studio profile, spaces and apps.",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@pass_config
def users_remove(config: Any, username: str, keep_studio_profile: bool, yes: bool) -> None:
    """Delete a Cognito user and the user's SageMaker Studio profile.

    The profile named after the user goes first, with the spaces it owns and
    their apps, since SageMaker deletes a profile only after them; that can
    take a few minutes. A profile whose owner tag names another Cognito user
    (a name that matches this one ignoring case) is left in place. The files
    under /home/<username> on the Studio EFS stay. When the profile cannot
    be deleted the Cognito user is kept, so the command can be run again;
    --keep-studio-profile deletes only the Cognito user.
    """
    from botocore.exceptions import ClientError

    from ..analytics_user_mgmt import (
        StudioProfileRemovalError,
        admin_delete_user,
        admin_get_user_sub,
        delete_studio_user_profile,
        discover_studio_domain_id,
    )

    formatter = get_output_formatter(config)
    pool_id, region = _require_cognito_pool_id(config)

    if not yes:
        target = "" if keep_studio_profile else " and their SageMaker Studio profile"
        confirm(f"Delete Cognito user '{username}'{target}?", abort=True)

    studio: dict[str, Any] | None = None
    if not keep_studio_profile:
        try:
            owner_sub = admin_get_user_sub(pool_id, region, username)
        except ClientError as exc:
            formatter.print_error(f"Failed to delete user {username}: {_error_code(exc)}")
            sys.exit(1)
        domain_id = discover_studio_domain_id(region, config.project_name)
        if domain_id is None:
            formatter.print_error(
                f"Could not find the Studio domain of {config.project_name}-analytics, so "
                f"the Cognito user {username} was kept; pass --keep-studio-profile to "
                "delete only the Cognito user."
            )
            sys.exit(1)
        formatter.print_info(f"Deleting the Studio profile {username} with its spaces and apps...")
        try:
            studio = delete_studio_user_profile(region, domain_id, username, owner_sub)
        except ClientError as exc:
            formatter.print_error(
                f"Failed to delete the Studio profile {username}: {_error_code(exc)}. The "
                "Cognito user was kept; pass --keep-studio-profile to delete only the "
                "Cognito user."
            )
            sys.exit(1)
        except StudioProfileRemovalError as exc:
            formatter.print_error(f"{exc}. The Cognito user {username} was kept.")
            sys.exit(1)

    try:
        admin_delete_user(pool_id, region, username)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        formatter.print_error(f"Failed to delete user {username}: {error_code}")
        sys.exit(1)

    if config.output_format != "table":
        formatter.print(
            {"username": username, "cognito_user_deleted": True, "studio_profile": studio}
        )
        return
    formatter.print_success(f"Deleted Cognito user: {username}")
    for line in _studio_removal_lines(username, studio) if studio else []:
        formatter.print_info(line)


@users_cmd.command("set-password")
@click.option("--username", required=True, help="Cognito username whose password to change.")
@click.option(
    "--password",
    envvar="GCO_STUDIO_PASSWORD",
    help=(
        "New password (also read from $GCO_STUDIO_PASSWORD; prompted "
        "otherwise). Mutually exclusive with --generate-password."
    ),
)
@click.option(
    "--generate-password",
    is_flag=True,
    help=(
        "Generate a strong random password, set it, and print it once. "
        "Mutually exclusive with --password."
    ),
)
@click.option(
    "--temporary",
    is_flag=True,
    help=(
        "Set the password as temporary so the user is forced to change "
        "it on first login (Permanent=false). Default is permanent."
    ),
)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@pass_config
def users_set_password(
    config: Any,
    username: str,
    password: str | None,
    generate_password: bool,
    temporary: bool,
    yes: bool,
) -> None:
    """Change a Cognito user's password via AdminSetUserPassword.

    By default the new password is marked ``Permanent=true`` so the
    user can sign in directly with ``gco analytics studio login``
    without the ``NEW_PASSWORD_REQUIRED`` challenge. Pass
    ``--temporary`` to require the user to choose their own password
    on first sign-in.
    """
    from botocore.exceptions import ClientError

    from ..analytics_user_mgmt import admin_set_user_password, generate_strong_password

    formatter = get_output_formatter(config)

    if password and generate_password:
        formatter.print_error("--password and --generate-password are mutually exclusive")
        sys.exit(1)

    pool_id, region = _require_cognito_pool_id(config)

    if generate_password:
        new_password = generate_strong_password()
    elif password is not None:
        new_password = password
    else:
        prompt_kwargs: dict[str, Any] = {
            "hide_input": True,
            "confirmation_prompt": True,
        }
        if config.output_format != "table":
            prompt_kwargs["err"] = True
        new_password = prompt("New password", **prompt_kwargs)

    if not yes:
        qualifier = "temporary" if temporary else "permanent"
        confirm_kwargs: dict[str, Any] = {"abort": True}
        if config.output_format != "table":
            confirm_kwargs["err"] = True
        confirm(
            f"Set a new {qualifier} password for Cognito user '{username}'?",
            **confirm_kwargs,
        )

    try:
        admin_set_user_password(
            pool_id=pool_id,
            region=region,
            username=username,
            password=new_password,
            permanent=not temporary,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        formatter.print_error(f"Failed to set password for {username}: {error_code}")
        sys.exit(1)

    qualifier = "temporary" if temporary else "permanent"
    if config.output_format == "table":
        formatter.print_success(f"Password set ({qualifier}) for {username}")
        if generate_password:
            formatter.print_info(f"Generated password (printed exactly once): {new_password}")
    else:
        result: dict[str, Any] = {
            "password_set": True,  # nosec B105  # boolean flag, not a credential
            "username": username,
            "user_pool_id": pool_id,
            "region": region,
            "password_state": qualifier,
            "password_generated": generate_password,
            "password_source": "generated" if generate_password else "provided",
            "password_permanent": not temporary,
        }
        if generate_password:
            result["password"] = new_password
        formatter.print(result)


# ---------------------------------------------------------------------------
# Studio login subgroup
# ---------------------------------------------------------------------------


@analytics.group("studio")
@pass_config
def studio_cmd(config: Any) -> None:
    """SageMaker Studio helpers (login, etc.)."""


def _studio_tokens(
    formatter: Any,
    *,
    pool_id: str,
    client_id: str,
    username: str,
    password: str,
    new_password: str | None,
    region: str,
) -> dict[str, str]:
    """Sign ``username`` in, answering NEW_PASSWORD_REQUIRED; exit 1 when Cognito refuses."""
    from botocore.exceptions import ClientError

    from ..analytics_user_mgmt import (
        NewPasswordRequiredError,
        authenticate_with_password,
        complete_new_password_challenge,
    )

    try:
        return authenticate_with_password(
            pool_id=pool_id,
            client_id=client_id,
            username=username,
            password=password,
            region=region,
        )
    except NewPasswordRequiredError as challenge:
        session = challenge.session
    except ClientError as exc:
        formatter.print_error(f"Cognito authentication failed: {_error_code(exc)}")
        sys.exit(1)
    except ValueError as exc:
        formatter.print_error(f"Cognito authentication failed: {exc}")
        sys.exit(1)

    if new_password is None:
        if not _stdin_is_interactive():
            formatter.print_error(_FIRST_SIGN_IN_HINT)
            sys.exit(1)
        click.echo("This account still has its temporary password; choose a new one.", err=True)
        new_password = prompt("New password", hide_input=True, confirmation_prompt=True, err=True)
    try:
        return complete_new_password_challenge(
            pool_id=pool_id,
            client_id=client_id,
            username=username,
            new_password=new_password,
            session=session,
            region=region,
        )
    except ClientError as exc:
        # Cognito's message names the unmet password rule; it holds no secret.
        message = exc.response.get("Error", {}).get("Message")
        formatter.print_error(
            f"Cognito refused the new password: {_error_code(exc)}"
            + (f" ({message})" if message else "")
        )
        sys.exit(1)
    except ValueError as exc:
        formatter.print_error(f"Cognito authentication failed: {exc}")
        sys.exit(1)


@studio_cmd.command("login")
@click.option("--username", required=True, help="Cognito username to sign in with.")
@click.option(
    "--password",
    envvar="GCO_STUDIO_PASSWORD",
    help="Password (also read from $GCO_STUDIO_PASSWORD; prompted otherwise).",
)
@click.option(
    "--new-password",
    envvar="GCO_STUDIO_NEW_PASSWORD",
    help=(
        "New password for an account that still has its temporary one (also read "
        "from $GCO_STUDIO_NEW_PASSWORD; prompted on a terminal otherwise)."
    ),
)
@click.option("--api-url", help="Override the API Gateway base URL (otherwise auto-discovered).")
@click.option("--open", "open_browser", is_flag=True, help="Open the URL in the default browser.")
@pass_config
def studio_login(
    config: Any,
    username: str,
    password: str | None,
    new_password: str | None,
    api_url: str | None,
    open_browser: bool,
) -> None:
    """Sign in to SageMaker Studio through Cognito and print the presigned URL.

    The sign-in is cognito-idp:AdminInitiateAuth (ADMIN_USER_PASSWORD_AUTH),
    so it needs AWS credentials allowed to call it on the pool. An account
    that still has its temporary password chooses a new one first:
    --new-password, or a prompt on a terminal, answers Cognito's
    NEW_PASSWORD_REQUIRED challenge (cognito-idp:AdminRespondToAuthChallenge),
    and the new password becomes the account's password.
    """
    from ..analytics_user_mgmt import (
        discover_api_endpoint,
        discover_cognito_client_id,
        discover_cognito_pool_id,
        fetch_studio_url,
    )

    formatter = get_output_formatter(config)
    region = config.api_gateway_region
    project_name = config.project_name

    pool_id = discover_cognito_pool_id(region, project_name)
    client_id = discover_cognito_client_id(region, project_name)
    if not pool_id or not client_id:
        formatter.print_error(_stack_missing_message(config.project_name))
        sys.exit(1)

    api_base = (
        api_url
        or discover_api_endpoint(region, project_name)
        or os.environ.get("GCO_API_GATEWAY_URL")
    )
    if not api_base:
        formatter.print_error(
            "Could not resolve API Gateway endpoint — pass --api-url or deploy gco-api-gateway."
        )
        sys.exit(1)

    if password is None:
        # stderr, like every other prompt here: stdout carries only the URL.
        password = prompt("Password", hide_input=True, err=True)

    tokens = _studio_tokens(
        formatter,
        pool_id=pool_id,
        client_id=client_id,
        username=username,
        password=password,
        new_password=new_password,
        region=region,
    )

    # /studio/login requires a scope, so it takes the access token; the ID
    # token, which has no scope claim, is refused there with a 401.
    access_token = tokens.get("AccessToken")
    if not access_token:
        formatter.print_error("Cognito authentication failed: no AccessToken returned")
        sys.exit(1)

    try:
        # Poll until the Lambda returns HTTP 200 with the presigned URL.
        # First-time logins trigger user-profile provisioning (30-60s);
        # the Lambda returns HTTP 202 while the profile is pending.
        import time as _time

        max_wait = 120  # seconds
        poll_interval = 5  # seconds
        elapsed = 0
        url = ""

        while elapsed < max_wait:
            # Only the URL matters here; the presigned expiry is reported by
            # `analytics studio-url`, not by this open-in-browser path.
            url, _expires_in, _ = fetch_studio_url(api_base, access_token)
            if url:
                break
            # 202 -- profile still provisioning. Progress goes to stderr:
            # stdout carries only the URL, for `URL=$(gco analytics ...)`.
            if elapsed == 0:
                click.echo("  Waiting for user profile to provision...", nl=False, err=True)
            click.echo(".", nl=False, err=True)
            _time.sleep(poll_interval)
            elapsed += poll_interval

        if elapsed > 0 and url:
            click.echo(" ready", err=True)
        elif not url:
            click.echo("", err=True)
            formatter.print_error(
                f"User profile did not become ready within {max_wait}s. Try again in a minute."
            )
            sys.exit(2)
    except urllib.error.HTTPError as exc:
        from ..analytics_user_mgmt import studio_login_error_token

        correlation_id = exc.headers.get("x-amzn-RequestId") if exc.headers else "N/A"
        token = studio_login_error_token(exc)
        status = f"HTTP {exc.code} {token}" if token else f"HTTP {exc.code}"
        formatter.print_error(f"login failed: {status}, correlation_id={correlation_id or 'N/A'}")
        hint = _STUDIO_LOGIN_ERROR_HINTS.get(token or "")
        if hint:
            formatter.print_error(hint.format(username=username))
        elif exc.code == 401 and token is None:
            # API Gateway's own refusal, before the Lambda ran: the token was
            # signed in a moment ago, so the likeliest cause is an API
            # deployed before /studio/login required an access token.
            formatter.print_error(_STUDIO_LOGIN_UNAUTHORIZED_HINT.format(project=project_name))
        sys.exit(2)
    except urllib.error.URLError as exc:
        formatter.print_error(f"login failed: network error: {exc.reason!r}")
        sys.exit(2)
    except ValueError as exc:
        formatter.print_error(f"login failed: {exc}")
        sys.exit(2)

    # Print the URL on its own line for pipe-friendliness.
    click.echo(url)
    if open_browser:
        click.launch(url)


# ---------------------------------------------------------------------------
# Doctor subcommand
# ---------------------------------------------------------------------------


@analytics.command("doctor")
@pass_config
def analytics_doctor(config: Any) -> None:
    """Run pre-flight checks before `gco stacks deploy gco-analytics`.

    Exits non-zero on any failing check. Each check prints ``✓``/``✗``
    plus a short remediation line so the operator knows exactly what
    to fix. With -o json or -o yaml the output is one document,
    ``{"passed": bool, "checks": [{"name", "ok", "remediation"}, ...]}``.
    """
    from ..analytics_user_mgmt import (
        check_ssm_parameter,
        check_stack_complete,
        scan_orphan_analytics_resources,
    )
    from ..config import _load_cdk_json
    from ..stacks import _find_cdk_json

    formatter = get_output_formatter(config)
    table = config.output_format == "table"
    checks: list[dict[str, Any]] = []

    def _emit(name: str, ok: bool, remediation: str) -> None:
        checks.append({"name": name, "ok": ok, "remediation": "" if ok else remediation})
        if not table:
            return
        if ok:
            click.echo(f"  ✓ {name}")
        else:
            click.echo(f"  ✗ {name}")
            if remediation:
                click.echo(f"    → {remediation}")

    # 1. cdk.json parses
    cdk_json_path = _find_cdk_json()
    if cdk_json_path is None:
        _emit(
            "cdk.json present",
            False,
            "run `gco analytics doctor` from the project root (cdk.json not found).",
        )
    else:
        try:
            with open(cdk_json_path, encoding="utf-8") as fh:
                json.load(fh)
            _emit("cdk.json parses as JSON", True, "")
        except json.JSONDecodeError as exc:
            _emit(
                "cdk.json parses as JSON",
                False,
                f"fix malformed JSON at {cdk_json_path}: {exc.msg} (line {exc.lineno})",
            )

    # 2. Prerequisite stacks healthy
    for region, stack_name in (
        (config.global_region, f"{config.project_name}-global"),
        (config.api_gateway_region, f"{config.project_name}-api-gateway"),
    ):
        ok, remediation = check_stack_complete(region, stack_name)
        _emit(
            f"{stack_name} is deployed",
            ok,
            remediation or f"deploy with `gco stacks deploy {stack_name}`",
        )

    cdk_regions = _load_cdk_json()
    regional_regions = cdk_regions.get("regional", []) if isinstance(cdk_regions, dict) else []
    for region in regional_regions:
        stack_name = f"{config.project_name}-{region}"
        ok, remediation = check_stack_complete(region, stack_name)
        _emit(
            f"{stack_name} is deployed",
            ok,
            remediation or f"deploy with `gco stacks deploy {stack_name}`",
        )

    # 3. SSM cluster-shared-bucket parameters exist
    from gco.stacks.constants import cluster_shared_ssm_parameter_prefix

    ssm_prefix = cluster_shared_ssm_parameter_prefix(config.project_name)
    for suffix in ("name", "arn", "region"):
        param = f"{ssm_prefix}/{suffix}"
        ok, remediation = check_ssm_parameter(config.global_region, param)
        _emit(
            f"SSM parameter {param} exists",
            ok,
            remediation and f"deploy {config.project_name}-global first ({remediation})",
        )

    # 4. No orphaned retained analytics resources
    orphan_cmds = scan_orphan_analytics_resources(config.api_gateway_region, config.project_name)
    _emit(
        "no orphaned retained analytics resources",
        not orphan_cmds,
        "; ".join(orphan_cmds) if orphan_cmds else "",
    )

    passed = all(check["ok"] for check in checks)
    if not table:
        formatter.print({"passed": passed, "checks": checks})
    if not passed:
        formatter.print_error(
            "Doctor checks failed — see remediation lines above."
            if table
            else "Doctor checks failed — see each failed check's remediation."
        )
        sys.exit(1)
    formatter.print_success("All pre-flight checks passed.")
