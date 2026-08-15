"""Offline contracts for CI and runtime artifact provenance controls."""

import ast
import re
import stat
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _workflow_step(relative_path: str, step_name: str) -> str:
    content = _read(relative_path)
    match = re.search(
        rf"^      - name: {re.escape(step_name)}\n.*?(?=^      - |\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"workflow step not found: {relative_path}: {step_name}"
    return match.group(0)


@pytest.mark.parametrize(
    ("relative_path", "version", "sha256"),
    [
        (
            ".github/workflows/lint.yml",
            "1.7.12",
            "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
        ),
        (
            ".github/workflows/integration-tests.yml",
            "v4.2.3",
            "e9b88b4ee95b18c706839c28d3a0220e5bc470e9cd9262410c90793c45ff8b7c",
        ),
        (
            ".github/workflows/integration-tests.yml",
            "v0.8.0",
            "9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883",
        ),
        (
            ".github/workflows/integration-tests.yml",
            "v3.32.1",
            "a1df919d9721cf667accdc3e72848911b0cb25cfab7d2478ad0c996302c95744",
        ),
        (
            ".github/workflows/integration-tests.yml",
            "v0.9.0",
            "1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b",
        ),
        (
            ".github/workflows/deps-scan.yml",
            "v4.2.3",
            "e9b88b4ee95b18c706839c28d3a0220e5bc470e9cd9262410c90793c45ff8b7c",
        ),
        (
            ".github/workflows/deps-scan.yml",
            "v1.36.3",
            "ebbd080e7c2e275093b55915722043257eb24004363e20acb3c4d71919f88336",
        ),
        (
            "lambda/helm-installer/Dockerfile",
            "v4.2.3",
            "e9b88b4ee95b18c706839c28d3a0220e5bc470e9cd9262410c90793c45ff8b7c",
        ),
        (
            "lambda/helm-installer/Dockerfile",
            "v1.36.3",
            "ebbd080e7c2e275093b55915722043257eb24004363e20acb3c4d71919f88336",
        ),
    ],
)
def test_downloaded_release_assets_have_committed_checksums(
    relative_path: str,
    version: str,
    sha256: str,
) -> None:
    content = _read(relative_path)

    assert version in content
    assert sha256 in content
    assert "sha256sum -c -" in content


@pytest.mark.parametrize(
    (
        "relative_path",
        "step_name",
        "version_declaration",
        "checksum_declaration",
        "download_fragment",
        "verification_command",
    ),
    [
        (
            ".github/workflows/lint.yml",
            "Install pinned actionlint",
            'ACTIONLINT_VERSION: "1.7.12"',
            'ACTIONLINT_SHA256: "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"',
            "actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz",
            'echo "${ACTIONLINT_SHA256}  ${archive}" | sha256sum -c -',
        ),
        (
            ".github/workflows/integration-tests.yml",
            "Install Helm",
            'HELM_VERSION: "v4.2.3"',
            'HELM_SHA256: "e9b88b4ee95b18c706839c28d3a0220e5bc470e9cd9262410c90793c45ff8b7c"',
            "helm-${HELM_VERSION}-linux-amd64.tar.gz",
            'echo "${HELM_SHA256}  ${archive}" | sha256sum -c -',
        ),
        (
            ".github/workflows/integration-tests.yml",
            "Install pinned kubeconform",
            'KUBECONFORM_VERSION: "v0.8.0"',
            'KUBECONFORM_SHA256: "9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883"',
            "kubeconform-linux-amd64.tar.gz",
            'echo "${KUBECONFORM_SHA256}  ${archive}" | sha256sum -c -',
        ),
        (
            ".github/workflows/integration-tests.yml",
            "Install Calico for NetworkPolicy enforcement",
            'CALICO_VERSION: "v3.32.1"',
            'CALICO_SHA256: "a1df919d9721cf667accdc3e72848911b0cb25cfab7d2478ad0c996302c95744"',
            "projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml",
            'echo "${CALICO_SHA256}  ${calico_manifest}" | sha256sum -c -',
        ),
        (
            ".github/workflows/integration-tests.yml",
            "Install Metrics Server for HPA reconciliation",
            'METRICS_SERVER_VERSION: "v0.9.0"',
            'METRICS_SERVER_SHA256: "1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b"',
            "metrics-server/releases/download/${METRICS_SERVER_VERSION}/components.yaml",
            'echo "${METRICS_SERVER_SHA256}  ${metrics_manifest}" | sha256sum -c -',
        ),
        (
            ".github/workflows/deps-scan.yml",
            "Install pinned Helm",
            'HELM_VERSION: "v4.2.3"',
            'HELM_SHA256: "e9b88b4ee95b18c706839c28d3a0220e5bc470e9cd9262410c90793c45ff8b7c"',
            "helm-${HELM_VERSION}-linux-amd64.tar.gz",
            'echo "${HELM_SHA256}  ${archive}" | sha256sum -c -',
        ),
        (
            ".github/workflows/deps-scan.yml",
            "Install pinned kubectl",
            'KUBECTL_VERSION: "v1.36.3"',
            'KUBECTL_SHA256: "ebbd080e7c2e275093b55915722043257eb24004363e20acb3c4d71919f88336"',
            "dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl",
            'echo "${KUBECTL_SHA256}  ${binary}" | sha256sum -c -',
        ),
    ],
)
def test_workflow_checksum_is_bound_to_its_download_step(
    relative_path: str,
    step_name: str,
    version_declaration: str,
    checksum_declaration: str,
    download_fragment: str,
    verification_command: str,
) -> None:
    workflow = _read(relative_path)
    step = _workflow_step(relative_path, step_name)

    assert version_declaration in workflow
    assert checksum_declaration in workflow
    assert download_fragment in step
    assert verification_command in step


def test_workflows_do_not_execute_mutable_remote_installers() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    )

    assert "get.docker.com" not in workflows
    assert "bash <(curl" not in workflows
    assert "raw.githubusercontent.com/rhysd/actionlint/main" not in workflows


def test_kind_manifests_are_authenticated_before_local_apply() -> None:
    workflow = _read(".github/workflows/integration-tests.yml")

    assert not re.search(
        r"kubectl\s+apply\s+-f\s+(?:\\\s*)?[\"']?https://",
        workflow,
    )
    assert 'kubectl apply -f "${calico_manifest}"' in workflow
    assert 'kubectl apply -f "${metrics_manifest}"' in workflow
    assert 'echo "${CALICO_SHA256}  ${calico_manifest}" | sha256sum -c -' in workflow
    assert 'echo "${METRICS_SERVER_SHA256}  ${metrics_manifest}" | sha256sum -c -' in workflow


def test_finch_repository_key_is_pinned_by_primary_fingerprint() -> None:
    workflow = _read(".github/workflows/integration-tests.yml")

    assert "C97195B13509CD7BD64D7F085E9EEE296292ACB8" in workflow
    assert 'primary_fingerprints[@]}" -ne 1' in workflow
    assert "gpg --batch --show-keys --with-colons" in workflow


def test_dependency_scan_credentials_are_default_branch_only() -> None:
    workflow = _read(".github/workflows/deps-scan.yml")

    assert (
        "if: github.ref_type == 'branch' && "
        "github.ref_name == github.event.repository.default_branch"
    ) in workflow
    assert "persist-credentials: false" in workflow
    assert "persist-credentials: true" not in workflow
    assert "steps.scan.outputs.scan_complete == 'true'" in workflow


def test_dependency_scanner_records_incomplete_queries_before_issue_closure() -> None:
    scanner = _read(".github/scripts/dependency-scan.sh")

    assert "INCOMPLETE_REASONS_FILE=" in scanner
    assert "mark_scan_incomplete()" in scanner
    assert "dependency_scan_is_complete" in scanner
    assert 'echo "scan_complete=$SCAN_COMPLETE"' in scanner
    assert scanner.count("mark_scan_incomplete ") >= 20


def test_accelerator_operational_errors_always_mark_the_scan_incomplete() -> None:
    scanner = _read(".github/scripts/dependency-scan.sh")
    section = scanner[
        scanner.index("# Accelerator catalog and Karpenter NodePools") : scanner.index(
            "# Summary + Markdown report"
        )
    ]
    wrapper = section[
        section.index("record_accelerator_operational_error()") : section.index(
            "python3 scripts/accelerator_catalog.py validate"
        )
    ]

    assert 'mark_scan_incomplete "${title}: ${detail}"' in wrapper
    assert len(re.findall(r"^\s+record_accelerator_operational_error ", section, re.MULTILINE)) == 5
    assert len(re.findall(r"^\s+write_accelerator_operational_report ", section, re.MULTILINE)) == 1


def test_new_authenticated_pins_are_in_monthly_drift_inventory() -> None:
    scanner = _read(".github/scripts/dependency-scan.sh")

    assert "ACTIONLINT_PIN=" in scanner
    assert '"rhysd/actionlint"' in scanner
    assert "CALICO_PIN=" in scanner
    assert '"projectcalico/calico"' in scanner
    assert "extract_python_string_constant" in scanner
    assert "AWS_CLI_IMAGE gco/services/inference_monitor.py" in scanner
    # The digest-freshness mechanics moved into shared lib helpers so every
    # digest-pinned image (AWS CLI runtime + live-validation smoke images)
    # gets the same committed-vs-published comparison.
    library = _read(".github/scripts/lib_dependency_scan.sh")
    assert "skopeo inspect --raw" in library
    assert "split_pinned_image_ref" in library
    assert "published_manifest_digest" in library
    assert "check_pinned_digest" in scanner
    assert 'check_pinned_digest "$AWS_CLI_RUNTIME_IMAGE"' in scanner
    assert "scripts/live_release_validation/manifests/" in scanner
    assert 'if [ "$committed" != "$published" ]; then' in scanner


def test_dependency_scanner_remains_directly_executable() -> None:
    mode = (ROOT / ".github/scripts/dependency-scan.sh").stat().st_mode

    assert mode & stat.S_IXUSR


def test_incomplete_reports_do_not_claim_zero_count_surfaces_are_current() -> None:
    scanner = _read(".github/scripts/dependency-scan.sh")

    assert "No drift was found in completed checks, but the scan is incomplete." in scanner
    assert 'label="no drift found (incomplete scan)"' in scanner
    assert "Zero-count surfaces are provisional, not confirmed current." in scanner


def test_release_publishes_branch_and_tag_atomically() -> None:
    workflow = _read(".github/workflows/release.yml")

    assert ('git push --atomic origin "HEAD:${GITHUB_REF}" "refs/tags/v${NEW_VERSION}"') in workflow
    assert 'git push origin "HEAD:${GITHUB_REF}"' not in workflow
    assert 'git push origin "v${NEW_VERSION}"' not in workflow


def test_model_sync_uses_an_immutable_official_aws_cli_image() -> None:
    source = _read("gco/services/inference_monitor.py")
    module = ast.parse(source)
    image = next(
        node.value.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "AWS_CLI_IMAGE" for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )

    assert "amazon/aws-cli:latest" not in source
    assert re.fullmatch(
        r"public\.ecr\.aws/aws-cli/aws-cli:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}",
        image,
    )


def test_actionlint_download_uses_the_published_amd64_asset() -> None:
    workflow = _read(".github/workflows/lint.yml")

    assert "actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" in workflow
    assert "actionlint_${ACTIONLINT_VERSION}_linux_x86_64.tar.gz" not in workflow


def test_helm_installer_checksums_are_non_overridable_trust_anchors() -> None:
    dockerfile = _read("lambda/helm-installer/Dockerfile")
    helm_section = dockerfile[
        dockerfile.index("# Install Helm") : dockerfile.index("# Install kubectl")
    ]
    kubectl_section = dockerfile[
        dockerfile.index("# Install kubectl") : dockerfile.index("# Install Python dependencies")
    ]

    assert "ARG HELM_SHA256" not in dockerfile
    assert "ARG KUBECTL_SHA256" not in dockerfile
    assert "helm-v4.2.3-linux-amd64.tar.gz" in helm_section
    assert (
        "e9b88b4ee95b18c706839c28d3a0220e5bc470e9cd9262410c90793c45ff8b7c  /tmp/helm.tar.gz"
    ) in helm_section
    assert "release/v1.36.3/bin/linux/amd64/kubectl" in kubectl_section
    assert (
        "ebbd080e7c2e275093b55915722043257eb24004363e20acb3c4d71919f88336  /tmp/kubectl"
    ) in kubectl_section


def test_incomplete_dependency_scan_ends_with_a_failing_step() -> None:
    workflow = _read(".github/workflows/deps-scan.yml")
    failure_step = workflow.index("- name: Fail an incomplete dependency scan")

    assert failure_step > workflow.index("- name: Close the resolved drift issue")
    failure_contract = workflow[failure_step:]
    assert "always()" in failure_contract
    assert "steps.scan.outputs.scan_complete != 'true'" in failure_contract
    assert "exit 1" in failure_contract
    assert "SCAN_COMPLETE: ${{ steps.scan.outputs.scan_complete }}" in workflow
    assert "The report is partial because one or more checks were incomplete" in workflow


def test_workflows_invoke_behaviorally_tested_runtime_verifiers() -> None:
    lambda_step = _workflow_step(
        ".github/workflows/integration-tests.yml", "Import each Lambda handler"
    )
    helm_step = _workflow_step(
        ".github/workflows/integration-tests.yml", "Verify helm + kubectl binaries"
    )
    dev_step = _workflow_step(
        ".github/workflows/integration-tests.yml", "Verify pinned toolchain versions"
    )

    assert "python3 .github/scripts/verify_lambda_imports.py" in lambda_step
    assert "verify_container_tool_versions.py helm-installer --image helm-installer:ci" in (
        " ".join(helm_step.split())
    )
    assert "verify_container_tool_versions.py dev --image gco-dev" in " ".join(dev_step.split())


def test_dev_container_matrix_keeps_native_amd64_and_arm64_coverage() -> None:
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))
    rows = workflow["jobs"]["integration-docker-dev-container"]["strategy"]["matrix"]["include"]
    architecture_contract = {
        row["arch"]: (row["runner"], row["expected-uname"], row["elf-e-machine"]) for row in rows
    }

    assert architecture_contract == {
        "amd64": ("ubuntu-latest", "x86_64", "0x3E"),
        "arm64": ("ubuntu-24.04-arm", "aarch64", "0xB7"),
    }


def test_direct_docker_scanners_are_prepulled_with_retry() -> None:
    workflow = yaml.safe_load(_read(".github/workflows/security.yml"))
    scanner_jobs = {
        "security-trufflehog-secrets": "trufflesecurity/trufflehog:3.96.0",
        "security-gitleaks-secrets": "zricethezav/gitleaks:v8.30.1",
        "security-checkov-iac": "bridgecrew/checkov:3.2.524",
        "security-kics-iac": "checkmarx/kics:v2.1.20",
    }

    for job_name, image in scanner_jobs.items():
        steps = workflow["jobs"][job_name]["steps"]
        pull_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("uses") == "./.github/actions/docker-pull-with-retry"
            and step.get("with", {}).get("images") == image
        )
        run_index = next(index for index, step in enumerate(steps) if image in step.get("run", ""))
        assert pull_index < run_index, f"{job_name} must pre-pull {image} before scanning"


def _job_env_pins(workflow: dict) -> dict[str, dict[str, str]]:
    """Map job name -> its job-level ``env`` pins (``*_VERSION`` / ``*_SHA256``).

    Job-scoped rather than workflow-scoped on purpose: this repo pins CI
    tooling per job so each one declares what it installs, which means the
    same pin name can legitimately appear several times — and can therefore
    silently disagree.
    """
    pins: dict[str, dict[str, str]] = {}
    for name, job in (workflow.get("jobs") or {}).items():
        env = (job or {}).get("env") or {}
        pins[name] = {
            key: str(value) for key, value in env.items() if key.endswith(("_VERSION", "_SHA256"))
        }
    return pins


def test_repeated_workflow_pins_agree_across_jobs() -> None:
    """A tool pinned by more than one job must be pinned to ONE value.

    integration-tests.yml installs the same tooling in several jobs (Helm in
    charts-valid and examples-smoke; Calico in cluster-e2e and
    examples-smoke), and the per-step checksum tests elsewhere in this file
    are substring assertions — they are satisfied by the FIRST matching
    declaration and cannot see a second one that drifted. Two jobs running
    different Calico builds would mean two different NetworkPolicy engines
    enforcing the manifests CI claims to validate, and a version/checksum
    pair that disagrees across jobs fails the download instead, which reads
    as a flake rather than a pinning mistake.

    Checks every ``*_VERSION`` / ``*_SHA256`` pin generically so tooling
    added later inherits the guarantee without a new test.
    """
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))
    pins = _job_env_pins(workflow)

    values_by_pin: dict[str, dict[str, set[str]]] = {}
    for job_name, job_pins in pins.items():
        for pin_name, value in job_pins.items():
            values_by_pin.setdefault(pin_name, {}).setdefault(value, set()).add(job_name)

    disagreements = {
        pin_name: {value: sorted(jobs) for value, jobs in by_value.items()}
        for pin_name, by_value in values_by_pin.items()
        if len(by_value) > 1
    }
    assert not disagreements, (
        "workflow pins disagree across jobs in integration-tests.yml "
        f"(bump every declaration together): {disagreements}"
    )

    # Guard the guard: if the shared pins ever stop being shared, this test
    # silently proves nothing. Calico and Helm are pinned by two jobs each.
    shared = {
        pin for pin, by_value in values_by_pin.items() if len(next(iter(by_value.values()))) > 1
    }
    assert {"CALICO_VERSION", "CALICO_SHA256"} <= shared, (
        "expected Calico to be pinned by more than one job; if the second "
        f"kind cluster was removed, drop this assertion. Shared pins: {sorted(shared)}"
    )


def test_every_calico_installing_job_pins_version_and_checksum() -> None:
    """A job that installs Calico must carry both of its own pins.

    Job-level ``env`` does not inherit between jobs, so a job that curls the
    Calico manifest while relying on another job's pins would expand
    ``${CALICO_VERSION}`` to an empty string and fetch a bogus URL (or, worse,
    skip the checksum comparison against an empty expectation).
    """
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))
    pins = _job_env_pins(workflow)

    installing_jobs = [
        name
        for name, job in workflow["jobs"].items()
        if any("projectcalico/calico" in (step.get("run") or "") for step in job.get("steps") or [])
    ]
    assert installing_jobs, "no job installs Calico — has the CNI setup moved?"

    for job_name in installing_jobs:
        job_pins = pins[job_name]
        assert "CALICO_VERSION" in job_pins, f"{job_name} installs Calico without CALICO_VERSION"
        assert "CALICO_SHA256" in job_pins, f"{job_name} installs Calico without CALICO_SHA256"


def test_kind_clusters_without_a_default_cni_install_one() -> None:
    """Using the Calico kind config obliges the job to install Calico.

    kind-calico.yaml sets ``disableDefaultCNI: true``, so the control plane
    cannot go Ready until a CNI is installed. A job that adopts the config
    without the install step hangs instead of failing with a clear cause.
    """
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))

    for job_name, job in workflow["jobs"].items():
        steps = job.get("steps") or []
        uses_calico_config = any(
            str(step.get("uses", "")).startswith("helm/kind-action")
            and "kind-calico.yaml" in str((step.get("with") or {}).get("config", ""))
            for step in steps
        )
        if not uses_calico_config:
            continue
        assert any("projectcalico/calico" in (step.get("run") or "") for step in steps), (
            f"{job_name} creates a kind cluster with the default CNI disabled "
            "but never installs Calico"
        )
