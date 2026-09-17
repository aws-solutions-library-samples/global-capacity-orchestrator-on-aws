"""`gco cluster doctor` — layered EKS access diagnosis.

The failures this exists to tell apart:

* kubectl timeouts — endpoint reachability (PRIVATE mode, or a public CIDR
  allowlist the caller is outside of);
* kubectl 'Unauthorized' — authentication (no EKS access entry);
* RBAC 'Forbidden' — authorization (entry with no associated policy);
* kubectl 'no such host' — a **stale kubeconfig context for a destroyed
  cluster**, which looks exactly like a private-endpoint problem and
  misdirected the original outage diagnosis.

The decision table lives in the pure ``diagnose`` function over a
``ClusterProbe``; the probes are thin subprocess seams tested with a patched
``subprocess.run``.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from cli import cluster_doctor
from cli.cluster_doctor import ClusterProbe, DoctorCheck, diagnose, endpoint_drift
from cli.main import cli

_ENDPOINT = "https://ABC123.gr7.us-east-1.eks.amazonaws.com"
_ROLE = "arn:aws:iam::123456789012:role/DeveloperRole"


def _probe(**overrides: Any) -> ClusterProbe:
    """A healthy public-cluster probe, overridable per test."""
    defaults: dict[str, Any] = {
        "cluster": "gco-us-east-1",
        "region": "us-east-1",
        "exists": True,
        "describe_error": None,
        "endpoint": _ENDPOINT,
        "public": True,
        "private": True,
        "public_cidrs": [],
        "caller_arn": _ROLE,
        "access_entries": [_ROLE],
        "associated_policies": [
            "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
        ],
        "kubeconfig_server": _ENDPOINT,
        "kubeconfig_tunnel_pinned": False,
    }
    defaults.update(overrides)
    return ClusterProbe(**defaults)


def _by_layer(checks: list[DoctorCheck]) -> dict[str, DoctorCheck]:
    return {check.layer: check for check in checks}


class TestDoctorCheckSerialization:
    def test_as_dict_includes_the_remedy_when_present(self) -> None:
        check = DoctorCheck(layer="reachability", status="warn", finding="PRIVATE", remedy="tunnel")
        assert check.as_dict() == {
            "layer": "reachability",
            "status": "warn",
            "finding": "PRIVATE",
            "remedy": "tunnel",
        }

    def test_as_dict_omits_an_absent_remedy(self) -> None:
        check = DoctorCheck(layer="authentication", status="ok", finding="entry exists")
        assert check.as_dict() == {
            "layer": "authentication",
            "status": "ok",
            "finding": "entry exists",
        }


class TestDiagnoseMissingCluster:
    def test_destroyed_cluster_with_stale_kubeconfig_names_both(self) -> None:
        checks = _by_layer(
            diagnose(
                _probe(
                    exists=False,
                    describe_error="An error occurred (ResourceNotFoundException): No cluster found",
                    kubeconfig_server="https://OLD456.gr7.us-east-1.eks.amazonaws.com",
                )
            )
        )
        assert checks["cluster"].status == "fail"
        assert "does not exist" in checks["cluster"].finding
        assert checks["kubeconfig"].status == "fail"
        assert "no such host" in checks["kubeconfig"].finding
        assert "stale context" in checks["kubeconfig"].finding
        assert "NOT a private-endpoint problem" in checks["kubeconfig"].finding

    def test_destroyed_cluster_without_kubeconfig_reports_only_the_cluster(self) -> None:
        checks = diagnose(
            _probe(
                exists=False,
                describe_error="ResourceNotFoundException",
                kubeconfig_server=None,
            )
        )
        assert [check.layer for check in checks] == ["cluster"]
        assert "gco stacks deploy" in (checks[0].remedy or "")

    def test_tunnel_pinned_kubeconfig_is_not_reported_stale(self) -> None:
        checks = diagnose(
            _probe(
                exists=False,
                describe_error="ResourceNotFoundException",
                kubeconfig_server="https://127.0.0.1:8443",
                kubeconfig_tunnel_pinned=True,
            )
        )
        assert [check.layer for check in checks] == ["cluster"]

    def test_other_describe_failures_are_unknown_not_destroyed(self) -> None:
        checks = diagnose(
            _probe(exists=False, describe_error="ExpiredTokenException: token expired")
        )
        assert [check.layer for check in checks] == ["cluster"]
        assert checks[0].status == "unknown"
        assert "credentials" in (checks[0].remedy or "")


class TestDiagnoseReachability:
    def test_public_restricted_names_the_allowlist(self) -> None:
        check = _by_layer(diagnose(_probe(public_cidrs=["203.0.113.0/24"])))["reachability"]
        assert check.status == "ok"
        assert "203.0.113.0/24" in check.finding
        assert "egress IP" in (check.remedy or "")

    def test_public_unrestricted_suggests_narrowing(self) -> None:
        check = _by_layer(diagnose(_probe(public_cidrs=[])))["reachability"]
        assert check.status == "ok"
        assert "0.0.0.0/0" in check.finding
        assert "gco stacks eks endpoint set" in (check.remedy or "")

    def test_open_allowlist_entry_reads_as_unrestricted(self) -> None:
        check = _by_layer(diagnose(_probe(public_cidrs=["0.0.0.0/0"])))["reachability"]
        assert "0.0.0.0/0" in check.finding

    def test_private_without_tunnel_warns_with_tunnel_remedy(self) -> None:
        check = _by_layer(diagnose(_probe(public=False, kubeconfig_server=None)))["reachability"]
        assert check.status == "warn"
        assert "PRIVATE" in check.finding
        assert "NOT an authentication problem" in check.finding
        assert "gco cluster tunnel --via-ssm auto" in (check.remedy or "")

    def test_private_with_tunnel_pin_is_ok(self) -> None:
        check = _by_layer(
            diagnose(
                _probe(
                    public=False,
                    kubeconfig_server="https://127.0.0.1:8443",
                    kubeconfig_tunnel_pinned=True,
                )
            )
        )["reachability"]
        assert check.status == "ok"
        assert "tunnel" in check.finding


class TestDiagnoseAuthenticationAndAuthorization:
    def test_missing_access_entry_fails_with_both_remedies(self) -> None:
        checks = _by_layer(diagnose(_probe(access_entries=["arn:aws:iam::1:role/Other"])))
        auth = checks["authentication"]
        assert auth.status == "fail"
        assert "Unauthorized" in auth.finding
        assert "gco stacks access" in (auth.remedy or "")
        assert "developer_access" in (auth.remedy or "")
        assert "authorization" not in checks  # dependent check skipped

    def test_present_entry_is_ok_and_policies_are_named(self) -> None:
        checks = _by_layer(diagnose(_probe()))
        assert checks["authentication"].status == "ok"
        assert checks["authorization"].status == "ok"
        assert "AmazonEKSClusterAdminPolicy" in checks["authorization"].finding

    def test_entry_without_policies_is_the_forbidden_case(self) -> None:
        check = _by_layer(diagnose(_probe(associated_policies=[])))["authorization"]
        assert check.status == "fail"
        assert "Forbidden" in check.finding
        assert "gco stacks access" in (check.remedy or "")

    def test_unknown_caller_is_reported(self) -> None:
        checks = _by_layer(diagnose(_probe(caller_arn=None, associated_policies=None)))
        assert checks["authentication"].status == "unknown"
        assert "authorization" not in checks

    def test_unlistable_entries_are_reported(self) -> None:
        checks = _by_layer(diagnose(_probe(access_entries=None, associated_policies=None)))
        assert checks["authentication"].status == "unknown"
        assert "ListAccessEntries" in (checks["authentication"].remedy or "")

    def test_unlistable_policies_are_reported(self) -> None:
        check = _by_layer(diagnose(_probe(associated_policies=None)))["authorization"]
        assert check.status == "unknown"


class TestDiagnoseKubeconfig:
    def test_no_context_warns_with_update_kubeconfig(self) -> None:
        check = _by_layer(diagnose(_probe(kubeconfig_server=None)))["kubeconfig"]
        assert check.status == "warn"
        assert "aws eks update-kubeconfig" in (check.remedy or "")

    def test_matching_context_is_ok(self) -> None:
        assert _by_layer(diagnose(_probe()))["kubeconfig"].status == "ok"

    def test_tunnel_pin_is_ok_and_named(self) -> None:
        check = _by_layer(
            diagnose(
                _probe(
                    kubeconfig_server="https://127.0.0.1:8443",
                    kubeconfig_tunnel_pinned=True,
                )
            )
        )["kubeconfig"]
        assert check.status == "ok"
        assert "tunnel" in check.finding

    def test_stale_context_on_a_live_cluster_is_distinguished(self) -> None:
        check = _by_layer(
            diagnose(_probe(kubeconfig_server="https://OLD456.gr7.us-east-1.eks.amazonaws.com"))
        )["kubeconfig"]
        assert check.status == "fail"
        assert "no such host" in check.finding
        assert "stale entry" in check.finding
        assert "update-kubeconfig" in (check.remedy or "")


class TestEndpointDrift:
    def test_configured_private_but_live_public(self) -> None:
        drift = endpoint_drift("PRIVATE", [], {"public": True, "public_cidrs": []})
        assert drift is not None
        assert "PRIVATE" in drift and "PUBLIC_AND_PRIVATE" in drift

    def test_configured_public_but_live_private(self) -> None:
        drift = endpoint_drift("PUBLIC_AND_PRIVATE", ["203.0.113.7/32"], {"public": False})
        assert drift == (
            "cdk.json eks_cluster.endpoint_access=PUBLIC_AND_PRIVATE but the live "
            "endpoint is PRIVATE"
        )

    def test_private_converged(self) -> None:
        assert endpoint_drift("PRIVATE", [], {"public": False, "public_cidrs": []}) is None

    def test_public_cidr_mismatch(self) -> None:
        drift = endpoint_drift(
            "PUBLIC_AND_PRIVATE",
            ["203.0.113.7/32"],
            {"public": True, "public_cidrs": ["198.51.100.0/24"]},
        )
        assert drift is not None
        assert "203.0.113.7/32" in drift

    def test_public_converged(self) -> None:
        assert (
            endpoint_drift(
                "PUBLIC_AND_PRIVATE",
                ["203.0.113.7/32"],
                {"public": True, "public_cidrs": ["203.0.113.7/32"]},
            )
            is None
        )

    def test_empty_configured_allowlist_matches_live_open_endpoint(self) -> None:
        assert (
            endpoint_drift(
                "PUBLIC_AND_PRIVATE", [], {"public": True, "public_cidrs": ["0.0.0.0/0"]}
            )
            is None
        )


def _completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["aws"], returncode=returncode, stdout=stdout, stderr=""
    )


class TestProbes:
    def test_caller_principal_normalizes_assumed_roles(self, monkeypatch) -> None:
        payload = json.dumps(
            {
                "Arn": "arn:aws:sts::123456789012:assumed-role/DeveloperRole/session",
                "Account": "123456789012",
            }
        )
        recorded: list[list[str]] = []

        def fake_run(args, capture_output, text):
            recorded.append(args)
            return _completed(stdout=payload)

        monkeypatch.setattr(cluster_doctor.subprocess, "run", fake_run)
        assert cluster_doctor.caller_principal_arn() == _ROLE
        assert recorded[0][:3] == ["aws", "sts", "get-caller-identity"]

    def test_caller_principal_passes_plain_arns_through(self, monkeypatch) -> None:
        payload = json.dumps({"Arn": _ROLE, "Account": "123456789012"})
        monkeypatch.setattr(cluster_doctor, "_run_aws", lambda args: _completed(stdout=payload))
        assert cluster_doctor.caller_principal_arn() == _ROLE

    @pytest.mark.parametrize(
        "result",
        [
            _completed(returncode=1),
            _completed(stdout="not json"),
            _completed(stdout="{}"),
        ],
    )
    def test_caller_principal_failure_modes_return_none(self, monkeypatch, result) -> None:
        monkeypatch.setattr(cluster_doctor, "_run_aws", lambda args: result)
        assert cluster_doctor.caller_principal_arn() is None

    def test_caller_principal_without_aws_cli_returns_none(self, monkeypatch) -> None:
        def raise_missing(args):
            raise FileNotFoundError("aws")

        monkeypatch.setattr(cluster_doctor, "_run_aws", raise_missing)
        assert cluster_doctor.caller_principal_arn() is None

    def test_list_access_entries_parses_the_arn_list(self, monkeypatch) -> None:
        payload = json.dumps({"accessEntries": [_ROLE, "arn:aws:iam::1:role/Other"]})
        monkeypatch.setattr(
            cluster_doctor.subprocess,
            "run",
            lambda args, capture_output, text: _completed(stdout=payload),
        )
        assert cluster_doctor.list_access_entries("gco-us-east-1", "us-east-1") == [
            _ROLE,
            "arn:aws:iam::1:role/Other",
        ]

    @pytest.mark.parametrize("result", [_completed(returncode=1), _completed(stdout="not json")])
    def test_list_access_entries_failure_modes_return_none(self, monkeypatch, result) -> None:
        monkeypatch.setattr(cluster_doctor, "_run_aws", lambda args: result)
        assert cluster_doctor.list_access_entries("c", "r") is None

    def test_list_access_entries_without_aws_cli_returns_none(self, monkeypatch) -> None:
        def raise_missing(args):
            raise FileNotFoundError("aws")

        monkeypatch.setattr(cluster_doctor, "_run_aws", raise_missing)
        assert cluster_doctor.list_access_entries("c", "r") is None

    def test_list_associated_policies_extracts_policy_arns(self, monkeypatch) -> None:
        payload = json.dumps(
            {
                "associatedAccessPolicies": [
                    {"policyArn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"},
                    {"unrelated": True},
                    "not-a-dict",
                ]
            }
        )
        monkeypatch.setattr(
            cluster_doctor.subprocess,
            "run",
            lambda args, capture_output, text: _completed(stdout=payload),
        )
        assert cluster_doctor.list_associated_access_policies("c", "us-east-1", _ROLE) == [
            "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"
        ]

    @pytest.mark.parametrize("result", [_completed(returncode=1), _completed(stdout="not json")])
    def test_list_associated_policies_failure_modes_return_none(self, monkeypatch, result) -> None:
        monkeypatch.setattr(cluster_doctor, "_run_aws", lambda args: result)
        assert cluster_doctor.list_associated_access_policies("c", "r", _ROLE) is None

    def test_list_associated_policies_without_aws_cli_returns_none(self, monkeypatch) -> None:
        def raise_missing(args):
            raise FileNotFoundError("aws")

        monkeypatch.setattr(cluster_doctor, "_run_aws", raise_missing)
        assert cluster_doctor.list_associated_access_policies("c", "r", _ROLE) is None


class TestKubeconfigClusterEntry:
    def _write_kubeconfig(self, tmp_path, server: str, *, tls_server_name: str | None = None):
        import yaml

        cluster: dict[str, Any] = {"server": server}
        if tls_server_name:
            cluster["tls-server-name"] = tls_server_name
        path = tmp_path / "kubeconfig"
        path.write_text(
            yaml.safe_dump(
                {
                    "clusters": [
                        {
                            "name": "arn:aws:eks:us-east-1:1:cluster/gco-us-east-1",
                            "cluster": cluster,
                        },
                        {"name": "unrelated", "cluster": {"server": "https://other"}},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_finds_the_entry_by_arn_suffix(self, tmp_path, monkeypatch) -> None:
        path = self._write_kubeconfig(tmp_path, _ENDPOINT)
        monkeypatch.setattr(cluster_doctor.kubectl_helpers, "_kubeconfig_file", lambda: path)
        assert cluster_doctor.kubeconfig_cluster_entry("gco-us-east-1") == (_ENDPOINT, False)

    def test_detects_a_tunnel_pin(self, tmp_path, monkeypatch) -> None:
        path = self._write_kubeconfig(
            tmp_path, "https://127.0.0.1:8443", tls_server_name="abc.eks.amazonaws.com"
        )
        monkeypatch.setattr(cluster_doctor.kubectl_helpers, "_kubeconfig_file", lambda: path)
        assert cluster_doctor.kubeconfig_cluster_entry("gco-us-east-1") == (
            "https://127.0.0.1:8443",
            True,
        )

    def test_missing_file_returns_none(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            cluster_doctor.kubectl_helpers, "_kubeconfig_file", lambda: tmp_path / "absent"
        )
        assert cluster_doctor.kubeconfig_cluster_entry("gco-us-east-1") is None

    def test_no_matching_entry_returns_none(self, tmp_path, monkeypatch) -> None:
        path = self._write_kubeconfig(tmp_path, _ENDPOINT)
        monkeypatch.setattr(cluster_doctor.kubectl_helpers, "_kubeconfig_file", lambda: path)
        assert cluster_doctor.kubeconfig_cluster_entry("gco-eu-west-1") is None


class TestProbeCluster:
    def test_collects_every_layer_for_a_live_cluster(self, monkeypatch) -> None:
        monkeypatch.setattr(
            cluster_doctor.kubectl_helpers,
            "describe_cluster_access",
            lambda cluster, region: {
                "endpoint": _ENDPOINT,
                "public": False,
                "private": True,
                "public_cidrs": [],
            },
        )
        monkeypatch.setattr(cluster_doctor, "caller_principal_arn", lambda: _ROLE)
        monkeypatch.setattr(cluster_doctor, "list_access_entries", lambda cluster, region: [_ROLE])
        monkeypatch.setattr(
            cluster_doctor,
            "list_associated_access_policies",
            lambda cluster, region, principal: ["arn:aws:eks::aws:cluster-access-policy/X"],
        )
        monkeypatch.setattr(
            cluster_doctor, "kubeconfig_cluster_entry", lambda cluster: (_ENDPOINT, False)
        )

        probe = cluster_doctor.probe_cluster("gco-us-east-1", "us-east-1")

        assert probe.exists is True
        assert probe.private is True and probe.public is False
        assert probe.caller_arn == _ROLE
        assert probe.access_entries == [_ROLE]
        assert probe.associated_policies == ["arn:aws:eks::aws:cluster-access-policy/X"]
        assert probe.kubeconfig_server == _ENDPOINT

    def test_missing_cluster_skips_the_dependent_probes(self, monkeypatch) -> None:
        def raise_not_found(cluster, region):
            raise RuntimeError("ResourceNotFoundException: no cluster")

        monkeypatch.setattr(
            cluster_doctor.kubectl_helpers, "describe_cluster_access", raise_not_found
        )
        monkeypatch.setattr(cluster_doctor, "caller_principal_arn", lambda: _ROLE)
        entries_called = MagicMock()
        monkeypatch.setattr(cluster_doctor, "list_access_entries", entries_called)
        monkeypatch.setattr(cluster_doctor, "kubeconfig_cluster_entry", lambda cluster: None)

        probe = cluster_doctor.probe_cluster("gco-us-east-1", "us-east-1")

        assert probe.exists is False
        assert "ResourceNotFoundException" in (probe.describe_error or "")
        entries_called.assert_not_called()
        assert probe.access_entries is None

    def test_policies_are_skipped_when_the_caller_has_no_entry(self, monkeypatch) -> None:
        monkeypatch.setattr(
            cluster_doctor.kubectl_helpers,
            "describe_cluster_access",
            lambda cluster, region: {
                "endpoint": _ENDPOINT,
                "public": True,
                "private": True,
                "public_cidrs": [],
            },
        )
        monkeypatch.setattr(cluster_doctor, "caller_principal_arn", lambda: _ROLE)
        monkeypatch.setattr(
            cluster_doctor,
            "list_access_entries",
            lambda cluster, region: ["arn:aws:iam::1:role/Other"],
        )
        policies_called = MagicMock()
        monkeypatch.setattr(cluster_doctor, "list_associated_access_policies", policies_called)
        monkeypatch.setattr(cluster_doctor, "kubeconfig_cluster_entry", lambda cluster: None)

        probe = cluster_doctor.probe_cluster("gco-us-east-1", "us-east-1")

        policies_called.assert_not_called()
        assert probe.associated_policies is None


class TestDoctorCommand:
    def _invoke(self, monkeypatch, checks: list[DoctorCheck], args: list[str] | None = None):
        probe = _probe()
        monkeypatch.setattr(cluster_doctor, "probe_cluster", lambda cluster, region: probe)
        monkeypatch.setattr(cluster_doctor, "diagnose", lambda p: checks)
        return CliRunner().invoke(
            cli, ["cluster", "doctor", "--region", "us-east-1", *(args or [])]
        )

    def test_healthy_cluster_exits_zero_and_prints_layers(self, monkeypatch) -> None:
        result = self._invoke(
            monkeypatch,
            [
                DoctorCheck(layer="reachability", status="ok", finding="public endpoint"),
                DoctorCheck(layer="authentication", status="ok", finding="entry exists"),
            ],
        )
        assert result.exit_code == 0
        assert "[reachability]" in result.output
        assert "[authentication]" in result.output

    def test_failing_layer_exits_nonzero_and_prints_the_remedy(self, monkeypatch) -> None:
        result = self._invoke(
            monkeypatch,
            [
                DoctorCheck(
                    layer="authentication",
                    status="fail",
                    finding="no EKS access entry",
                    remedy="gco stacks access -r us-east-1",
                )
            ],
        )
        assert result.exit_code == 1
        assert "no EKS access entry" in result.output
        assert "remedy: gco stacks access -r us-east-1" in result.output

    def test_json_output_is_the_structured_document(self, monkeypatch) -> None:
        result = self._invoke(
            monkeypatch,
            [DoctorCheck(layer="reachability", status="warn", finding="PRIVATE", remedy="tunnel")],
            args=None,
        )
        assert result.exit_code == 0

    def test_json_format_emits_check_dicts(self, monkeypatch) -> None:
        probe = _probe()
        monkeypatch.setattr(cluster_doctor, "probe_cluster", lambda cluster, region: probe)
        monkeypatch.setattr(
            cluster_doctor,
            "diagnose",
            lambda p: [
                DoctorCheck(layer="reachability", status="warn", finding="PRIVATE", remedy="tunnel")
            ],
        )
        result = CliRunner().invoke(
            cli, ["-o", "json", "cluster", "doctor", "--region", "us-east-1"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["cluster"] == "gco-us-east-1"
        assert payload["checks"] == [
            {
                "layer": "reachability",
                "status": "warn",
                "finding": "PRIVATE",
                "remedy": "tunnel",
            }
        ]
