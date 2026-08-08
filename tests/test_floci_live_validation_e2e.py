"""Floci E2E: the real live-validation harness against the emulator.

This is the deepest layer of the Floci suite: it executes ``gco release
validate --emulator-endpoint`` — the actual operator command — which runs
``python -m scripts.live_release_validation`` as a subprocess against the
emulator. Nothing in the harness is mocked. What runs for real:

* the CLI wrapper's derivation (SHA, branch, run id, private report dir);
* ``require_local_execution``'s verified emulator opt-in (this suite runs in
  GitHub Actions, where the harness otherwise refuses to start);
* preflight: git identity pinning, STS account verification against the
  emulator, EC2 enabled-region discovery, ``cdk list`` over the real cloud
  assembly, CDKToolkit health checks per target region, and the
  fresh-run refusal of pre-existing project stacks;
* baseline capture of protected CloudFormation/ECR state;
* guaranteed cleanup and final report writing.

Scope note — where this stops short of a real deployment: the ``deploy``
action drives the Node CDK CLI through ``StackManager``, which builds and
publishes container assets for six service images and an EKS cluster;
that depth is bounded by emulator fidelity and CI time, and is documented
in docs/FLOCI_TESTING.md rather than half-tested here. The preflight →
baseline path already executes every identity, inventory, and safety gate
the real run would, against real wire-protocol AWS state.

Requires the CDK toolchain (``node_modules/.bin`` on PATH and Lambda build
trees staged) in addition to the emulator; the floci-tests workflow
provides both. Locally: follow docs/FLOCI_TESTING.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import boto3
import pytest

from tests._floci import floci_test_markers, unique_name

pytestmark = [
    *floci_test_markers(),
    pytest.mark.skipif(
        shutil.which("cdk") is None,
        reason="the harness preflight shells out to the Node CDK CLI (npm ci first)",
    ),
]

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bootstrap_marker_template() -> str:
    """A minimal healthy CDKToolkit stand-in.

    Preflight requires a healthy stack NAMED CDKToolkit in every target
    region and records its ARN/status; it does not consume bootstrap
    resources until the deploy action. A real ``cdk bootstrap`` against the
    emulator is exercised separately by the deploy-depth stage in the
    workflow; here the marker keeps the preflight/baseline scope precise.
    """
    return json.dumps(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Description": "CDK bootstrap stand-in for emulator validation",
            "Resources": {
                "StagingBucket": {"Type": "AWS::S3::Bucket"},
            },
            "Outputs": {"BucketName": {"Value": {"Ref": "StagingBucket"}}},
        }
    )


@pytest.fixture(scope="module")
def emulated_topology(verified_floci_endpoint):
    """CDKToolkit marker stacks in every region cdk.json targets."""
    with (REPO_ROOT / "cdk.json").open() as fh:
        regions_config = json.load(fh)["context"]["deployment_regions"]
    regions = sorted(
        {
            regions_config["global"],
            regions_config["api_gateway"],
            regions_config["monitoring"],
            *regions_config["regional"],
        }
    )
    for region in regions:
        cloudformation = boto3.client("cloudformation", region_name=region)
        cloudformation.create_stack(
            StackName="CDKToolkit", TemplateBody=_bootstrap_marker_template()
        )
        cloudformation.get_waiter("stack_create_complete").wait(StackName="CDKToolkit")
    yield regions
    for region in regions:
        cloudformation = boto3.client("cloudformation", region_name=region)
        cloudformation.delete_stack(StackName="CDKToolkit")
        cloudformation.get_waiter("stack_delete_complete").wait(StackName="CDKToolkit")


def _run_release_validate(
    endpoint: str, report_root: Path, *extra: str, actions: str
) -> tuple[subprocess.CompletedProcess, Path]:
    run_id = unique_name("floci-e2e")
    report_dir = report_root / run_id
    env = dict(os.environ)
    # The harness subprocess needs the repo importable and the documented
    # Floci-gap shims active (see tests/_floci_sitecustomize/).
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), str(REPO_ROOT / "tests" / "_floci_sitecustomize")]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli.main",
            "release",
            "validate",
            "--expected-account",
            os.environ["AWS_ACCESS_KEY_ID"],
            "--i-understand-this-deploys-and-destroys-infrastructure",
            "--emulator-endpoint",
            endpoint,
            "--actions",
            actions,
            "--run-id",
            run_id,
            "--report-dir",
            str(report_dir),
            *extra,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    return result, report_dir


class TestPreflightAndBaselineAgainstTheEmulator:
    def test_full_identity_and_inventory_gates_pass_and_report(
        self, verified_floci_endpoint, emulated_topology, tmp_path_factory
    ):
        report_root = tmp_path_factory.mktemp("floci-e2e-reports")
        result, report_dir = _run_release_validate(
            verified_floci_endpoint, report_root, actions="preflight,baseline"
        )
        assert result.returncode == 0, (
            f"harness failed\nSTDOUT:\n{result.stdout[-4000:]}\nSTDERR:\n{result.stderr[-4000:]}"
        )

        report_path = report_dir / "live-release-validation.json"
        assert report_path.is_file(), "every initialized run must write the JSON report"
        report = json.loads(report_path.read_text())
        assert report["status"] == "partial", (
            "a subset run must report PARTIAL, never PASSED — passed is reserved for "
            f"the complete action registry; got {report['status']}"
        )
        results = {item["name"]: item["status"] for item in report["action_results"]}
        assert results == {"preflight": "passed", "baseline": "passed"}, results

        preflight = next(item for item in report["action_results"] if item["name"] == "preflight")
        details = preflight["details"]
        assert details["account"] == os.environ["AWS_ACCESS_KEY_ID"]
        assert details["bootstrap_stacks"], "CDKToolkit health must be recorded per region"
        assert details["target_stack_regions"], "cdk list must resolve the project stacks"
        assert all(name.startswith("gco") for name in details["target_stack_regions"]), details[
            "target_stack_regions"
        ]

        assert (report_dir / "live-release-validation.md").is_file()
        assert (report_dir / "checkpoint.json").is_file()

    def test_preflight_refuses_a_wrong_account_against_the_emulator(
        self, verified_floci_endpoint, emulated_topology, tmp_path_factory
    ):
        report_root = tmp_path_factory.mktemp("floci-e2e-wrong-account")
        run_id = unique_name("floci-e2e-neg")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(REPO_ROOT), str(REPO_ROOT / "tests" / "_floci_sitecustomize")]
        )
        wrong_account = "100000000001"
        assert wrong_account != os.environ["AWS_ACCESS_KEY_ID"]
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cli.main",
                "release",
                "validate",
                "--expected-account",
                wrong_account,
                "--i-understand-this-deploys-and-destroys-infrastructure",
                "--emulator-endpoint",
                verified_floci_endpoint,
                "--actions",
                "preflight",
                "--run-id",
                run_id,
                "--report-dir",
                str(report_root / run_id),
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
        assert result.returncode != 0, (
            "an account mismatch must fail the run — identity pinning is the core "
            "safety property and must hold against the emulator exactly as it does "
            "against real AWS"
        )
        report_path = report_root / run_id / "live-release-validation.json"
        assert report_path.is_file(), "even a failed preflight must write its report"
        report = json.loads(report_path.read_text())
        assert report["status"] == "failed"
        preflight = next(item for item in report["action_results"] if item["name"] == "preflight")
        assert "does not match expected" in (preflight.get("error") or ""), preflight
