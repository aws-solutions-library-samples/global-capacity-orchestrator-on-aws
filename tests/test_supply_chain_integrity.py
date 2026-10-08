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
            "v3.33.0",
            "2de8f47595fb9c41b3f47d7b767a1f8e72ecf84057af834738ff12689a234da5",
        ),
        (
            ".github/workflows/integration-tests.yml",
            "v0.9.0",
            "1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b",
        ),
        (
            "lambda/helm-installer/Dockerfile",
            "v4.3.0",
            "86584a54def73570558f66f5111cc53dfed56689637ae32c1201205d494f54fb",
        ),
        (
            "lambda/helm-installer/Dockerfile",
            "v1.37.1",
            "65691ff77eb6fa44c908b77a1082c9f092c3b9733b5cefabec0d1104890e21a8",
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
            'CALICO_VERSION: "v3.33.0"',
            'CALICO_SHA256: "2de8f47595fb9c41b3f47d7b767a1f8e72ecf84057af834738ff12689a234da5"',
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
                if token.startswith(("-", ".", "$")) or "/" in token or token.endswith(".txt"):
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
            "integration-kind-cost-pipeline",
            "Install Helm",
            "helm-${HELM_VERSION}-linux-amd64.tar.gz",
            'echo "${HELM_SHA256}  ${archive}" | sha256sum -c -',
        ),
        (
            "integration-kind-cost-pipeline",
            "Install Calico for NetworkPolicy enforcement",
            "projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml",
            'echo "${CALICO_SHA256}  ${calico_manifest}" | sha256sum -c -',
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
        (
            "integration-kind-platform-addons",
            "Install Helm",
            "helm-${HELM_VERSION}-linux-amd64.tar.gz",
            'echo "${HELM_SHA256}  ${archive}" | sha256sum -c -',
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

    for job_id in (
        "integration-kind-cluster-e2e",
        "integration-kind-cost-pipeline",
        "integration-kind-examples-smoke",
        "integration-kind-platform-addons",
    ):
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
        if step.get("name") == "Verify NetworkPolicy enforcement (allowed and denied paths)"
    )
    assert kind_index < load_index < probe_index
    assert (
        "preload_kind_images.py --cluster gco-ci --image busybox:1.38.0"
        in cluster_steps[load_index]["run"]
    )
    # The enforcement step launches its targets and probe clients through two
    # helper functions (one `kubectl run` each); the governance step runs one
    # dry-run pod. Every launch must use the pre-pulled pinned image.
    consumers = {
        "Verify NetworkPolicy enforcement (allowed and denied paths)": 2,
        "Apply ResourceQuotas and LimitRanges": 1,
    }
    for step_name, expected_count in consumers.items():
        run = next(step["run"] for step in cluster_steps if step.get("name") == step_name)
        assert run.count("--image=busybox:1.38.0") == expected_count, step_name
        assert len(re.findall(r"kubectl (?:-n \S+ )?run ", run)) == expected_count, step_name


def test_every_kind_image_load_goes_through_the_preload_script() -> None:
    """No workflow calls ``kind load`` itself; ``preload_kind_images.py`` is the one loader.

    ``kind load docker-image`` imports a ``docker save`` archive with ``ctr
    images import --all-platforms``. On Docker 29 (Ubuntu 26.04 runners) the
    containerd image store records a built or pulled image under its
    multi-platform index with content for the daemon's platform only, so that
    import fails with ``content digest ... not found`` (kubernetes-sigs/kind#4224).
    The script saves the daemon's platform alone and loads the archive. A raw
    ``kind load`` in a workflow brings the failure back, so none may exist, and
    every job that loads an image must install the project (PyYAML) first.
    """
    loaders: list[str] = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        direct = [
            line.strip()
            for line in text.splitlines()
            if "kind load" in line and not line.strip().startswith("#")
        ]
        assert not direct, f"{path.name} runs kind load directly; use preload_kind_images.py"
        workflow = yaml.safe_load(text)
        for job_id, job in workflow["jobs"].items():
            steps = job.get("steps") or []
            installs = [
                index
                for index, step in enumerate(steps)
                if re.search(r"pip install -e [\"']?\.", step.get("run") or "")
            ]
            for index, step in enumerate(steps):
                run = step.get("run") or ""
                if "preload_kind_images.py" not in run:
                    continue
                loaders.append(f"{path.name}:{job_id}:{step.get('name')}")
                assert installs and installs[0] < index, (
                    f"{job_id}/{step.get('name')} loads images before pip install -e ."
                )
    assert len(loaders) >= 9, loaders


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
        ("trust-manager", "TRUST_MANAGER_CHART_ARCHIVE"),
        ("kubeflow-trainer", "TRAINER_CHART_ARCHIVE"),
        ("mlflow", "MLFLOW_CHART_ARCHIVE"),
    ):
        assert f"pull_chart {chart} {env_name}" in prefetch

    local_archives = {
        "Install ServiceMonitor CRD from the pinned kube-prometheus-stack": "${KPS_CHART_ARCHIVE}",
        "Install pinned cert-manager (the trainer chart's cert dependency)": "${CERT_MANAGER_CHART_ARCHIVE}",
        "Install pinned trust-manager with shipped values": "${TRUST_MANAGER_CHART_ARCHIVE}",
        "Re-run the trust-manager install as an upgrade (idempotency contract)": (
            "${TRUST_MANAGER_CHART_ARCHIVE}"
        ),
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


def test_kind_platform_addons_is_a_real_artifact_test_that_stays_fail_fast() -> None:
    """The platform add-ons job installs the pinned charts from local archives.

    Registry pulls retry; installs and every assertion stay single-shot. Every
    image a chart runs is preloaded into the node, with retry, before that
    chart's install: each preload renders the chart with the install's exact
    values and loads into the job's own cluster, so ``helm install --wait``
    never waits on a kubelet pull (ECR Public's anonymous throttle kept failing
    Argo CD's Redis that way). The chart values and post-Helm manifests come
    from the helpers the regional stack calls, Argo CD syncs this commit, and
    both dashboards are captured by the CLI's own screenshot code and uploaded
    even when a later step fails.
    """
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))
    job = workflow["jobs"]["integration-kind-platform-addons"]
    assert job["name"] == "integration:kind:platform-addons"
    assert job["env"]["GITOPS_REVISION"] == (
        "${{ github.event.pull_request.head.sha || github.sha }}"
    )
    assert "github.event.pull_request.head.repo.clone_url" in job["env"]["GITOPS_REPO_URL"]
    steps = job["steps"]
    by_name = {step.get("name"): step for step in steps if isinstance(step, dict)}
    prefetch = by_name["Prefetch the pinned platform add-on charts with retry"]["run"]
    assert "for attempt in 1 2 3 4" in prefetch
    assert "timeout 60s helm pull" in prefetch
    for chart, env_name in (
        ("argocd", "ARGOCD_CHART_ARCHIVE"),
        ("crossplane", "CROSSPLANE_CHART_ARCHIVE"),
        ("crossview", "CROSSVIEW_CHART_ARCHIVE"),
    ):
        assert f"pull_chart {chart} {env_name}" in prefetch
    for step_name, archive in (
        ("Install the pinned argo-cd chart with the shipped values", "${ARGOCD_CHART_ARCHIVE}"),
        (
            "Install the pinned crossplane and crossview charts with the shipped values",
            '"${!archive_var}"',
        ),
    ):
        run = by_name[step_name]["run"]
        assert archive in run, step_name
        assert "helm pull" not in run, step_name
        assert "helm repo" not in run, step_name
        assert "for attempt in" not in run, step_name
    render = by_name["Render the Argo CD chart values and post-Helm manifests like the stack"][
        "run"
    ]
    assert "argocd_chart_values" in render
    assert "compute_argocd_replacements" in render
    assert '"autoscaling": {"enabled": True' in render
    install = by_name["Install the pinned argo-cd chart with the shipped values"]["run"]
    assert '--values "${RUNNER_TEMP}/argocd-gco-values.json"' in install
    assert (
        "argocd-repo-server"
        in by_name["Prove the repo-server autoscaler owns the replica count"]["run"]
    )
    captures = {
        "Capture the Argo CD UI with the gco gitops screenshot code": (
            "gitops.capture_argocd_screenshot",
            "gitops.create_session_token",
        ),
        "Check and capture the Crossview dashboard with the gco crossplane code": (
            "crossplane.capture_dashboard_screenshot",
            "/api/health",
        ),
    }
    for step_name, fragments in captures.items():
        run = by_name[step_name]["run"]
        for fragment in fragments:
            assert fragment in run, (step_name, fragment)
        assert "build_port_forward_command" in run
    chromium = by_name["Install the headless Chromium the captures drive (with retry)"]["run"]
    assert "playwright install --with-deps --only-shell chromium" in chromium
    assert "for attempt in 1 2 3" in chromium
    assert by_name["Install project (renderers, Lambda helpers and the capture code)"]["run"] == (
        'pip install -e ".[diagrams]"'
    )
    upload = by_name["Upload the dashboard captures"]
    assert upload["if"] == "always()"
    assert upload["uses"] == "./.github/actions/upload-artifact-with-retry"
    assert upload["with"]["name"] == "platform-addon-dashboards"
    order = [step.get("name") for step in steps]
    assert (
        order.index("Install the pinned argo-cd chart with the shipped values")
        < order.index("Prove the repo-server autoscaler owns the replica count")
        < order.index("Sync the GitOps fixture from this commit")
        < order.index("Capture the Argo CD UI with the gco gitops screenshot code")
        < order.index("Cascade the example Application through the installer cleanup")
        < order.index("Compose the example BatchJob")
        < order.index("Check and capture the Crossview dashboard with the gco crossplane code")
        < order.index("Tear Crossplane down the way the stack does")
        < order.index("Upload the dashboard captures")
    )

    kind_step = next(
        step for step in steps if str(step.get("uses", "")).startswith("helm/kind-action")
    )
    cluster = kind_step["with"]["cluster_name"]
    assert cluster == "gco-platform-addons"
    render_name = "Render the Argo CD chart values and post-Helm manifests like the stack"
    argocd_preload = "Preload the images the argo-cd chart runs into kind (with retry)"
    # The argo-cd preload renders with the stack's override, so it follows that render.
    assert order.index(render_name) < order.index(argocd_preload)
    values_flags = re.compile(r'--values "[^"]+"')
    for preload_name, install_name, template in (
        (
            argocd_preload,
            "Install the pinned argo-cd chart with the shipped values",
            'helm template argocd "${ARGOCD_CHART_ARCHIVE}"',
        ),
        (
            "Preload the images the crossplane and crossview charts run into kind (with retry)",
            "Install the pinned crossplane and crossview charts with the shipped values",
            'helm template "${chart}" "${!archive_var}"',
        ),
    ):
        preload_run = by_name[preload_name]["run"]
        install_run = by_name[install_name]["run"]
        assert steps.index(kind_step) < order.index(preload_name) < order.index(install_name)
        assert template in preload_run, preload_name
        # The render is the install's: the same values files, in the same order.
        assert values_flags.findall(preload_run) == values_flags.findall(install_run), preload_name
        assert values_flags.findall(install_run), install_name
        assert (
            f"python3 .github/scripts/preload_kind_images.py --cluster {cluster}" in preload_run
        ), preload_name
        assert not re.search(r"\bhelm (?:install|upgrade)\b", preload_run), preload_name
        # The retry lives in the preload; the install it precedes stays single-shot.
        assert len(re.findall(r"^\s*helm install\b", install_run, re.MULTILINE)) == 1, install_name
        assert "retry" not in install_run.lower(), install_name
        assert "helm upgrade" not in install_run, install_name
    assert (
        "for chart in crossplane crossview; do"
        in by_name[
            "Preload the images the crossplane and crossview charts run into kind (with retry)"
        ]["run"]
    )


def test_kind_cluster_e2e_dry_runs_the_inference_deployments_the_monitor_renders() -> None:
    """The rendered inference Deployments must meet a real apiserver before a live deploy.

    The step has to use the production renderer (not a hand-written manifest),
    cover the renderer's branches (vLLM default, SGLang default, SGLang with
    operator-supplied launcher flags), submit with ``--dry-run=server`` so the
    admission chain runs without persisting anything, and require all three to
    be accepted. It must run once the shipped namespaces exist and before the
    namespace picks up its ResourceQuota, so a rejection is the renderer's.
    """
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))
    steps = workflow["jobs"]["integration-kind-cluster-e2e"]["steps"]
    order = [step.get("name") or step.get("uses") for step in steps]
    name = "Server-side dry-run the inference Deployments the monitor renders"
    run = next(step["run"] for step in steps if step.get("name") == name)

    assert "from gco.services.inference_monitor import InferenceMonitor" in run
    assert "monitor._build_inference_deployment_object(" in run
    assert '"framework": "vllm"' in run
    assert run.count('"framework": "sglang"') == 2
    assert '"--attention-backend", "triton", "--disable-cuda-graph"' in run
    assert '"node_selector": {' in run
    assert 'assert container["command"] == ["python3", "-m", "sglang.launch_server"]' in run
    # Every model pod carries the endpoint-tls-proxy sidecar: containers are
    # picked by name (never by position), and each render must front the
    # model's own port on 8443 with the gco-inference wildcard leaf.
    assert '(sidecar,) = [c for c in pod["containers"] if c["name"] == "endpoint-tls-proxy"]' in run
    assert (
        '(container,) = [c for c in pod["containers"] if c["name"] != "endpoint-tls-proxy"]' in run
    )
    assert '["containers"][0]' not in run
    assert '{"containerPort": 8443, "name": "https", "protocol": "TCP"}' in run
    assert 'sidecar_env["TLS_PROXY_UPSTREAM_PORT"] == str(spec["port"])' in run
    assert '"gco-inference-tls" in {' in run
    assert "kubectl apply --dry-run=server -f" in run
    assert "kubectl -n gco-inference get serviceaccount gco-service-account" in run
    assert """test "$(grep -c 'created (server dry run)' """ in run
    assert '= "3"' in run
    assert order.index("Apply namespaces + RBAC") < order.index(name)
    assert order.index(name) < order.index("Apply ResourceQuotas and LimitRanges")


def test_kind_cluster_e2e_runs_the_platform_on_internal_ca_tls() -> None:
    """cluster-e2e issues every platform leaf from one CA and proves the TLS-only posture.

    Each Secret the platform pods mount gets a leaf carrying the dnsNames of
    the shipped Certificate of the same name plus the ``ca.crt`` key the pods
    project as their clients' only trust anchor (without it they never leave
    ContainerCreating). The render gives tracing an explicit value and lets
    only listed opaque tokens fall back to a stub; the inference proxy then
    verifies its own sidecar through ``gco.services.internal_tls``; and the
    NetworkPolicy probes cover the TLS-only ports (inference-monitor metrics
    on 9443 not 9090; model pods on 8443 from the proxy only).
    """
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))
    steps = workflow["jobs"]["integration-kind-cluster-e2e"]["steps"]
    order = [step.get("name") or step.get("uses") for step in steps]
    by_name = {step.get("name"): step for step in steps if isinstance(step, dict)}

    seed_name = "Seed the platform TLS Secrets from a CI internal CA"
    seed = by_name[seed_name]["run"]
    for secret in (
        "health-monitor-tls",
        "manifest-processor-tls",
        "inference-proxy-tls",
        "inference-monitor-tls",
        "cost-monitor-tls",
    ):
        assert secret in seed, secret
    for fragment in (
        '"post-helm-api-workload-certificates.yaml"',
        '"post-helm-cost-monitoring-tls.yaml"',
        '("ClusterIssuer", "gco-internal-ca")',
        '",".join(certificate["spec"]["dnsNames"])',
        "basicConstraints = critical, CA:TRUE",
        "subjectKeyIdentifier = hash",
        "authorityKeyIdentifier = keyid",
        "openssl verify -x509_strict -purpose sslserver",
        "--type=kubernetes.io/tls",
        '--from-file=ca.crt="${ca_dir}/ca.crt"',
    ):
        assert fragment in seed, fragment
    # `kubectl create secret tls` has no ca.crt; the self-signed leaf is gone.
    assert "create secret tls" not in seed
    assert order.index("Apply namespaces + RBAC") < order.index(seed_name)
    assert order.index(seed_name) < order.index("Render and apply deployments")

    render = by_name["Render and apply deployments"]["run"]
    assert '"{{TRACING_ENABLED}}": "false"' in render
    assert '"{{TRACING_SAMPLE_RATIO}}": "0.05"' in render
    assert "opaque = {" in render
    assert 'raise SystemExit(f"{name}: no CI value for {sorted(leftover)}")' in render

    verify_name = "Verify the internal CA chain through the services' own trust code"
    verify = by_name[verify_name]["run"]
    assert "kubectl -n gco-system exec deploy/inference-proxy -c inference-proxy --" in verify
    assert "from gco.services.internal_tls import" in verify
    assert 'get_healthz("inference-proxy.gco-system.svc.cluster.local")' in verify
    assert "except ssl.SSLCertVerificationError" in verify
    assert (
        order.index("Wait for inference proxy TLS rollout")
        < order.index(verify_name)
        < order.index("Verify inference proxy HPA is actively computing replicas")
    )

    netpol = by_name["Verify NetworkPolicy enforcement (allowed and denied paths)"]["run"]
    assert (
        "start_target gco-system netpol-target-metrics app=inference-monitor,project=gco 9443 9090"
        in netpol
    )
    assert 'app=netpol-probe-client "$metrics_ip" 9443 reachable' in netpol
    assert 'app=netpol-probe-client "$metrics_ip" 9090 blocked' in netpol
    assert (
        "start_target gco-inference netpol-target-model app=netpol-model,gco.io/type=inference "
        "8443 8000" in netpol
    )
    assert 'app=inference-proxy,gco.aws/ci-only=true "$model_ip" 8443 reachable' in netpol
    assert 'app=inference-proxy,gco.aws/ci-only=true "$model_ip" 8000 blocked' in netpol
    foreign = 'netpol-test-model-foreign app=netpol-probe-client "$model_ip" 8443 blocked'
    assert f"probe default {foreign}" in netpol


def test_kind_cost_pipeline_runs_the_real_monitor_against_the_pinned_charts() -> None:
    """The cost-pipeline job must stay a real-artifact test, fail-fast on mutation.

    Same prefetch/local-archive contract as examples-smoke (registry retries
    only on the read-only ``helm pull``; every install consumes one local
    archive once), plus the properties that make the job worth having: the
    chart identities come from ``charts.yaml`` through ``--emit-ref`` /
    ``--emit-values`` (never a literal version), the cost monitor is the
    ``cost-monitor:ci`` image built from the shipped Dockerfile and rendered
    from the shipped ``34-cost-monitor.yaml`` with the production
    ``OPENCOST_BASE_URL`` untouched, the S3 stand-in is the digest-pinned
    Floci emulator, the monitor's real data probe and report API are what is
    asserted, and the Parquet columns are checked against
    ``ALLOCATION_REPORT_FIELDS`` inside the monitor's own container.
    """
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))
    job = workflow["jobs"]["integration-kind-cost-pipeline"]
    assert job["name"] == "integration:kind:cost-pipeline"
    assert job["timeout-minutes"] >= 45
    assert job["env"]["CI_S3_EMULATOR_IMAGE"].startswith("floci/floci:")
    steps = job["steps"]
    by_name = {step.get("name"): step for step in steps if isinstance(step, dict)}

    prefetch = by_name["Prefetch pinned Kind charts with retry"]["run"]
    assert "for attempt in 1 2 3 4" in prefetch
    assert "timeout 60s helm pull" in prefetch
    assert 'echo "${env_name}=${archive}" >> "${GITHUB_ENV}"' in prefetch
    for chart, env_name in (
        ("kube-prometheus-stack", "KPS_CHART_ARCHIVE"),
        ("opencost", "OPENCOST_CHART_ARCHIVE"),
    ):
        assert f"pull_chart {chart} {env_name}" in prefetch

    local_archives = {
        "Install pinned kube-prometheus-stack with shipped values": (
            "kube-prometheus-stack",
            "${KPS_CHART_ARCHIVE}",
        ),
        "Install pinned OpenCost with shipped values": ("opencost", "${OPENCOST_CHART_ARCHIVE}"),
    }
    for step_name, (chart, archive) in local_archives.items():
        run = by_name[step_name]["run"]
        assert archive in run, step_name
        assert f"--emit-ref {chart}" in run, step_name
        assert f"--emit-values {chart}" in run, step_name
        assert "helm repo add" not in run, step_name
        assert "helm repo update" not in run, step_name
        assert "helm pull" not in run, step_name
        assert "for attempt in" not in run, step_name
        assert "retrying" not in run.lower(), step_name
        mutations = re.findall(r"^\s*(?:if ! )?helm (?:install|upgrade)\b", run, re.MULTILINE)
        assert len(mutations) == 1, step_name
    # The deploy-time overlay mirrors the regional stack's injected values;
    # the CI-only omissions are named where they are set.
    kps = by_name["Install pinned kube-prometheus-stack with shipped values"]["run"]
    assert '["context"]["cluster_observability"]' in kps
    assert '"storageClassName": storage_class' in kps
    assert 'storage_class = "gco-observability-gp3"' in kps
    assert "rollout status statefulset/prometheus-kube-prometheus-stack-prometheus" in kps
    opencost = by_name["Install pinned OpenCost with shipped values"]["run"]
    # The deploy-time values are the stack's own output, not a copy: the two
    # functions are executed from the stack source with the loaded image.
    assert 'source = Path("gco/stacks/regional_stack.py")' in opencost
    assert 'node.name == "_chart_tls_proxy_sidecar"' in opencost
    assert 'node.name == "_opencost_chart_values"' in opencost
    assert 'cost_monitor_image=SimpleNamespace(image_uri="cost-monitor:ci")' in opencost
    assert 'cluster=SimpleNamespace(cluster_name=os.environ["CI_CLUSTER_ID"])' in opencost
    assert 'sidecar["name"] == "opencost-tls-proxy"' in opencost
    assert "python3 - <<'PY' > \"${RUNNER_TEMP}/opencost-ci-overlay.yaml\"" in opencost
    # ...and no hand-written overlay beside it.
    assert 'defaultClusterId: "${CI_CLUSTER_ID}"' not in opencost
    # The sidecar made it into the chart pod on the Service's named port,
    # from an optional Secret volume that must not exist yet (the keypair
    # wait is exercised), and the pod wears the selector labels the
    # opencost-tls Service and allow-cost-monitor-to-opencost select.
    assert 'containers[?(@.name=="opencost-tls-proxy")].ports[?(@.name=="https")]' in opencost
    assert 'volumes[?(@.name=="gco-tls")].secret.optional}\')" = "true"' in opencost
    assert "get secret opencost-tls" in opencost
    assert "-l app.kubernetes.io/name=opencost,app.kubernetes.io/instance=opencost" in opencost
    # The chart's plaintext Service stays for the ServiceMonitor the shipped
    # values enable (Prometheus scrapes 9003 in-namespace; nothing else may).
    assert '= "9003"' in opencost
    assert "get servicemonitor opencost" in opencost
    # kind has no cloud provider: the fallback price sheet needs a writable
    # /var/configs, supplied as a scratch volume INTO the shipped read-only
    # root — which the step must prove survived the overlay. Helm replaces
    # lists, so it joins the stack's extraVolumes instead of replacing them.
    assert '{"name": "pricing-configs", "mountPath": "/var/configs"}' in opencost
    assert 'values["extraVolumes"].append({"name": "pricing-configs", "emptyDir": {}})' in opencost
    assert 'securityContext.readOnlyRootFilesystem}\')" = "true"' in opencost

    # No cert-manager here: the post-Helm cost TLS file is rendered like the
    # applier renders it, its Service applied as shipped, and its two
    # Certificates issued by a CI CA with their own names — both Secrets
    # carrying ca.crt from the one CA — before the monitor rolls out.
    tls = by_name["Apply the post-Helm cost TLS objects with a CI internal CA"]["run"]
    assert "post-helm-cost-monitoring-tls.yaml" in tls
    assert 'text.replace("{{COST_MONITORING_ENABLED}}", "true")' in tls
    assert "unsubstituted token(s) remain" in tls
    assert '[("gco-system", "cost-monitor-tls"), ("monitoring", "opencost-tls")]' in tls
    assert '("Service", "opencost-tls")' in tls
    assert '("ClusterIssuer", "gco-internal-ca")' in tls
    assert "openssl verify -x509_strict -purpose sslserver" in tls
    assert "--type=kubernetes.io/tls" in tls
    assert '--from-file=ca.crt="${ca_dir}/ca.crt"' in tls
    assert 'echo "COST_PIPELINE_CA_FILE=${ca_dir}/ca.crt" >> "${GITHUB_ENV}"' in tls
    assert "kubernetes.io/service-name=opencost-tls" in tls
    assert 'internal_ssl_context("/var/run/gco/tls/ca.crt")' in tls
    assert "exec deploy/opencost -c opencost-tls-proxy" in tls
    assert "helm " not in tls

    build = by_name["Build cost-monitor image"]
    assert build["with"]["file"] == "dockerfiles/Dockerfile.cost-monitor"
    assert build["with"]["tags"] == "cost-monitor:ci"
    render = by_name["Render and apply the cost monitor"]["run"]
    assert "34-cost-monitor.yaml" in render
    assert '"{{COST_MONITOR_IMAGE}}": "cost-monitor:ci"' in render
    assert 'irsa = {"AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE"}' in render
    assert '{"name": "COST_REPORT_BUCKET", "value": os.environ["COST_REPORT_BUCKET"]}' in render
    assert '"value": f"http://{s3_ip}:4566"' in render  # ClusterIP: path-style S3 addressing
    # The production OpenCost URL is asserted present, never injected: the
    # render adds emulator credentials and the bucket, nothing OpenCost-side.
    assert '{"name": "OPENCOST_BASE_URL"' not in render
    assert '>= {"OPENCOST_BASE_URL"' in render
    assert "networkpolicy allow-cost-monitor-to-opencost" in render
    assert "ci-allow-s3-emulator-egress" in render
    # Two containers now: picked by name, both on the loaded image, tracing
    # given its CI value, and only the listed opaque tokens stubbed.
    assert '(container,) = [c for c in containers if c["name"] == "cost-monitor"]' in render
    assert '(container,) = deployment["spec"]["template"]["spec"]["containers"]' not in render
    assert 'pod_container["imagePullPolicy"] = "Never"' in render
    assert '"{{TRACING_ENABLED}}": "false"' in render
    assert '"{{TRACING_SAMPLE_RATIO}}": "0.05"' in render
    assert "34-cost-monitor.yaml: no CI value for" in render
    assert 'get service cost-monitor -o jsonpath=\'{.spec.ports[*].port}\')" = "8443"' in render

    # The monitor's API is read over verified HTTPS through its TLS sidecar
    # (the manifest processor's hop), never its loopback-bound plaintext port.
    status = by_name["Wait for the cost monitor to see OpenCost returning data"]["run"]
    assert "/internal/status" in status
    assert 'status["opencost_returning_data"]' in status
    assert 'status["opencost_healthy"]' in status
    report = by_name["Generate an ad-hoc report and verify the Parquet object end to end"]["run"]
    for run in (status, report):
        assert "port-forward svc/cost-monitor 18443:8443" in run
        assert '--cacert "${COST_PIPELINE_CA_FILE}" --resolve "${api}:127.0.0.1"' in run
        assert 'api="cost-monitor.gco-system.svc.cluster.local:18443"' in run
        assert '"https://${api}/internal/' in run
        assert "18080" not in run
    assert "/internal/reports" in report
    assert "from gco.services.cost_monitor import ALLOCATION_REPORT_FIELDS" in report
    assert "table.column_names == list(ALLOCATION_REPORT_FIELDS)" in report
    assert "kubectl -n gco-system exec deploy/cost-monitor -c cost-monitor --" in report
    assert "aws s3api head-object" in report
    summary = by_name["Summary"]["run"]
    assert "exec deploy/cost-monitor -c cost-monitor --" in summary
    prometheus = by_name["Verify Prometheus scrapes the pinned OpenCost"]["run"]
    assert "node_total_hourly_cost" in prometheus
    # One probe command (a TLS client, so the loaded cost-monitor image), five
    # verdicts: the monitor reaches only OpenCost's TLS front door, only the
    # manifest processor reaches the monitor, and neither plaintext port nor
    # an unlisted peer gets through.
    netpol_name = "Verify the cost pipeline's NetworkPolicies admit only its TLS hops"
    netpol = by_name[netpol_name]["run"]
    assert netpol.count("--image=cost-monitor:ci") == 1
    assert "--image-pull-policy=Never" in netpol
    assert len(re.findall(r"kubectl (?:-n \S+ )?run ", netpol)) == 1
    assert "busybox" not in netpol
    assert "ssl.create_default_context(cadata=" in netpol
    assert "except TimeoutError:" in netpol
    for probe in (
        'probe gco-system netpol-probe-cost-monitor app=cost-monitor,project=gco "${opencost_tls}" 9443 reachable',
        "probe gco-system netpol-probe-plaintext app=cost-monitor,project=gco \\\n"
        "  opencost.monitoring.svc.cluster.local 9003 blocked",
        'probe gco-system netpol-probe-unlabelled app=netpol-probe "${opencost_tls}" 9443 blocked',
        "probe gco-system netpol-probe-manifest-processor app=manifest-processor,gco.aws/ci-only=true \\\n"
        '  "${cost_monitor}" 8443 reachable',
        'probe default netpol-probe-foreign app=netpol-probe "${cost_monitor}" 8443 blocked',
    ):
        assert probe in netpol, probe
    # busybox is no longer part of this job, so it is neither pulled nor loaded.
    assert "busybox" not in yaml.safe_dump(job)

    order = [step.get("name") or step.get("uses") for step in steps]
    kind_index = next(
        i
        for i, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("helm/kind-action")
    )
    assert (
        order.index("Install Helm")
        < order.index("Prefetch pinned Kind charts with retry")
        < kind_index
    )
    assert (
        order.index("Apply the shipped NetworkPolicies (Calico-enforced)")
        < order.index("Install pinned kube-prometheus-stack with shipped values")
        < order.index("Install pinned OpenCost with shipped values")
        < order.index("Apply the post-Helm cost TLS objects with a CI internal CA")
        < order.index("Render and apply the cost monitor")
        < order.index("Verify Prometheus scrapes the pinned OpenCost")
        < order.index("Wait for the cost monitor to see OpenCost returning data")
        < order.index("Generate an ad-hoc report and verify the Parquet object end to end")
        < order.index(netpol_name)
    )
    # cost-monitor:ci is on the node before anything runs it: the OpenCost
    # pod's TLS sidecar, the monitor itself, and the NetworkPolicy probes.
    assert order.index("Load the cost-monitor image into kind") < order.index(
        "Install pinned OpenCost with shipped values"
    )


def test_kind_examples_smoke_issues_the_shipped_internal_pki() -> None:
    """The shipped gco-internal-ca chain meets the pinned cert-manager in examples-smoke.

    It is the one kind job that already installs cert-manager, and the trainer
    install before the step proves the webhook answers. The step applies the
    issuance fence and then the PKI manifest, both unrendered (neither carries
    a placeholder), waits for both ClusterIssuers and the CA Certificate, and
    for every leaf requires Ready, ``ca.crt`` equal to the CA's certificate,
    and a strict X.509 verification for the name clients dial.

    Then, once a dry run shows the fence enforced, it proves the fence in the
    real API server: a tenant Certificate from gco-internal-ca is refused with
    the policy's message, widening gco-inference-tls is refused, a
    CertificateRequest from a non-cert-manager user in system:masters is
    refused, a tenant's own Issuer still issues, and cert-manager still
    re-issues a listed leaf that verifies against the CA. Every refusal fails
    the step if the API server admits it instead.
    tests/test_internal_tls_manifests.py evaluates the same probes against the
    policy's CEL. ValidatingAdmissionPolicy v1 needs Kubernetes 1.30 or later.
    """
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))
    steps = workflow["jobs"]["integration-kind-examples-smoke"]["steps"]
    order = [step.get("name") for step in steps]
    name = "Issue the shipped internal PKI with the pinned cert-manager"
    run = next(step["run"] for step in steps if step.get("name") == name)

    assert "manifests/08-internal-ca-issuance.yaml" in run
    assert "manifests/09-tenant-write-fence.yaml" in run
    assert "manifests/post-helm-api-workload-certificates.yaml" in run
    # Both fences are in before cert-manager issues anything, so it issues
    # gco-inference-tls, which only it may write, under the write fence.
    assert (
        run.index('kubectl apply -f "${fence_manifest}"')
        < run.index('kubectl apply -f "${write_fence_manifest}"')
        < run.index('kubectl apply -f "${manifest}"')
        < run.index('done 3< "${pki}/leaves.txt"')
    )
    fence_checks = [
        'kubectl apply --dry-run=server -f "${fence}/tenant-leaf.yaml"',
        'if kubectl apply -f "${fence}/tenant-leaf.yaml"',
        "grep -qF \"ValidatingAdmissionPolicy 'gco-internal-ca-issuance'\"",
        'grep -qF "${denied}" "${fence}/tenant-leaf.err"',
        "patch certificate gco-inference-tls --dry-run=server",
        'grep -qF "may carry only its own DNS names"',
        "if kubectl create --as=gco-ci-tenant --as-group=system:masters",
        'grep -qF "accepts requests only from cert-manager"',
        'kubectl apply -f "${fence}/tenant-own-issuer.yaml"',
        "wait --for=condition=Ready certificate/gco-ci-tenant-own-issuer",
        "kubectl -n gco-system delete secret health-monitor-tls",
        "-verify_hostname health-monitor.gco-system.svc.cluster.local",
        "kubectl -n gco-jobs delete certificate gco-ci-tenant-own-issuer",
        "kubectl -n gco-jobs delete issuer gco-internal-ca",
    ]
    positions = [run.index(fragment) for fragment in fence_checks]
    assert positions == sorted(positions)
    assert run.index('done 3< "${pki}/leaves.txt"') < positions[0]
    assert 'denied="the ClusterIssuer gco-internal-ca signs only the GCO platform leaves"' in run
    # Tag AND digest: kind re-pushes the same version tag for every kind
    # release, so a tag-only pin names a different image after each one.
    node_image = re.fullmatch(
        r"kindest/node:v1\.(\d+)\.\d+@sha256:[0-9a-f]{64}", workflow["env"]["KIND_NODE_IMAGE"]
    )
    assert node_image is not None and int(node_image.group(1)) >= 30
    for wait in (
        "kubectl wait --for=condition=Ready clusterissuer/gco-internal-ca-bootstrap",
        "kubectl -n cert-manager wait --for=condition=Ready certificate/gco-internal-ca",
        "kubectl wait --for=condition=Ready clusterissuer/gco-internal-ca --timeout",
        'wait --for=condition=Ready "certificate/${certificate}"',
    ):
        assert wait in run, wait
    assert "-fingerprint -sha256" in run
    assert "openssl verify -x509_strict -purpose sslserver" in run
    assert 'doc["spec"].get("isCA")' in run
    assert (
        order.index("Install pinned cert-manager (the trainer chart's cert dependency)")
        < order.index("Re-run the trainer install as an upgrade (idempotency contract)")
        < order.index(name)
    )


def test_kind_examples_smoke_proves_the_tenant_write_fence() -> None:
    """09-tenant-write-fence.yaml, probed in a real API server right after the PKI.

    Once a dry run shows the policy enforced, an identity in system:masters
    (so RBAC stops nothing) that is not the inference monitor is refused, with
    the policy's own messages: writing gco-inference-tls (the monitor too is
    refused that), creating a monitor-labelled ConfigMap, rewriting or
    unlabelling a monitor ConfigMap, recreating a deleted pod program without
    the label, changing or restarting a monitor Deployment's pod template,
    creating the shared mooncake-master, and forging a lifecycle id. In
    between, the same identity annotates, deletes and scales those objects and
    the monitor's ServiceAccount writes them, all admitted. The probe
    Deployment has zero replicas, so the job pulls nothing for it.
    tests/test_tenant_write_fence.py evaluates the same rules offline.
    """
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))
    steps = workflow["jobs"]["integration-kind-examples-smoke"]["steps"]
    order = [step.get("name") for step in steps]
    name = "Prove the tenant write fence in the real API server"
    assert (
        order.index(name)
        == order.index("Issue the shipped internal PKI with the pinned cert-manager") + 1
    )
    run = next(step["run"] for step in steps if step.get("name") == name)

    assert "policy=\"ValidatingAdmissionPolicy 'gco-tenant-write-fence'\"" in run
    assert "--as=gco-ci-tenant --as-group=system:masters" in run
    assert (
        "--as=system:serviceaccount:gco-system:gco-inference-monitor-sa --as-group=system:masters"
        in run
    )
    probes = [
        'kubectl "${tenant[@]}" create --dry-run=server -f "${work}/labelled.json"',
        'echo "::error::the API server never enforced gco-tenant-write-fence"',
        'refused "a tenant replacing the ca.crt of gco-inference-tls"',
        'refused "the inference monitor writing gco-inference-tls"',
        'refused "a tenant creating a monitor-labelled ConfigMap"',
        'kubectl "${monitor[@]}" create -f "${work}/program.json"',
        'refused "a tenant rewriting a monitor ConfigMap"',
        'refused "a tenant unlabelling a monitor ConfigMap"',
        "annotate configmap gco-ci-fence-tls-proxy gco-ci/note=admitted",
        "delete configmap gco-ci-fence-tls-proxy",
        'refused "a tenant recreating a deleted pod program unlabelled"',
        'kubectl "${monitor[@]}" create -f "${work}/deployment.json"',
        'refused "a tenant changing a monitor Deployment\'s pod template"',
        'refused "a tenant restarting a monitor Deployment"',
        "scale deployment/gco-ci-fence --replicas=0",
        "set env deployment/gco-ci-fence GCO_CI_FENCE=monitor",
        'refused "a tenant creating mooncake-master"',
        'refused "a tenant forging a lifecycle id"',
        "kubectl -n gco-inference delete deployment gco-ci-fence",
    ]
    positions = [run.index(probe) for probe in probes]
    assert positions == sorted(positions)
    # A refusal only counts with the policy's name and its own message.
    assert 'grep -qF "${policy}" "${work}/err"' in run
    assert 'grep -qF "${message}" "${work}/err"' in run
    assert 'echo "::error::the API server admitted ${what}"' in run
    for message in (
        "only the cert-manager controller",
        "the inference monitor mounts ConfigMaps named",
        "may set, change or remove the gco.io/lifecycle-id",
    ):
        assert message in run, message
    assert '"replicas":0' in run


def test_kind_examples_smoke_serves_mlflow_over_verified_https() -> None:
    """The MLflow HTTPS hop, end to end in examples-smoke.

    trust-manager installs from the shipped values right after cert-manager and
    may read Secrets only in its own namespace (never cert-manager's, where the
    CA key lives) and write ConfigMaps only in gco-jobs. The mlflow chart pod
    gets the TLS sidecar the regional stack builds, executed from the stack
    source, not restated; the only CI substitution is the image carrying the
    same tls_proxy.py. post-helm-mlflow-tls.yaml is applied as rendered, both
    leaves and the Bundle turn Ready/Synced, the gco-jobs bundle is exactly the
    internal CA certificate, the write fence keeps it trust-manager's, and the
    sidecar serves verified TLS. Then the REAL example runs as the processor SA
    over the shipped client policy, with DNS and 443 as the only CI additions.
    """
    workflow = yaml.safe_load(_read(".github/workflows/integration-tests.yml"))
    steps = workflow["jobs"]["integration-kind-examples-smoke"]["steps"]
    order = [step.get("name") for step in steps]
    by_name = {step.get("name"): step for step in steps if isinstance(step, dict)}

    install = "Install pinned trust-manager with shipped values"
    upgrade = "Re-run the trust-manager install as an upgrade (idempotency contract)"
    mlflow = "Install pinned mlflow chart with shipped values"
    stage = "Stage the TLS sidecar's pinned interpreter under a local tag (with retry)"
    publish = "Publish the MLflow CA bundle and serve MLflow over verified HTTPS"
    example = "Run the REAL mlflow tracking example over verified HTTPS as the processor SA"
    assert (
        order.index("Install pinned cert-manager (the trainer chart's cert dependency)")
        < order.index(install)
        < order.index(upgrade)
        < order.index("Issue the shipped internal PKI with the pinned cert-manager")
        < order.index("Prove the tenant write fence in the real API server")
        < order.index(stage)
        < order.index(mlflow)
        < order.index("Apply the post-Helm mlflow network policies")
        < order.index("Probe mlflow host validation (allowed 200 / arbitrary 403 / health exempt)")
        < order.index(publish)
        < order.index("Load the mlflow example's image into kind")
        < order.index(example)
    )

    run = by_name[install]["run"]
    assert "--emit-ref trust-manager" in run
    assert "--emit-values trust-manager" in run
    assert "--wait --timeout 5m" in run
    assert "crd/bundles.trust.cert-manager.io" in run
    for verdict in (
        'test "$(can_i get secrets "${namespace}")" = "yes"',
        'test "$(can_i get secrets cert-manager)" = "no"',
        'test "$(can_i create configmaps gco-jobs)" = "yes"',
        'test "$(can_i create configmaps gco-system)" = "no"',
        'test "$(can_i create secrets gco-jobs)" = "no"',
    ):
        assert verdict in run, verdict
    run = by_name[upgrade]["run"]
    assert 'if ! helm upgrade trust-manager "${TRUST_MANAGER_CHART_ARCHIVE}"' in run
    assert "validatingwebhookconfiguration trust-manager" in run

    run = by_name[mlflow]["run"]
    assert 'source = Path("gco/stacks/regional_stack.py")' in run
    assert 'helpers = {"_chart_tls_proxy_sidecar", "_mlflow_allowed_hosts"}' in run
    assert 'node.name == "_mlflow_chart_values"' in run
    assert 'manifest_processor_image=SimpleNamespace(image_uri="manifest-processor:ci")' in run
    assert 'sidecar["name"] == "mlflow-tls-proxy"' in run
    # The CI substitution is the carrier only: the pinned interpreter image the
    # inference monitor uses, running the shipped program from a ConfigMap. It
    # is staged on the node by digest under a local tag and never pulled, so an
    # ECR Public limit cannot fail the rollout.
    kind_step = next(
        step for step in steps if str(step.get("uses", "")).startswith("helm/kind-action")
    )
    cluster = kind_step["with"]["cluster_name"]
    assert steps.index(kind_step) < order.index(stage)
    local_image = workflow["jobs"]["integration-kind-examples-smoke"]["env"][
        "CI_TLS_PROXY_LOCAL_IMAGE"
    ]
    assert re.fullmatch(r"[a-z0-9-]+:[a-z0-9.-]+", local_image), local_image
    staged = by_name[stage]["run"]
    assert "from gco.services.inference_monitor import ENDPOINT_TLS_PROXY_IMAGE" in staged
    assert f"python3 .github/scripts/preload_kind_images.py --cluster {cluster}" in staged
    assert '--pinned "${image}=${CI_TLS_PROXY_LOCAL_IMAGE}"' in staged
    assert 'sidecar["image"] = os.environ["CI_TLS_PROXY_LOCAL_IMAGE"]' in run
    assert 'sidecar["imagePullPolicy"] = "Never"' in run
    assert "ENDPOINT_TLS_PROXY_IMAGE" not in run
    assert "--from-file=tls_proxy.py=gco/services/tls_proxy.py" in run
    assert 'sidecar["command"] = ["python3", "/etc/gco-tls-proxy/tls_proxy.py"]' in run
    assert '"nodeSelector": values["nodeSelector"]' in run
    assert '--values "${RUNNER_TEMP}/mlflow-ci-overlay.yaml"' in run
    assert 'containers[?(@.name=="mlflow-tls-proxy")].ports[?(@.name=="https")]' in run
    assert 'volumes[?(@.name=="gco-tls")].secret.optional}\')" = "true"' in run
    assert "get secret mlflow-tls" in run

    probe = by_name["Probe mlflow host validation (allowed 200 / arbitrary 403 / health exempt)"]
    assert '-H "Host: mlflow-tls.monitoring.svc.cluster.local:5443"' in probe["run"]
    assert 'test "$allowed_tls" = "200"' in probe["run"]

    run = by_name[publish]["run"]
    fragments = [
        "sed 's|{{MLFLOW_ENABLED}}|true|g' \"${manifest}\"",
        'kubectl apply -f "${rendered}"',
        "wait --for=condition=Ready certificate/mlflow-tls",
        "certificate/gco-internal-ca-source",
        "wait --for=condition=Synced bundles.trust.cert-manager.io/gco-internal-ca",
        "-verify_hostname mlflow-tls.monitoring.svc.cluster.local",
        'assert set(data) == {"ca.crt"}',
        '.count("-----BEGIN CERTIFICATE-----") == 1',
        '-noout -fingerprint -sha256)" != "${ca_fingerprint}"',
        '-o jsonpath=\'{.items[*].metadata.namespace}\')" = "gco-jobs"',
        "kubectl --as=gco-ci-tenant --as-group=system:masters -n gco-jobs",
        'grep -qF "${owned}"',
        "annotate configmap gco-internal-ca gco-ci/probe=admitted --dry-run=server",
        "-l kubernetes.io/service-name=mlflow-tls",
        "exec deploy/mlflow -c mlflow-tls-proxy -- python3 -c",
    ]
    positions = [run.index(fragment) for fragment in fragments]
    assert positions == sorted(positions)
    assert 'owned="only trust-manager (system:serviceaccount:${tm_namespace}:trust-manager)"' in run
    assert 'ssl.create_default_context(cafile="/var/run/gco/tls/ca.crt")' in run

    run = by_name[example]["run"]
    assert 'kubectl apply --as="$PROCESSOR_SA" -f examples/mlflow-tracking-job.yaml' in run
    assert 'grep -q "Read-back verified"' in run
    # The CI-only policy selects MLflow clients only and adds DNS and 443,
    # never the server's ports: those stay the shipped policy's to grant.
    policy_text = run[run.index("apiVersion: networking.k8s.io/v1") : run.index("EOF\n")]
    policy = yaml.safe_load(policy_text)
    assert policy["spec"]["podSelector"] == {"matchLabels": {"gco.io/mlflow-client": "true"}}
    ports = {entry["port"] for rule in policy["spec"]["egress"] for entry in rule["ports"]}
    assert ports == {53, 5353, 443}
    assert all("to" not in rule for rule in policy["spec"]["egress"])
    assert "kubectl -n gco-jobs delete networkpolicy gco-ci-mlflow-client-dns-and-https" in run


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
    # Piping curl into kubectl would evade the URL regex above while skipping
    # the checksum; every downloaded manifest lands in a file first.
    assert not re.search(r"curl[^\n]*\|\s*kubectl\s+apply", workflow)
    # The retired hosted-Argo CD job fetched CRDs from a movable Git tag; the
    # self-managed chart ships its CRDs, so no such download may come back.
    assert "argoproj/argo-cd/" not in workflow
    assert "ARGOCD_VERSION" not in workflow


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
    # The Crossplane Function package in post-helm-crossplane.yaml is an xpkg
    # reference, not an ``image:`` line or a charts.yaml pin, so nothing but
    # this scan would notice it ageing. The retired Argo CD CRD tag is gone.
    assert "FUNCTION_GO_TEMPLATING_PIN=" in scanner
    assert "extract_crossplane_function_pin" in scanner
    assert '"crossplane-contrib/function-go-templating"' in scanner
    # The package is also pinned by digest. The digest gets the same
    # committed-vs-published check as every other digest pin, and ``no`` keeps
    # the package out of the image sweep, which would report the GitHub
    # release check's drift a second time.
    assert "extract_crossplane_function_packages" in scanner
    assert (
        'check_pinned_digest "$package_ref" '
        '"lambda/kubectl-applier-simple/manifests/post-helm-crossplane.yaml" no'
    ) in scanner
    assert "ARGOCD_PIN" not in scanner
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


def _scan_completeness_arguments(scanner: str) -> str:
    """Return the argument list passed to ``dependency_scan_is_complete``.

    Matching the whole call rather than ``"$X_SKIP_REASON"; then`` means adding a
    surface to the end of the list cannot break the assertions for the ones
    already there.
    """
    match = re.search(r"dependency_scan_is_complete \\\n(?P<args>.*?); then", scanner, re.DOTALL)
    assert match is not None, "could not locate the dependency_scan_is_complete call"
    return match.group("args")


def test_runner_images_are_covered_by_the_monthly_drift_scan() -> None:
    """``runs-on:`` is not a version pin, so only this scan can catch it ageing.

    The two properties worth locking down: the result has to reach the report
    (counted, summary row, all-clear condition), and a failed catalog read has to
    become a recorded skip — otherwise an upstream README change would silently
    turn the check into a permanent all-clear.
    """
    scanner = _read(".github/scripts/dependency-scan.sh")

    assert "=== Checking runner images ===" in scanner
    assert "check_runner_images.py --format rows" in scanner
    assert "check_runner_images.py --format notes" in scanner, (
        "preview images must be reported separately from actionable drift"
    )
    assert 'summary_row "Runner Images"' in scanner
    assert "RUNNER_IMAGE_SKIP_REASON" in _scan_completeness_arguments(scanner), (
        "RUNNER_IMAGE_SKIP_REASON must reach dependency_scan_is_complete, otherwise a "
        "failed catalog read leaves the scan claiming completeness"
    )
    assert '[ "$RUNNER_IMAGE_COUNT" -eq 0 ]' in scanner
    assert scanner.count('"$RUNNER_IMAGE_RESULTS"') >= 3


def test_ruby_interpreter_pin_is_covered_by_the_monthly_drift_scan() -> None:
    """The Ruby series is pinned like Python's, so it needs the same currency check.

    Dependabot watches the gems in Gemfile.lock but says nothing about the
    interpreter, so without this the pin could sit on an end-of-life series
    indefinitely. ``.ruby-version`` is compared against endoflife.date in the
    monthly scan, and the result has to reach the report: counted, given a
    summary row, and — when the lookup fails — recorded as a skip so a failed
    query cannot read as "up to date".
    """
    pin = _read(".ruby-version").strip()
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", pin), (
        f".ruby-version must hold a bare Ruby series or version, found {pin!r}"
    )

    library = _read(".github/scripts/lib_dependency_scan.sh")
    assert "get_latest_endoflife_cycle()" in library
    assert "get_latest_ruby_release()" in library
    assert "read_ruby_version_pin()" in library

    scanner = _read(".github/scripts/dependency-scan.sh")
    assert "=== Checking Ruby release ===" in scanner
    assert 'RUBY_PIN_CURRENT="$(read_ruby_version_pin .ruby-version)"' in scanner
    assert 'LATEST_RUBY="$(get_latest_ruby_release)"' in scanner
    assert 'summary_row "Ruby Release"' in scanner
    assert "RUBY_RELEASE_SKIP_REASON" in _scan_completeness_arguments(scanner), (
        "RUBY_RELEASE_SKIP_REASON must be passed to dependency_scan_is_complete, "
        "otherwise a failed endoflife.date lookup leaves the scan claiming completeness"
    )
    assert '[ "$RUBY_RELEASE_COUNT" -eq 0 ]' in scanner, (
        "the all-clear condition must include the Ruby count"
    )
    assert scanner.count('"$RUBY_RELEASE_RESULTS"') >= 3, (
        "the Ruby results tempfile must be written, rendered and cleaned up"
    )


def test_ruby_version_file_is_the_only_source_of_the_ci_ruby() -> None:
    """One pin, read by the workflow — never a literal duplicated in YAML."""
    literals: list[str] = []
    for workflow in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for match in re.finditer(
            r"ruby-version:\s*[\"']?([^\"'\n]+)[\"']?", workflow.read_text(encoding="utf-8")
        ):
            value = match.group(1).strip()
            if value != ".ruby-version":
                literals.append(f"{workflow.name}: {value}")
    assert not literals, (
        f"workflows must set ruby-version to '.ruby-version', not a literal: {literals}"
    )

    step = _workflow_job_step(".github/workflows/unit-tests.yml", "unit-bats-shell", "Set up Ruby")
    assert step["with"]["ruby-version"] == ".ruby-version"


def test_gem_dependencies_are_exactly_pinned_and_locked() -> None:
    """Gems get the same treatment as pip and npm: exact pins plus a lockfile."""
    gemfile = _read("Gemfile")
    declared = re.findall(r'^gem "([^"]+)", "([^"]+)"$', gemfile, re.MULTILINE)
    assert declared, "Gemfile declares no exactly-pinned gems"
    for name, version in declared:
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
            f"gem {name} must be pinned to an exact version, found {version!r}"
        )

    lock = _read("Gemfile.lock")
    assert "CHECKSUMS" in lock, (
        "Gemfile.lock must carry the Bundler CHECKSUMS block so a republished "
        "gem cannot change under us"
    )
    assert "BUNDLED WITH" in lock
    for name, version in declared:
        assert f"{name} ({version}) sha256=" in lock, f"no committed checksum for {name} {version}"

    dependabot = yaml.safe_load(_read(".github/dependabot.yml"))
    ecosystems = {entry["package-ecosystem"] for entry in dependabot["updates"]}
    assert "bundler" in ecosystems, "Dependabot must watch the bundler ecosystem"


def test_shell_coverage_gate_installs_the_committed_gem_lock() -> None:
    """The bats job must install exactly Gemfile.lock, then enforce the floor."""
    install = _workflow_job_step(
        ".github/workflows/unit-tests.yml", "unit-bats-shell", "Install the committed gem lock"
    )["run"]
    assert "bundle config set --local frozen true" in install, (
        "without frozen, Bundler silently re-resolves instead of failing on drift"
    )
    assert "bundle install" in install

    # The correctness gate stays uninstrumented: the suite runs once plain, as a
    # contributor runs it, and once traced. The traced run goes through
    # tests/BATS/bashcov_wrapper.sh rather than `bashcov -- bats`, because
    # bashcov's own SHELLOPTS propagation dragged bats's `nounset`/`errexit`/
    # `pipefail` into scripts that never opted into them; the wrapper carries
    # tracing through BASH_ENV instead, so both runs execute the same suite and
    # both exit codes gate the job.
    plain = _workflow_job_step(
        ".github/workflows/unit-tests.yml", "unit-bats-shell", "Run BATS suite"
    )["run"]
    assert "bats tests/BATS/" in plain
    assert "bashcov" not in plain, (
        "the authoritative BATS run must not be instrumented; coverage is measured "
        "by the separate traced run"
    )

    measure = _workflow_job_step(
        ".github/workflows/unit-tests.yml", "unit-bats-shell", "Measure shell coverage"
    )["run"]
    assert "bundle exec bashcov" in measure
    assert "tests/BATS/bashcov_wrapper.sh tests/BATS/" in measure, (
        "the traced run must go through the wrapper (over the whole suite), not "
        "`bashcov -- bats`, or instrumentation leaks into the scripts under test"
    )
    assert "bashcov -- bats" not in measure

    gate = _workflow_job_step(
        ".github/workflows/unit-tests.yml", "unit-bats-shell", "Enforce the shell coverage floor"
    )["run"]
    assert "check_bash_coverage.py" in gate
    assert "--report coverage/report" in gate, (
        "the statement-level report pages.yml publishes is written by the gate step"
    )


def test_dependency_scanner_remains_directly_executable() -> None:
    mode = (ROOT / ".github/scripts/dependency-scan.sh").stat().st_mode

    assert mode & stat.S_IXUSR


def test_incomplete_reports_do_not_claim_zero_count_surfaces_are_current() -> None:
    scanner = _read(".github/scripts/dependency-scan.sh")

    assert "No drift was found in completed checks, but the scan is incomplete." in scanner
    assert 'label="no drift found (incomplete scan)"' in scanner
    assert "Zero-count surfaces are provisional, not confirmed current." in scanner


def test_release_stage_one_commits_every_version_bump_output() -> None:
    """The workflow must stage every tracked file the bumper maintains."""
    step = _workflow_step(".github/workflows/release.yml", "Push release branch")
    matches = re.findall(r"^\s*git add (.+)$", step, re.MULTILINE)

    assert len(matches) == 1
    assert set(matches[0].split()) == {
        "VERSION",
        "README.md",
        "gco/_version.py",
        "cli/__init__.py",
        "gco_mcp/README.md",
    }
    assert step.index("git add ") < step.index("git diff --exit-code") < step.index("git commit ")


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
    assert "ARG BUILDX_VERSION=v0.38.0" in buildx_section
    assert "buildx-${BUILDX_VERSION}.linux-${TARGETARCH}" in buildx_section
    assert (
        'amd64) BUILDX_SHA256="4fe4cc38adf48169132749b6ca22a990928db0118e3407584ee553723115d287"'
    ) in buildx_section
    assert (
        'arm64) BUILDX_SHA256="38e890e1a162bfdbf32fbe91404991dc4fc28e55b0de901b34374a061bab4d2c"'
    ) in buildx_section
    assert (
        'echo "${BUILDX_SHA256}  /usr/local/lib/docker/cli-plugins/docker-buildx" | sha256sum -c -'
    ) in buildx_section

    assert "ARG HELM_SHA256" not in installer_dockerfile
    assert "ARG KUBECTL_SHA256" not in installer_dockerfile
    assert "helm-v4.3.0-linux-amd64.tar.gz" in helm_section
    assert (
        "86584a54def73570558f66f5111cc53dfed56689637ae32c1201205d494f54fb  /tmp/helm.tar.gz"
    ) in helm_section
    assert "release/v1.37.1/bin/linux/amd64/kubectl" in kubectl_section
    assert (
        "65691ff77eb6fa44c908b77a1082c9f092c3b9733b5cefabec0d1104890e21a8  /tmp/kubectl"
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
        "amd64": ("ubuntu-26.04", "x86_64", "0x3E"),
        "arm64": ("ubuntu-26.04-arm", "aarch64", "0xB7"),
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


def test_npm_audit_retries_a_registry_operational_error() -> None:
    """`npm audit`'s advisory-bulk fetch against registry.npmjs.org has
    returned a transient 503, which npm reports as {"error": {...}} instead
    of a real {"vulnerabilities": {...}} report. That single unlucky fetch
    must not fail the job the way it did in CI — it is retried before the
    checker script ever sees the report."""
    step = _workflow_job_step(
        ".github/workflows/security.yml",
        "security-npm-audit",
        "Install and audit every locked npm graph",
    )
    body = step["run"]

    assert "AUDIT_ATTEMPTS" in step.get("env", {})
    assert re.search(r"npm audit .*--json", body, re.DOTALL)
    assert "isinstance(data.get('error'), dict)" in body
    assert 'sleep "$AUDIT_RETRY_DELAY"' in body
    # The retry loop must run before the exact/expiring checker, so a
    # transient fetch failure never reaches the finding-comparison logic.
    retry_index = body.index("while true; do")
    checker_index = body.index("check_npm_audit.py")
    assert retry_index < checker_index


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
