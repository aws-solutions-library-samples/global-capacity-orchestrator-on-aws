"""Help-text and documentation contract for the EBS volume policy (task 9.2).

``test_docs_coverage.py`` guards that every command/tool *appears* in the
reference docs. This module guards the *semantics* those docs and the Click
help promise, so a future edit cannot quietly drift the operator-facing
contract away from the resolver in ``cli/volume_cleanup.py``:

* both ``gco stacks destroy`` and ``gco stacks destroy-all`` expose the two
  policy options (``--retain-volumes`` / ``--delete-volumes``) with their
  approved, command-specific semantics (Requirements 1.4-1.10);
* neither the help examples nor the operator docs imply ``--delete-volumes`` is
  required for ``destroy-all -y`` — the bare ``-y`` example is annotated as
  deleting eligible volumes and carries no delete flag (Requirements 1.4, 1.5);
* the live-validation ``--volume-scenario`` registry and the runbook stay in
  lockstep, and the E2E command lines the runbook documents match exactly what
  the shared command-aware resolver produces (Requirements 8.10, 8.11).

Pure text/CLI-introspection checks; nothing here touches AWS, a stack manager,
or a live validation run.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from cli.commands.stacks_cmd import stacks
from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    DestroyCommandKind,
    VolumePolicy,
    resolve_volume_cleanup_request,
)
from scripts.live_release_validation.volume_scenario import VOLUME_SCENARIO_SELECTIONS

REPO_ROOT = Path(__file__).resolve().parent.parent
_CLI_DOC = REPO_ROOT / "docs" / "CLI.md"
_README = REPO_ROOT / "README.md"
_RUNBOOK = REPO_ROOT / "docs" / "LIVE_RELEASE_VALIDATION.md"

_POLICY_OPTIONS = {"--retain-volumes", "--delete-volumes"}


def _norm(text: str) -> str:
    """Collapse all whitespace so wrapped help/doc phrases match reliably.

    Click rewraps docstrings (and Markdown wraps prose), so a promised phrase
    can be split across lines. Normalizing to single spaces lets the contract
    assert the phrase, not its incidental line breaks.
    """
    return " ".join(text.split())


def _option_names(command: click.Command) -> set[str]:
    names: set[str] = set()
    for param in command.params:
        if isinstance(param, click.Option):
            names.update(param.opts)
            names.update(param.secondary_opts)
    return names


def _help(*args: str) -> str:
    result = CliRunner().invoke(stacks, [*args, "--help"])
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture(scope="module")
def cli_doc() -> str:
    return _norm(_CLI_DOC.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def readme() -> str:
    return _norm(_README.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def runbook_raw() -> str:
    return _RUNBOOK.read_text(encoding="utf-8")


class TestBothCommandsExposePolicyOptions:
    """Requirements 1.4-1.10: the two policy options exist on both commands."""

    @pytest.mark.parametrize("command_name", ["destroy", "destroy-all"])
    def test_command_registers_both_policy_options(self, command_name: str) -> None:
        command = stacks.commands[command_name]

        assert _option_names(command) >= _POLICY_OPTIONS

    @pytest.mark.parametrize("command_name", ["destroy", "destroy-all"])
    def test_help_lists_both_policy_options(self, command_name: str) -> None:
        text = _help(command_name)

        assert "--retain-volumes" in text
        assert "--delete-volumes" in text

    @pytest.mark.parametrize("command_name", ["destroy", "destroy-all"])
    def test_policy_options_are_flags_without_a_value(self, command_name: str) -> None:
        command = stacks.commands[command_name]
        flags = {
            param.opts[0]: param
            for param in command.params
            if isinstance(param, click.Option) and param.opts[0] in _POLICY_OPTIONS
        }

        for name, option in flags.items():
            assert option.is_flag, f"{name} must remain a boolean flag"


class TestDestroyAllApprovedSemantics:
    """Requirements 1.4, 1.5, 1.6, 1.9: destroy-all -y implicit-delete contract."""

    def test_help_states_implicit_delete_authorization(self) -> None:
        text = _norm(_help("destroy-all"))

        assert "'gco stacks destroy-all -y' implicitly AUTHORIZES DELETION" in text
        assert "unless --retain-volumes is supplied" in text

    def test_help_states_delete_flag_is_not_required(self) -> None:
        text = _norm(_help("destroy-all"))

        assert (
            "--delete-volumes is not required and there is no separate volume prompt "
            "on this path" in text
        )

    def test_help_states_retain_overrides_the_implicit_delete(self) -> None:
        text = _norm(_help("destroy-all"))

        assert "Pass --retain-volumes to keep the volumes instead" in text
        assert "explicit retention overrides the implicit delete" in text

    def test_help_rejects_both_flags_together(self) -> None:
        text = _norm(_help("destroy-all"))

        assert (
            "Passing both --retain-volumes and --delete-volumes is rejected before "
            "any action" in text
        )


class TestSingleDestroyApprovedSemantics:
    """Requirements 1.1, 1.7, 1.9: single-stack destroy retains by default."""

    def test_help_states_retain_default_and_no_implicit_delete(self) -> None:
        text = _norm(_help("destroy"))

        assert "Volume policy for single-stack destroy defaults to RETAIN" in text
        assert "Deletion is never implicit here" in text

    def test_help_states_delete_flag_prompts_unless_yes(self) -> None:
        text = _norm(_help("destroy"))

        assert "pass --delete-volumes to delete eligible volumes" in text
        assert "prompts for an irreversible-data confirmation unless -y" in text

    def test_help_rejects_both_flags_together(self) -> None:
        text = _norm(_help("destroy"))

        assert (
            "Passing both --retain-volumes and --delete-volumes is rejected before "
            "any action" in text
        )


class TestExamplesDoNotImplyDeleteFlagIsRequired:
    """Requirements 1.4, 1.5: the bare ``destroy-all -y`` example deletes volumes.

    The regression this guards against is an example that pairs ``-y`` with
    ``--delete-volumes`` and so teaches operators the flag is needed. The bare
    ``-y`` example must be annotated as deleting eligible volumes with no delete
    flag sitting between the invocation and its comment.
    """

    def test_destroy_all_help_example_deletes_on_bare_yes(self) -> None:
        text = _norm(_help("destroy-all"))

        assert "gco stacks destroy-all -y # deletes eligible volumes" in text

    def test_destroy_all_help_example_uses_retain_to_keep_volumes(self) -> None:
        text = _norm(_help("destroy-all"))

        assert "gco stacks destroy-all -y --retain-volumes # keeps all volumes" in text

    def test_cli_doc_example_deletes_on_bare_yes(self, cli_doc: str) -> None:
        assert "gco stacks destroy-all -y # deletes eligible volumes" in cli_doc

    def test_readme_states_no_delete_flag_or_prompt(self, readme: str) -> None:
        assert "no `--delete-volumes` flag or extra prompt" in readme


class TestCliDocApprovedSemantics:
    """Requirements 1.4-1.10: docs/CLI.md documents the resolver contract."""

    def test_doc_lists_both_policy_options(self, cli_doc: str) -> None:
        assert "--retain-volumes" in cli_doc
        assert "--delete-volumes" in cli_doc

    def test_doc_states_implicit_delete_authorization(self, cli_doc: str) -> None:
        assert "implicitly authorizes deletion of eligible" in cli_doc
        assert "unless `--retain-volumes` is supplied" in cli_doc

    def test_doc_states_delete_flag_is_not_required(self, cli_doc: str) -> None:
        assert "`--delete-volumes` is **not** required" in cli_doc

    def test_doc_states_conflict_is_rejected(self, cli_doc: str) -> None:
        assert "rejected before any action" in cli_doc


class TestValidationRegistryDocsLockstep:
    """Requirements 8.10, 8.11: runbook scenarios and command lines match code.

    Two lockstep guards distinct from the volume-inventory action's
    export/runbook-row guard in ``test_live_validation_volume_inventory*``:
    every ``--volume-scenario`` selection the code accepts is documented, and
    the E2E command lines the runbook promises for the two live cases are
    exactly what the shared command-aware resolver produces.
    """

    def test_every_volume_scenario_selection_is_documented(self, runbook_raw: str) -> None:
        assert len(VOLUME_SCENARIO_SELECTIONS) >= 4, (
            f"sanity floor: only {len(VOLUME_SCENARIO_SELECTIONS)} scenario selections"
        )
        missing = [value for value in VOLUME_SCENARIO_SELECTIONS if f"`{value}`" not in runbook_raw]
        assert not missing, (
            "Volume-scenario selections missing from docs/LIVE_RELEASE_VALIDATION.md: "
            + ", ".join(missing)
        )

    def test_delete_case_command_line_matches_resolver(self, runbook_raw: str) -> None:
        # Requirement 8.10: the delete case exercises `destroy-all -y` with no
        # `--delete-volumes`, which the resolver treats as authorized delete.
        decision = resolve_volume_cleanup_request(
            command=DestroyCommandKind.ALL,
            retain_volumes=False,
            delete_volumes=False,
            yes=True,
        )
        assert decision.policy is VolumePolicy.DELETE
        assert decision.deletion_authorized is True
        assert decision.authorization_source is DeletionAuthorizationSource.DESTROY_ALL_WITH_YES
        assert decision.requires_volume_confirmation is False

        normalized = _norm(runbook_raw)
        assert "exercising `gco stacks destroy-all -y` (implicit delete" in normalized
        assert "**no** `--delete-volumes` flag" in normalized

    def test_retain_override_case_command_line_matches_resolver(self, runbook_raw: str) -> None:
        # Requirement 8.11: the retain-override case exercises
        # `destroy-all -y --retain-volumes`, which the resolver treats as retain.
        decision = resolve_volume_cleanup_request(
            command=DestroyCommandKind.ALL,
            retain_volumes=True,
            delete_volumes=False,
            yes=True,
        )
        assert decision.policy is VolumePolicy.RETAIN
        assert decision.deletion_authorized is False
        assert decision.requires_volume_confirmation is False

        normalized = _norm(runbook_raw)
        assert "exercising `gco stacks destroy-all -y --retain-volumes`" in normalized
