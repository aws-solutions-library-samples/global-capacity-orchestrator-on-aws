"""`gco stacks eks endpoint set` and the endpoint drift/discoverability surface.

The command is an audited, synth-time-only cdk.json mutation following the
`gco stacks fsx` pattern. The properties pinned here:

* **Refusal to widen silently.** PUBLIC_AND_PRIVATE with no --cidr is an
  error, and an internet-open endpoint must be spelled out as
  --cidr 0.0.0.0/0. Malformed CIDRs are rejected.
* **Config only.** The command edits cdk.json and tells the operator to
  deploy; it never touches AWS.
* **Drift visibility.** `gco stacks status` on a base regional stack
  compares the configured endpoint against the live cluster and prints a
  drift warning; probe failures never break status.
* **Discoverability.** A successful regional deploy points at
  `gco stacks access`, `gco cluster tunnel`, and `gco cluster doctor`.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli.commands.stacks_cmd import (
    _print_cluster_access_hint,
    _print_eks_endpoint_drift,
    _valid_cidr,
)
from cli.main import cli


def _cdk_json(tmp_path: Any, context: dict[str, Any] | None = None) -> Any:
    path = tmp_path / "cdk.json"
    path.write_text(
        json.dumps({"context": context or {"eks_cluster": {"endpoint_access": "PRIVATE"}}}),
        encoding="utf-8",
    )
    return path


def _written_context(path: Any) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))["context"]


class TestValidCidr:
    def test_accepts_normal_cidrs(self) -> None:
        assert _valid_cidr("203.0.113.7/32")
        assert _valid_cidr("0.0.0.0/0")

    def test_rejects_garbage(self) -> None:
        assert not _valid_cidr("203.0.113.7")  # no prefix
        assert not _valid_cidr("256.0.0.1/24")  # octet out of range
        assert not _valid_cidr("10.0.0.0/33")  # prefix out of range
        assert not _valid_cidr("corp-office")


class TestEndpointSet:
    def _invoke(self, args: list[str], tmp_path: Any, context: dict[str, Any] | None = None):
        path = _cdk_json(tmp_path, context)
        with patch("cli.stacks._find_cdk_json", return_value=path):
            result = CliRunner().invoke(cli, ["stacks", "eks", "endpoint", "set", *args])
        return result, path

    def test_public_without_cidrs_is_refused(self, tmp_path: Any) -> None:
        result, path = self._invoke(["PUBLIC_AND_PRIVATE", "-y"], tmp_path)
        assert result.exit_code == 1
        assert "Refusing to widen" in result.output
        assert _written_context(path)["eks_cluster"] == {"endpoint_access": "PRIVATE"}

    def test_invalid_cidr_is_rejected(self, tmp_path: Any) -> None:
        result, path = self._invoke(["PUBLIC_AND_PRIVATE", "--cidr", "corp-office", "-y"], tmp_path)
        assert result.exit_code == 1
        assert "Invalid CIDR" in result.output
        assert _written_context(path)["eks_cluster"] == {"endpoint_access": "PRIVATE"}

    def test_public_with_explicit_cidrs_is_written(self, tmp_path: Any) -> None:
        result, path = self._invoke(
            ["PUBLIC_AND_PRIVATE", "--cidr", "203.0.113.7/32", "--cidr", "198.51.100.0/24", "-y"],
            tmp_path,
        )
        assert result.exit_code == 0
        written = _written_context(path)["eks_cluster"]
        assert written["endpoint_access"] == "PUBLIC_AND_PRIVATE"
        assert written["public_access_cidrs"] == ["203.0.113.7/32", "198.51.100.0/24"]
        assert "Config only" in result.output
        assert "gco stacks deploy" in result.output

    def test_internet_open_must_be_spelled_out(self, tmp_path: Any) -> None:
        result, path = self._invoke(["PUBLIC_AND_PRIVATE", "--cidr", "0.0.0.0/0", "-y"], tmp_path)
        assert result.exit_code == 0
        assert _written_context(path)["eks_cluster"]["public_access_cidrs"] == ["0.0.0.0/0"]

    def test_private_needs_no_cidrs(self, tmp_path: Any) -> None:
        result, path = self._invoke(
            ["PRIVATE", "-y"],
            tmp_path,
            context={
                "eks_cluster": {
                    "endpoint_access": "PUBLIC_AND_PRIVATE",
                    "public_access_cidrs": ["203.0.113.7/32"],
                }
            },
        )
        assert result.exit_code == 0
        written = _written_context(path)["eks_cluster"]
        assert written["endpoint_access"] == "PRIVATE"
        # The allowlist is preserved for a later flip back to public.
        assert written["public_access_cidrs"] == ["203.0.113.7/32"]

    def test_private_with_cidrs_notes_they_are_stored_for_later(self, tmp_path: Any) -> None:
        result, path = self._invoke(["PRIVATE", "--cidr", "203.0.113.7/32", "-y"], tmp_path)
        assert result.exit_code == 0
        assert "later flip" in result.output
        assert _written_context(path)["eks_cluster"]["public_access_cidrs"] == ["203.0.113.7/32"]

    def test_confirmation_prompt_aborts_without_writing(self, tmp_path: Any) -> None:
        path = _cdk_json(tmp_path)
        with patch("cli.stacks._find_cdk_json", return_value=path):
            result = CliRunner().invoke(
                cli,
                ["stacks", "eks", "endpoint", "set", "PRIVATE"],
                input="n\n",
            )
        assert result.exit_code != 0
        assert _written_context(path)["eks_cluster"] == {"endpoint_access": "PRIVATE"}

    def test_missing_cdk_json_is_a_clean_error(self, tmp_path: Any) -> None:
        with patch("cli.stacks._find_cdk_json", return_value=None):
            result = CliRunner().invoke(cli, ["stacks", "eks", "endpoint", "set", "PRIVATE", "-y"])
        assert result.exit_code == 1
        assert "cdk.json not found" in result.output


class TestEndpointDriftInStatus:
    def _formatter(self) -> MagicMock:
        return MagicMock()

    def _config(self) -> MagicMock:
        config = MagicMock()
        config.project_name = "gco"
        return config

    def test_non_regional_stacks_are_skipped(self, monkeypatch) -> None:
        formatter = self._formatter()
        describe = MagicMock()
        monkeypatch.setattr("cli.kubectl_helpers.describe_cluster_access", describe)
        _print_eks_endpoint_drift(formatter, self._config(), "gco-global", "us-east-2")
        describe.assert_not_called()
        formatter.print_warning.assert_not_called()

    def test_drift_is_reported_with_the_converge_command(self, monkeypatch) -> None:
        formatter = self._formatter()
        monkeypatch.setattr(
            "cli.stacks.get_eks_cluster_config",
            lambda: {"endpoint_access": "PRIVATE", "public_access_cidrs": []},
        )
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access",
            lambda cluster, region: {
                "endpoint": "https://x",
                "public": True,
                "private": True,
                "public_cidrs": [],
            },
        )
        _print_eks_endpoint_drift(formatter, self._config(), "gco-us-east-1", "us-east-1")
        warning = formatter.print_warning.call_args.args[0]
        assert "Config drift" in warning
        assert "gco stacks deploy gco-us-east-1 -y" in warning

    def test_converged_endpoint_prints_nothing(self, monkeypatch) -> None:
        formatter = self._formatter()
        monkeypatch.setattr(
            "cli.stacks.get_eks_cluster_config",
            lambda: {"endpoint_access": "PRIVATE", "public_access_cidrs": []},
        )
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access",
            lambda cluster, region: {
                "endpoint": "https://x",
                "public": False,
                "private": True,
                "public_cidrs": [],
            },
        )
        _print_eks_endpoint_drift(formatter, self._config(), "gco-us-east-1", "us-east-1")
        formatter.print_warning.assert_not_called()

    def test_probe_failures_never_break_status(self, monkeypatch) -> None:
        formatter = self._formatter()

        def raise_error(cluster, region):
            raise RuntimeError("describe failed")

        monkeypatch.setattr("cli.kubectl_helpers.describe_cluster_access", raise_error)
        _print_eks_endpoint_drift(formatter, self._config(), "gco-us-east-1", "us-east-1")
        formatter.print_warning.assert_not_called()


class TestClusterAccessHint:
    def _config(self) -> MagicMock:
        config = MagicMock()
        config.project_name = "gco"
        return config

    def test_regional_stack_gets_the_access_pointers(self) -> None:
        formatter = MagicMock()
        _print_cluster_access_hint(formatter, self._config(), "gco-us-east-1")
        hint = formatter.print_info.call_args.args[0]
        assert "gco stacks access -r us-east-1" in hint
        assert "gco cluster tunnel" in hint
        assert "gco cluster doctor" in hint

    def test_non_cluster_stacks_get_no_hint(self) -> None:
        for stack in (
            "gco-global",
            "gco-api-gateway",
            "gco-monitoring",
            "gco-regional-api-us-east-1",
            "unrelated-stack",
            "gco-",
        ):
            formatter = MagicMock()
            _print_cluster_access_hint(formatter, self._config(), stack)
            formatter.print_info.assert_not_called()


class TestEksClusterConfigHelpers:
    def test_get_merges_defaults(self, tmp_path: Any) -> None:
        from cli.stacks import get_eks_cluster_config

        path = _cdk_json(tmp_path, {"eks_cluster": {"endpoint_access": "PUBLIC_AND_PRIVATE"}})
        with patch("cli.stacks._find_cdk_json", return_value=path):
            config = get_eks_cluster_config()
        assert config["endpoint_access"] == "PUBLIC_AND_PRIVATE"
        assert config["public_access_cidrs"] == []
        assert config["developer_access"] == []

    def test_update_preserves_unrelated_keys(self, tmp_path: Any) -> None:
        from cli.stacks import update_eks_cluster_config

        path = _cdk_json(
            tmp_path,
            {
                "eks_cluster": {
                    "endpoint_access": "PRIVATE",
                    "developer_access": [{"principal_arn": "arn:aws:iam::1:role/Dev"}],
                }
            },
        )
        with patch("cli.stacks._find_cdk_json", return_value=path):
            update_eks_cluster_config({"endpoint_access": "PUBLIC_AND_PRIVATE"})
        written = _written_context(path)["eks_cluster"]
        assert written["endpoint_access"] == "PUBLIC_AND_PRIVATE"
        assert written["developer_access"] == [{"principal_arn": "arn:aws:iam::1:role/Dev"}]
