"""Offline contracts for CI and runtime artifact provenance controls."""

import ast
import re
import stat
import tomllib
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


def _workflow_job_step(relative_path: str, job_id: str, step_name: str) -> dict:
    """Return one named step from one job, rejecting duplicate/missing matches."""
    workflow = yaml.safe_load(_read(relative_path))
    job = (workflow.get("jobs") or {}).get(job_id)
    assert isinstance(job, dict), f"workflow job not found: {relative_path}: {job_id}"
    matches = [
        step
        for step in job.get("steps") or []
        if isinstance(step, dict) and step.get("name") == step_name
    ]
    assert len(matches) == 1, (
        f"expected one workflow step: {relative_path}: {job_id}: {step_name}; found {len(matches)}"
    )
    return matches[0]


def test_required_linux_unit_jobs_install_the_committed_lock() -> None:
    """Required Linux test/CDK jobs must execute the graph that keys their cache."""
    workflow = yaml.safe_load(_read(".github/workflows/unit-tests.yml"))
    locked_jobs = {
        "unit-pytest-core-shard",
        "unit-cdk-synth",
        "unit-cdk-config-matrix",
        "unit-cdk-project-name-scoping",
        "unit-cdk-nag-compliance",
    }
    for job_id in locked_jobs:
        job = workflow["jobs"][job_id]
        commands = "\n".join(step.get("run", "") for step in job["steps"] if isinstance(step, dict))
        assert "pip install -r requirements-lock.txt" in commands, job_id
        assert "pip install -e . --no-deps" in commands, job_id

    # This job intentionally proves that project metadata resolves from scratch.
    fresh_commands = "\n".join(
        step.get("run", "")
        for step in workflow["jobs"]["unit-fresh-install"]["steps"]
        if isinstance(step, dict)
    )
    assert 'pip install -e ".[cdk]"' in fresh_commands
    assert "pip install -r requirements-lock.txt" not in fresh_commands


def test_lockfile_check_uses_its_pinned_resolver_toolchain() -> None:
    step = _workflow_step(
        ".github/workflows/unit-tests.yml", "Install the locked pip-tools version"
    )
    assert 'python -m pip install "pip==25.0.1"' in step
    assert "grep -E '^pip-tools==' requirements-lock.txt" in step
    assert "pip install pip-tools" not in step


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
            "v0.8.0",
            "9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883",
        ),
        (
            ".github/workflows/integration-tests.yml",
            "v3.32.2",
            "a8c828a06a87c629a282ebbc424895b77f3a030251993e41ea400a743675bb02",
        ),
        (
            ".github/workflows/integration-tests.yml",
            "v0.9.0",
            "1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b",
        ),
        (
            "lambda/helm-installer/Dockerfile",
            "v4.2.4",
            "c306b46f719b0a4da32d0f78ee21bf90ce8d602f15b22ab753f0674d1670a7f3",
        ),
        (
            "lambda/helm-installer/Dockerfile",
            "v1.36.4",
            "8b8f088da2dab964f853b38464033b1be15ede2839eca751482357c45abdd05a",
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
            # Helm pin is derived at runtime from the installer Dockerfile;
            # the "declaration" is the derive step that loads GITHUB_ENV,
            # and the download/verify binding is unchanged.
            ".github/workflows/integration-tests.yml",
            "Install Helm",
            "extract_helm_installer_pins lambda/helm-installer/Dockerfile | grep '^HELM_'",
            "grep -q '^HELM_SHA256=' \"$GITHUB_ENV\"",
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
            'CALICO_VERSION: "v3.32.2"',
            'CALICO_SHA256: "a8c828a06a87c629a282ebbc424895b77f3a030251993e41ea400a743675bb02"',
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
            "extract_helm_installer_pins lambda/helm-installer/Dockerfile | tr '|' '='",
            'grep -q "^${pin}=" "$GITHUB_ENV"',
            "helm-${HELM_VERSION}-linux-amd64.tar.gz",
            'echo "${HELM_SHA256}  ${archive}" | sha256sum -c -',
        ),
        (
            ".github/workflows/deps-scan.yml",
            "Install pinned kubectl",
            "extract_helm_installer_pins lambda/helm-installer/Dockerfile | tr '|' '='",
            'grep -q "^${pin}=" "$GITHUB_ENV"',
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


def test_helm_and_kubectl_pins_live_only_in_the_installer_dockerfile() -> None:
    """Workflows derive Helm/kubectl pins; literal copies must not return.

    lambda/helm-installer/Dockerfile is the single source: CI jobs load
    HELM_* / KUBECTL_* into GITHUB_ENV from it via
    ``extract_helm_installer_pins``. A literal ``HELM_VERSION: "vX"`` in any
    workflow would shadow the derived value inside that job and silently
    drift from what the installer Lambda actually ships. (Runtime half of
    this guard: the version-consistency section of dependency-scan.sh
    reports any reintroduced workflow copy.)
    """
    installer = _read("lambda/helm-installer/Dockerfile")
    assert "get.helm.sh/helm-v" in installer
    assert "dl.k8s.io/release/v" in installer

    offenders: dict[str, list[str]] = {}
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        hits = re.findall(
            r"^\s*(?:HELM|KUBECTL)_(?:VERSION|SHA256):.*$",
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        "literal Helm/kubectl pin declarations reintroduced in workflows "
        f"(derive them from lambda/helm-installer/Dockerfile instead): {offenders}"
    )

    # Both deriving workflows must actually run the derive step.
    for workflow in (".github/workflows/integration-tests.yml", ".github/workflows/deps-scan.yml"):
        assert "extract_helm_installer_pins" in _read(workflow), (
            f"{workflow} no longer derives its Helm/kubectl pins from the installer Dockerfile"
        )


def test_workflows_never_pip_install_a_package_pyproject_declares() -> None:
    """CI installs the project, never a distribution pyproject already declares.

    Naming a declared package in a workflow creates a second copy of its
    version with nothing reconciling the two. That drifted for real: the moto
    server step pinned 5.2.2 while pyproject moved to 5.2.3 and, because the
    step also constrained against requirements-lock.txt, pip refused to resolve
    at all. Deriving the version would have fixed the symptom and left the
    second copy in place, so the packages are not named at all any more — jobs
    install ``.``/``.[extra]`` or the lock, and the queue-processor job gets its
    SQS wire API from the same digest-pinned emulator floci-tests.yml uses.

    Targets that are not a declared distribution stay legal: ``pip==25.0.1``
    (the installer bootstrapping a throwaway resolver env), ``uv``, and
    lock-derived ``"$pin"`` installs. ``deps-scan.yml`` is exempt outright —
    resolving packages against *latest* is that workflow's entire purpose.
    """
    pyproject = tomllib.loads(_read("pyproject.toml"))
    project = pyproject.get("project", {})
    specs = list(project.get("dependencies", []) or [])
    for group in (project.get("optional-dependencies", {}) or {}).values():
        specs.extend(group or [])

    def normalize(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    declared = {normalize(re.split(r"[\[=!<>;~ ]", spec, maxsplit=1)[0]) for spec in specs}
    declared.discard("gco-cli")

    def named_packages(command: str) -> list[str]:
        """Distribution names a ``pip install`` command installs by name."""
        found = []
        for invocation in re.findall(r"pip install([^\n|;&]*)", command):
            for raw in invocation.split():
                token = raw.strip("\"'")
                if not token:
                    continue
                # Flags, requirement/constraint files, the project itself, and
                # wholly shell-interpolated targets (``"$pin"`` read out of the
                # lock) are all legitimate. A token that merely *contains* a
                # variable is not exempt: ``pyyaml==${v}`` still names the
                # package, which is the copy this guard exists to prevent.
                if (
                    token.startswith("-")
                    or token.startswith(".")
                    or token.startswith("$")
                    or "/" in token
                    or token.endswith(".txt")
                ):
                    continue
                name = normalize(re.split(r"[\[=!<>;~]", token, maxsplit=1)[0])
                if name in declared:
                    found.append(token)
        return found

    offenders: dict[str, list[str]] = {}
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        if path.name == "deps-scan.yml":
            continue
        hits = named_packages(path.read_text(encoding="utf-8"))
        if hits:
            offenders[path.name] = hits

    assert not offenders, (
        "workflow steps pip-install a distribution pyproject.toml already declares, "
        "creating a second copy of its version; install the project "
        '(``pip install -e .`` / ``-e ".[extra]"``) or requirements-lock.txt instead: '
        f"{offenders}"
    )


def test_workflows_do_not_execute_mutable_remote_installers() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    )

    assert "get.docker.com" not in workflows
    assert "bash <(curl" not in workflows
    assert "raw.githubusercontent.com/rhysd/actionlint/main" not in workflows


@pytest.mark.parametrize(
    ("job_id", "step_name", "download_fragment", "verification_command"),
    [
        (
            "integration-helm-charts-valid",
            "Install Helm",
            "helm-${HELM_VERSION}-linux-amd64.tar.gz",
            'echo "${HELM_SHA256}  ${archive}" | sha256sum -c -',
        ),
        (
            "integration-k8s-manifest-schema",
            "Install pinned kubeconform",
            "kubeconform-linux-amd64.tar.gz",
            'echo "${KUBECONFORM_SHA256}  ${archive}" | sha256sum -c -',
        ),
        (
            "integration-kind-cluster-e2e",
            "Install Calico for NetworkPolicy enforcement",
            "projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml",
            'echo "${CALICO_SHA256}  ${calico_manifest}" | sha256sum -c -',
        ),
        (
            "integration-kind-cluster-e2e",
            "Install Metrics Server for HPA reconciliation",
            "metrics-server/releases/download/${METRICS_SERVER_VERSION}/components.yaml",
            'echo "${METRICS_SERVER_SHA256}  ${metrics_manifest}" | sha256sum -c -',
        ),
        (
            "integration-kind-examples-smoke",
            "Install Helm",
            "helm-${HELM_VERSION}-linux-amd64.tar.gz",
            'echo "${HELM_SHA256}  ${archive}" | sha256sum -c -',
        ),
        (
            "integration-kind-examples-smoke",
            "Install Calico for NetworkPolicy enforcement",
            "projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml",
            'echo "${CALICO_SHA256}  ${calico_manifest}" | sha256sum -c -',
        ),
    ],
)
def test_kind_bootstrap_downloads_retry_all_transport_errors_before_checksum(
    job_id: str,
    step_name: str,
    download_fragment: str,
    verification_command: str,
) -> None:
    step = _workflow_job_step(".github/workflows/integration-tests.yml", job_id, step_name)
    run = step.get("run") or ""

    for option in (
        "--connect-timeout 15",
        "--max-time 60",
        "--retry 3",
        "--retry-all-errors",
        "--retry-max-time 180",
        "--remove-on-error",
    ):
        assert option in run, f"{job_id}/{step_name} lacks {option}"
    assert download_fragment in run
    assert verification_command in run
    assert run.index("curl ") < run.index(verification_command)


def test_kind_node_and_probe_images_are_prepulled_before_use() -> None:
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))
    jobs = workflow["jobs"]

    for job_id in ("integration-kind-cluster-e2e", "integration-kind-examples-smoke"):
        steps = jobs[job_id]["steps"]
        kind_index = next(
            index
            for index, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("helm/kind-action")
        )
        pull_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("uses") == "./.github/actions/docker-pull-with-retry"
            and "${{ env.KIND_NODE_IMAGE }}" in str((step.get("with") or {}).get("images", ""))
        )
        assert pull_index < kind_index, f"{job_id} must pre-pull the Kind node image"

    cluster_steps = jobs["integration-kind-cluster-e2e"]["steps"]
    bootstrap_pull = next(
        step
        for step in cluster_steps
        if step.get("name") == "Pre-pull Kind bootstrap images with retry"
    )
    assert "busybox:1.38.0" in bootstrap_pull["with"]["images"]
    kind_index = next(
        i
        for i, step in enumerate(cluster_steps)
        if str(step.get("uses", "")).startswith("helm/kind-action")
    )
    load_index = next(
        i
        for i, step in enumerate(cluster_steps)
        if step.get("name") == "Load pinned probe image into Kind"
    )
    probe_index = next(
        i
        for i, step in enumerate(cluster_steps)
        if step.get("name") == "Verify NetworkPolicy enforcement blocks cross-namespace traffic"
    )
    assert kind_index < load_index < probe_index
    assert "kind load docker-image busybox:1.38.0 --name gco-ci" in cluster_steps[load_index]["run"]
    consumers = {
        "Verify NetworkPolicy enforcement blocks cross-namespace traffic": 2,
        "Apply ResourceQuotas and LimitRanges": 1,
    }
    for step_name, expected_count in consumers.items():
        run = next(step["run"] for step in cluster_steps if step.get("name") == step_name)
        assert run.count("--image=busybox:1.38.0") == expected_count, step_name


def test_kind_examples_prefetches_charts_but_keeps_mutations_fail_fast() -> None:
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))
    job = workflow["jobs"]["integration-kind-examples-smoke"]
    assert job["timeout-minutes"] >= 60
    steps = job["steps"]
    by_name = {step.get("name"): step for step in steps if isinstance(step, dict)}
    prefetch = by_name["Prefetch pinned Kind charts with retry"]["run"]

    assert "for attempt in 1 2 3 4" in prefetch
    assert "timeout 60s helm pull" in prefetch
    assert "delay=$((2 ** attempt))" in prefetch
    assert 'if [[ "${ref}" == oci://* ]]' in prefetch
    assert 'pull_args=("${ref}")' in prefetch
    assert 'pull_args=("${ref#*/}" --repo "${repo_url}")' in prefetch
    assert 'echo "${env_name}=${archive}" >> "${GITHUB_ENV}"' in prefetch
    for chart, env_name in (
        ("kube-prometheus-stack", "KPS_CHART_ARCHIVE"),
        ("cert-manager", "CERT_MANAGER_CHART_ARCHIVE"),
        ("kubeflow-trainer", "TRAINER_CHART_ARCHIVE"),
        ("mlflow", "MLFLOW_CHART_ARCHIVE"),
    ):
        assert f"pull_chart {chart} {env_name}" in prefetch

    local_archives = {
        "Install ServiceMonitor CRD from the pinned kube-prometheus-stack": "${KPS_CHART_ARCHIVE}",
        "Install pinned cert-manager (the trainer chart's cert dependency)": "${CERT_MANAGER_CHART_ARCHIVE}",
        "Install pinned kubeflow-trainer chart with shipped values": "${TRAINER_CHART_ARCHIVE}",
        "Re-run the trainer install as an upgrade (idempotency contract)": "${TRAINER_CHART_ARCHIVE}",
        "Install pinned mlflow chart with shipped values": "${MLFLOW_CHART_ARCHIVE}",
    }
    for step_name, archive in local_archives.items():
        run = by_name[step_name]["run"]
        assert archive in run, step_name
        assert "helm repo add" not in run, step_name
        assert "helm repo update" not in run, step_name
        assert "helm pull" not in run, step_name
        assert "for attempt in" not in run, step_name
        assert "retrying" not in run.lower(), step_name
        mutations = re.findall(r"^\s*(?:if ! )?helm (?:install|upgrade)\b", run, re.MULTILINE)
        assert len(mutations) <= 1, step_name

    install_helm_index = next(
        i for i, step in enumerate(steps) if step.get("name") == "Install Helm"
    )
    prefetch_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Prefetch pinned Kind charts with retry"
    )
    kind_index = next(
        i
        for i, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("helm/kind-action")
    )
    assert install_helm_index < prefetch_index < kind_index


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


def test_release_stage_one_never_writes_to_main() -> None:
    """Stage 1 (release.yml) must go through the PR gate like any other change.

    The version bump lands on a release/vX.Y.Z branch and merges through
    review + required checks. Direct pushes to the dispatching ref, tag
    creation, and GitHub Release creation are stage-2 concerns; any of them
    reappearing here would bypass branch protection.
    """
    workflow = _read(".github/workflows/release.yml")

    assert 'git push origin "HEAD:refs/heads/${BRANCH}"' in workflow
    assert "HEAD:${GITHUB_REF}" not in workflow
    assert "HEAD:refs/heads/main" not in workflow
    assert "git tag" not in workflow
    assert "gh release create" not in workflow
    # Only from main: a dispatch on a tag or side branch is fail-closed.
    assert "if: github.ref == 'refs/heads/main'" in workflow


def test_release_stage_two_publishes_only_the_merged_release_commit() -> None:
    """Stage 2 (release-publish.yml) tags main's merge commit, idempotently.

    It reacts only to main pushes that change VERSION, refuses to move an
    existing v-tag (immutability), verifies every version mirror agrees
    before tagging, and gates push-event publishes on the `Release vX.Y.Z`
    commit subject so a stray VERSION edit is never auto-tagged.
    """
    workflow = _read(".github/workflows/release-publish.yml")

    assert "branches: [main]" in workflow
    assert "- VERSION" in workflow
    assert 'git tag -a "v${NEW_VERSION}" -m "Release v${NEW_VERSION}" "$GITHUB_SHA"' in workflow
    assert 'git push origin "refs/tags/v${NEW_VERSION}"' in workflow
    assert "--verify-tag" in workflow
    # Immutability + idempotency guards.
    assert "Released tags are immutable" in workflow
    assert "already points at" in workflow
    assert "gh release view" in workflow
    # Version mirrors must agree before anything is published.
    assert "refusing to tag" in workflow
    # Push-event publishes require a release commit subject.
    assert 'pattern="^Release v${NEW_VERSION//./\\\\.}( \\(#[0-9]+\\))?$"' in workflow


def test_release_workflows_share_one_serialized_concurrency_group() -> None:
    """Both stages share `group: release` and never cancel in-flight runs.

    A publish interleaving with the next release's branch cut (or a canceled
    half-publish) is exactly the torn state the old single-stage atomic push
    protected against; the shared no-cancel group is its replacement.
    """
    for path in (".github/workflows/release.yml", ".github/workflows/release-publish.yml"):
        workflow = _read(path)
        assert "group: release" in workflow, path
        assert "cancel-in-progress: false" in workflow, path


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


def test_container_tool_checksums_are_non_overridable_trust_anchors() -> None:
    dev_dockerfile = _read("Dockerfile.dev")
    buildx_section = dev_dockerfile[
        dev_dockerfile.index("# Install the Docker Buildx CLI plugin") : dev_dockerfile.index(
            "# Install uv"
        )
    ]
    installer_dockerfile = _read("lambda/helm-installer/Dockerfile")
    helm_section = installer_dockerfile[
        installer_dockerfile.index("# Install Helm") : installer_dockerfile.index(
            "# Install kubectl"
        )
    ]
    kubectl_section = installer_dockerfile[
        installer_dockerfile.index("# Install kubectl") : installer_dockerfile.index(
            "# Install Python dependencies"
        )
    ]

    assert "ARG BUILDX_SHA256" not in dev_dockerfile
    assert "ARG BUILDX_VERSION=v0.36.1" in buildx_section
    assert "buildx-${BUILDX_VERSION}.linux-${TARGETARCH}" in buildx_section
    assert (
        'amd64) BUILDX_SHA256="48af8a397ebd60178778bf63611dbcebe5f5e7a9be90eb9147b24b9587455778"'
    ) in buildx_section
    assert (
        'arm64) BUILDX_SHA256="5d0cafd9d16afe1a0f0d9529885344ace2cc99efdd531b6c783c5455a6001569"'
    ) in buildx_section
    assert (
        'echo "${BUILDX_SHA256}  /usr/local/lib/docker/cli-plugins/docker-buildx" | sha256sum -c -'
    ) in buildx_section

    assert "ARG HELM_SHA256" not in installer_dockerfile
    assert "ARG KUBECTL_SHA256" not in installer_dockerfile
    assert "helm-v4.2.4-linux-amd64.tar.gz" in helm_section
    assert (
        "c306b46f719b0a4da32d0f78ee21bf90ce8d602f15b22ab753f0674d1670a7f3  /tmp/helm.tar.gz"
    ) in helm_section
    assert "release/v1.36.4/bin/linux/amd64/kubectl" in kubectl_section
    assert (
        "8b8f088da2dab964f853b38464033b1be15ede2839eca751482357c45abdd05a  /tmp/kubectl"
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

    # Tooling installed by MORE than one job is single-sourced at the
    # workflow-level env block instead of repeated per job. Guard both
    # halves of that scheme: the shared declarations exist exactly there,
    # and no job-level env shadows one of them — a shadow would silently
    # fork the value while still reading as "pinned" in review.
    workflow_env = workflow.get("env") or {}
    shared_pins = {"KIND_VERSION", "KIND_NODE_IMAGE", "CALICO_VERSION", "CALICO_SHA256"}
    missing = shared_pins - set(workflow_env)
    assert not missing, (
        f"shared kind/Calico pins missing from the workflow-level env block: {sorted(missing)}"
    )
    shadows = {
        job_name: sorted(shared_pins & set(job_pins))
        for job_name, job_pins in pins.items()
        if shared_pins & set(job_pins)
    }
    assert not shadows, (
        "job-level env re-declares a workflow-level shared pin (the shadow "
        f"wins inside that job and can drift unseen): {shadows}"
    )


def test_every_calico_installing_job_pins_version_and_checksum() -> None:
    """Every job that installs Calico must resolve both of its pins.

    The pins live once, in the workflow-level ``env`` block, which inherits
    into every job — the historical failure mode (a job curling the manifest
    while the pins sat in a *different job's* env, expanding
    ``${CALICO_VERSION}`` to an empty string) cannot recur as long as the
    workflow-level declarations exist and every kind-action step references
    the same single source rather than carrying a literal copy.
    """
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))
    workflow_env = workflow.get("env") or {}

    installing_jobs = [
        name
        for name, job in workflow["jobs"].items()
        if any("projectcalico/calico" in (step.get("run") or "") for step in job.get("steps") or [])
    ]
    assert installing_jobs, "no job installs Calico — has the CNI setup moved?"

    assert "CALICO_VERSION" in workflow_env, "CALICO_VERSION missing from workflow-level env"
    assert "CALICO_SHA256" in workflow_env, "CALICO_SHA256 missing from workflow-level env"

    # The kind-action steps must reference the shared declarations, not
    # carry literal version/node-image copies that can drift per job.
    kind_steps = [
        (job_name, step)
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps") or []
        if str(step.get("uses", "")).startswith("helm/kind-action")
    ]
    assert kind_steps, "no kind-action steps found — has cluster creation moved?"
    for job_name, step in kind_steps:
        with_ = step.get("with") or {}
        assert with_.get("version") == "${{ env.KIND_VERSION }}", (
            f"{job_name} pins the kind binary inline instead of referencing "
            "the workflow-level KIND_VERSION"
        )
        assert with_.get("node_image") == "${{ env.KIND_NODE_IMAGE }}", (
            f"{job_name} pins the kind node image inline instead of referencing "
            "the workflow-level KIND_NODE_IMAGE"
        )


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
