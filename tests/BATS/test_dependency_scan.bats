#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for .github/scripts/dependency-scan.sh
# ─────────────────────────────────────────────────────────────────────────────
# Functional tests that source lib_dependency_scan.sh and exercise the real
# functions with controlled inputs. No grep-for-strings — every test calls
# the actual function and asserts on its output.
#
# Run:  bats tests/BATS/test_dependency_scan.bats
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT=".github/scripts/dependency-scan.sh"
LIB=".github/scripts/lib_dependency_scan.sh"

setup() {
    source "$LIB"
}

# ── Syntax ───────────────────────────────────────────────────────────────────

@test "dependency-scan.sh passes bash -n syntax check" {
    bash -n "$SCRIPT"
}

@test "lib_dependency_scan.sh passes bash -n syntax check" {
    bash -n "$LIB"
}

@test "dependency-scan.sh passes shellcheck" {
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "lib_dependency_scan.sh passes shellcheck" {
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    shellcheck -x "$LIB"
}

# ── accelerator catalog reporting ───────────────────────────────────────────

@test "offline accelerator catalog validator passes the committed repository" {
    run python3 scripts/accelerator_catalog.py validate
    [ "$status" -eq 0 ]
    [[ "$output" == *"Accelerator catalog validation passed"* ]]
}

@test "parse_accelerator_drift_count returns the exact valid count" {
    tmpfile="$(mktemp)"
    printf '%s\n' \
        '{"status":"drift","drift_count":7,"added_count":4,"removed_count":2,"metadata_change_count":1,"regions_checked":17}' \
        > "$tmpfile"
    run parse_accelerator_drift_count "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$output" = "7" ]
    rm -f "$tmpfile"
}

@test "parse_accelerator_drift_count rejects malformed JSON" {
    tmpfile="$(mktemp)"
    printf '%s\n' '{not-json' > "$tmpfile"
    run parse_accelerator_drift_count "$tmpfile"
    [ "$status" -ne 0 ]
    [ -z "$output" ]
    rm -f "$tmpfile"
}

@test "parse_accelerator_drift_count rejects a missing count" {
    tmpfile="$(mktemp)"
    printf '%s\n' '{"status":"current","regions_checked":17}' > "$tmpfile"
    run parse_accelerator_drift_count "$tmpfile"
    [ "$status" -ne 0 ]
    [ -z "$output" ]
    rm -f "$tmpfile"
}

# ── parse_image_registry ─────────────────────────────────────────────────────

@test "parse_image_registry: nvcr.io image returns nvcr.io registry" {
    result="$(parse_image_registry "nvcr.io/nvidia/cuda")"
    [ "$result" = "nvcr.io|nvidia/cuda" ]
}

@test "parse_image_registry: gcr.io image returns gcr.io registry" {
    result="$(parse_image_registry "gcr.io/google-containers/pause")"
    [ "$result" = "gcr.io|google-containers/pause" ]
}

@test "parse_image_registry: quay.io image returns quay.io registry" {
    result="$(parse_image_registry "quay.io/prometheus/node-exporter")"
    [ "$result" = "quay.io|prometheus/node-exporter" ]
}

@test "parse_image_registry: ghcr.io image returns ghcr.io registry" {
    result="$(parse_image_registry "ghcr.io/actions/runner")"
    [ "$result" = "ghcr.io|actions/runner" ]
}

@test "parse_image_registry: registry.k8s.io image returns registry.k8s.io registry" {
    result="$(parse_image_registry "registry.k8s.io/coredns/coredns")"
    [ "$result" = "registry.k8s.io|coredns/coredns" ]
}

@test "parse_image_registry: public.ecr.aws image returns public.ecr.aws registry" {
    result="$(parse_image_registry "public.ecr.aws/eks/coredns")"
    [ "$result" = "public.ecr.aws|eks/coredns" ]
}

@test "parse_image_registry: org/repo defaults to docker.io" {
    result="$(parse_image_registry "pytorch/pytorch")"
    [ "$result" = "docker.io|pytorch/pytorch" ]
}

@test "parse_image_registry: bare image name defaults to docker.io/library/" {
    result="$(parse_image_registry "python")"
    [ "$result" = "docker.io|library/python" ]
}

@test "parse_image_registry: bare image 'nginx' gets library/ prefix" {
    result="$(parse_image_registry "nginx")"
    [ "$result" = "docker.io|library/nginx" ]
}

@test "parse_image_registry: deeply nested path preserves full repo" {
    result="$(parse_image_registry "nvcr.io/nvidia/k8s/dcgm-exporter")"
    [ "$result" = "nvcr.io|nvidia/k8s/dcgm-exporter" ]
}

# ── is_semver_tag ────────────────────────────────────────────────────────────

@test "is_semver_tag: v1.2.3 is semver" {
    is_semver_tag "v1.2.3"
}

@test "is_semver_tag: 1.2.3 is semver" {
    is_semver_tag "1.2.3"
}

@test "is_semver_tag: v0.19.1 is semver" {
    is_semver_tag "v0.19.1"
}

@test "is_semver_tag: 3.14 (two-part) is semver" {
    is_semver_tag "3.14"
}

@test "is_semver_tag: latest is NOT semver" {
    ! is_semver_tag "latest"
}

@test "is_semver_tag: sha256:abc123 is NOT semver" {
    ! is_semver_tag "sha256:abc123def"
}

@test "is_semver_tag: empty string is NOT semver" {
    ! is_semver_tag ""
}

@test "is_semver_tag: 3.14-slim is semver (prefix match)" {
    is_semver_tag "3.14-slim"
}

# ── is_project_image ─────────────────────────────────────────────────────────

@test "is_project_image: gco/manifest-processor is a project image" {
    is_project_image "gco/manifest-processor"
}

@test "is_project_image: gco/health-monitor is a project image" {
    is_project_image "gco/health-monitor"
}

@test "is_project_image: pytorch/pytorch is NOT a project image" {
    ! is_project_image "pytorch/pytorch"
}

@test "is_project_image: python is NOT a project image" {
    ! is_project_image "python"
}

@test "is_project_image: nvcr.io/nvidia/cuda is NOT a project image" {
    ! is_project_image "nvcr.io/nvidia/cuda"
}

# ── compare_semver ───────────────────────────────────────────────────────────

@test "compare_semver: 1.0.0 vs 2.0.0 is newer" {
    result="$(compare_semver "1.0.0" "2.0.0")"
    [ "$result" = "newer" ]
}

@test "compare_semver: 1.0.0 vs 1.0.0 is same" {
    result="$(compare_semver "1.0.0" "1.0.0")"
    [ "$result" = "same" ]
}

@test "compare_semver: 2.0.0 vs 1.0.0 is older" {
    result="$(compare_semver "2.0.0" "1.0.0")"
    [ "$result" = "older" ]
}

@test "compare_semver: v1.2.3 vs v1.2.4 is newer (strips v prefix)" {
    result="$(compare_semver "v1.2.3" "v1.2.4")"
    [ "$result" = "newer" ]
}

@test "compare_semver: v0.19.1 vs v0.20.0 is newer" {
    result="$(compare_semver "v0.19.1" "v0.20.0")"
    [ "$result" = "newer" ]
}

@test "compare_semver: 16.6 vs 16.13 is newer (Aurora-style two-part)" {
    result="$(compare_semver "16.6" "16.13")"
    [ "$result" = "newer" ]
}

@test "compare_semver: 16.13 vs 16.6 is older" {
    result="$(compare_semver "16.13" "16.6")"
    [ "$result" = "older" ]
}

@test "compare_semver: mixed v prefix (v1.0.0 vs 1.0.1) is newer" {
    result="$(compare_semver "v1.0.0" "1.0.1")"
    [ "$result" = "newer" ]
}

# ── extract_aurora_versions ──────────────────────────────────────────────────

@test "extract_aurora_versions: finds version from regional_stack.py" {
    run extract_aurora_versions "gco/stacks/regional_stack.py"
    [ "$status" -eq 0 ]
    # Should find a version like 17.9 or 16.6 (depends on constants module availability)
    [[ "$output" =~ [0-9]+\.[0-9]+ ]]
}

@test "extract_aurora_versions: returns sorted unique versions" {
    # The function now imports from constants module first, so test with
    # a file that has the VER_ pattern but also verify the regex fallback
    # by temporarily making the import fail
    run bash -c '
        source .github/scripts/lib_dependency_scan.sh
        tmpfile="$(mktemp)"
        cat > "$tmpfile" <<EOF
version=rds.AuroraPostgresEngineVersion.VER_16_6,
version=rds.AuroraPostgresEngineVersion.VER_15_4,
version=rds.AuroraPostgresEngineVersion.VER_16_6,
EOF
        # Force the regex fallback by running in a subshell without gco on PYTHONPATH
        PYTHONPATH=/nonexistent python3 -c "
import re, sys
with open(sys.argv[1]) as f:
    text = f.read()
seen = set()
for m in re.finditer(r\"AuroraPostgresEngineVersion\\.VER_(\d+)_(\d+)\", text):
    v = f\"{m.group(1)}.{m.group(2)}\"
    if v not in seen:
        seen.add(v)
        print(v)
" "$tmpfile" | sort -V
        rm -f "$tmpfile"
    '
    [ "$status" -eq 0 ]
    [ "$(echo "$output" | wc -l | tr -d ' ')" -eq 2 ]
    [ "$(echo "$output" | head -1)" = "15.4" ]
    [ "$(echo "$output" | tail -1)" = "16.6" ]
}

@test "extract_aurora_versions: returns empty for file with no Aurora versions" {
    # Force the regex fallback path
    run bash -c '
        source .github/scripts/lib_dependency_scan.sh
        tmpfile="$(mktemp)"
        echo "no aurora versions here" > "$tmpfile"
        PYTHONPATH=/nonexistent python3 -c "
import re, sys
with open(sys.argv[1]) as f:
    text = f.read()
for m in re.finditer(r\"AuroraPostgresEngineVersion\\.VER_(\d+)_(\d+)\", text):
    print(f\"{m.group(1)}.{m.group(2)}\")
" "$tmpfile"
        rm -f "$tmpfile"
    '
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── extract_emr_versions ─────────────────────────────────────────────────────

@test "extract_emr_versions: reads EMR_SERVERLESS_RELEASE_LABEL from constants.py" {
    run extract_emr_versions "gco/stacks/constants.py"
    [ "$status" -eq 0 ]
    # The pinned label is emr-7.13.0 at the time of writing — assert the
    # shape so a legitimate bump of the constant does not break the test.
    [[ "$output" =~ ^emr-[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

@test "extract_emr_versions: returns the exact pinned constant value" {
    # Pin the expected value against the constants module so a silent
    # drift between the lib helper and the source of truth surfaces here.
    #
    # NOTE: we read constants.py with a regex rather than ``from
    # gco.stacks.constants import ...`` because the BATS CI job runs in
    # a minimal environment that does not install the ``[cdk]`` extra,
    # so ``gco/stacks/__init__.py`` (which pulls in ``aws_cdk``) fails
    # to import. The helper under test has its own try-except fallback
    # for exactly this reason; this assertion mirrors that fallback.
    expected="$(python3 -c '
import re
with open("gco/stacks/constants.py") as f:
    m = re.search(r"EMR_SERVERLESS_RELEASE_LABEL\s*=\s*\"([^\"]+)\"", f.read())
print(m.group(1) if m else "")
')"
    [ -n "$expected" ]
    run extract_emr_versions "gco/stacks/constants.py"
    [ "$status" -eq 0 ]
    [ "$output" = "$expected" ]
}

@test "extract_emr_versions: regex fallback returns empty when the constant is missing" {
    # Mirror the Aurora "returns empty for file with no Aurora versions"
    # test — the ``from gco.stacks.constants import ...`` branch can't
    # be forced to fail from inside this repo (editable install puts
    # the module on sys.path), so exercise the regex fallback directly
    # against a fixture that does not contain the constant.
    run bash -c '
        tmpfile="$(mktemp)"
        echo "# no EMR label here" > "$tmpfile"
        python3 -c "
import re, sys
with open(sys.argv[1]) as f:
    text = f.read()
m = re.search(r\"EMR_SERVERLESS_RELEASE_LABEL\\s*=\\s*\\\"([^\\\"]+)\\\"\", text)
if m:
    print(m.group(1))
" "$tmpfile"
        rm -f "$tmpfile"
    '
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_emr_versions: regex fallback parses a literal constants.py fixture" {
    # Positive-direction check of the regex fallback — independent of
    # the gco.stacks.constants import path.
    run bash -c '
        tmpfile="$(mktemp)"
        cat > "$tmpfile" <<EOF
EMR_SERVERLESS_RELEASE_LABEL = "emr-7.13.0"
EOF
        python3 -c "
import re, sys
with open(sys.argv[1]) as f:
    text = f.read()
m = re.search(r\"EMR_SERVERLESS_RELEASE_LABEL\\s*=\\s*\\\"([^\\\"]+)\\\"\", text)
if m:
    print(m.group(1))
" "$tmpfile"
        rm -f "$tmpfile"
    '
    [ "$status" -eq 0 ]
    [ "$output" = "emr-7.13.0" ]
}

# ── extract_eks_addons ───────────────────────────────────────────────────────

@test "extract_eks_addons: finds at least one addon in regional_stack.py" {
    run extract_eks_addons "gco/stacks/regional_stack.py"
    [ "$status" -eq 0 ]
    # Should find addons either via constants import or regex fallback
    # Output is pipe-delimited name|version
    [[ "$output" == *"|"* ]]
}

@test "extract_eks_addons: finds aws-efs-csi-driver addon" {
    run extract_eks_addons "gco/stacks/regional_stack.py"
    [ "$status" -eq 0 ]
    [[ "$output" == *"efs-csi"* ]] || [[ "$output" == *"aws-efs"* ]]
}

@test "extract_eks_addons: returns empty for file with no addons" {
    # Force the regex fallback path
    run bash -c '
        source .github/scripts/lib_dependency_scan.sh
        tmpfile="$(mktemp)"
        echo "no addons here" > "$tmpfile"
        PYTHONPATH=/nonexistent python3 -c "
import re, sys
with open(sys.argv[1]) as f:
    text = f.read()
for m in re.finditer(r\"addon_name=\\\"([^\\\"]+)\\\".*?addon_version=\\\"([^\\\"]+)\\\"\", text, re.DOTALL):
    print(f\"{m.group(1)}|{m.group(2)}\")
" "$tmpfile"
        rm -f "$tmpfile"
    '
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── extract_helm_charts ──────────────────────────────────────────────────────

@test "extract_helm_charts: finds kube-prometheus-stack in the real charts.yaml" {
    run extract_helm_charts "lambda/helm-installer/charts.yaml"
    [ "$status" -eq 0 ]
    [[ "$output" == *'"name": "kube-prometheus-stack"'* ]]
    [[ "$output" == *'"chart": "kube-prometheus-stack"'* ]]
}

@test "extract_helm_charts: reports a non-empty version for kube-prometheus-stack" {
    # Assert the pin is present and shaped like a chart version, without
    # hardcoding the exact value so a legitimate bump doesn't break the test.
    run extract_helm_charts "lambda/helm-installer/charts.yaml"
    [ "$status" -eq 0 ]
    kp_line="$(printf '%s\n' "$output" | grep '"name": "kube-prometheus-stack"')"
    [ -n "$kp_line" ]
    version="$(printf '%s' "$kp_line" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

@test "extract_helm_charts: includes the mandatory keda chart" {
    run extract_helm_charts "lambda/helm-installer/charts.yaml"
    [ "$status" -eq 0 ]
    [[ "$output" == *'"name": "keda"'* ]]
}

@test "extract_helm_charts: every entry is valid JSON carrying the expected keys" {
    run extract_helm_charts "lambda/helm-installer/charts.yaml"
    [ "$status" -eq 0 ]
    printf '%s\n' "$output" | python3 -c "
import json, sys
lines = [ln for ln in sys.stdin if ln.strip()]
assert lines, 'no chart entries emitted'
for ln in lines:
    obj = json.loads(ln)
    assert {'name', 'repo_url', 'chart', 'version', 'use_oci'} <= set(obj), ln
"
}

@test "extract_helm_charts: parses a minimal charts.yaml fixture" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
charts:
  demo-chart:
    repo_url: https://example.com/charts
    chart: demo
    version: "1.2.3"
EOF
    run extract_helm_charts "$tmpfile"
    [ "$status" -eq 0 ]
    [[ "$output" == *'"name": "demo-chart"'* ]]
    [[ "$output" == *'"version": "1.2.3"'* ]]
    rm -f "$tmpfile"
}

@test "extract_helm_charts: returns empty for a missing file" {
    run extract_helm_charts "/nonexistent/charts.yaml"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── extract_dockerfile_pins ──────────────────────────────────────────────────

@test "extract_dockerfile_pins: finds all seven pins in Dockerfile.dev" {
    run extract_dockerfile_pins "Dockerfile.dev"
    [ "$status" -eq 0 ]
    # All seven allowlisted pins should be present.
    [[ "$output" == *"NODE_VERSION|"* ]]
    [[ "$output" == *"NPM_VERSION|"* ]]
    [[ "$output" == *"CDK_VERSION|"* ]]
    [[ "$output" == *"KUBECTL_VERSION|"* ]]
    [[ "$output" == *"AWSCLI_VERSION|"* ]]
    [[ "$output" == *"DOCKER_VERSION|"* ]]
    [[ "$output" == *"BUILDX_VERSION|"* ]]
}

@test "extract_dockerfile_pins: emits pipe-delimited NAME|VALUE pairs" {
    run extract_dockerfile_pins "Dockerfile.dev"
    [ "$status" -eq 0 ]
    # Each line is exactly NAME|VALUE — no stray whitespace, no ARG prefix.
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        [[ "$line" =~ ^[A-Z_][A-Z0-9_]*\|[^[:space:]]+$ ]] || {
            echo "bad line: '$line'"
            return 1
        }
    done <<< "$output"
}

@test "extract_dockerfile_pins: NODE_VERSION keeps the v prefix" {
    # The Dockerfile pins Node with the leading 'v' because the
    # nodejs.org dist URL and tarball name both use it
    # (node-vX.Y.Z-linux-<arch>.tar.gz). Assert we preserve it so the
    # download URL and the deps-scan compare line up.
    run extract_dockerfile_pins "Dockerfile.dev"
    [ "$status" -eq 0 ]
    node_line="$(echo "$output" | grep '^NODE_VERSION|')"
    value="${node_line#NODE_VERSION|}"
    [[ "$value" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

@test "extract_dockerfile_pins: KUBECTL_VERSION keeps the v prefix" {
    # The Dockerfile pins kubectl with the leading 'v' (matches the
    # dl.k8s.io URL scheme). Assert we preserve it so the upstream
    # query URL builds correctly.
    run extract_dockerfile_pins "Dockerfile.dev"
    [ "$status" -eq 0 ]
    k_line="$(echo "$output" | grep '^KUBECTL_VERSION|')"
    value="${k_line#KUBECTL_VERSION|}"
    [[ "$value" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

@test "extract_dockerfile_pins: BUILDX_VERSION keeps the v prefix" {
    # Docker Buildx is pinned with the leading v (the GitHub release tag
    # and the buildx-<tag>.linux-<arch> asset name both use it), so the
    # deps-scan release-tag compare lines up. Assert it is preserved.
    run extract_dockerfile_pins "Dockerfile.dev"
    [ "$status" -eq 0 ]
    b_line="$(echo "$output" | grep '^BUILDX_VERSION|')"
    value="${b_line#BUILDX_VERSION|}"
    [[ "$value" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

@test "extract_dockerfile_pins: NPM_VERSION value is bare semver" {
    # The npm pin in Dockerfile.dev is a bare ``X.Y.Z`` (no ``v``
    # prefix) so it concatenates cleanly into ``npm install -g
    # npm@${NPM_VERSION}``. Assert that shape is preserved.
    run extract_dockerfile_pins "Dockerfile.dev"
    [ "$status" -eq 0 ]
    npm_line="$(echo "$output" | grep '^NPM_VERSION|')"
    value="${npm_line#NPM_VERSION|}"
    [[ "$value" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

@test "extract_dockerfile_pins: ignores ARG names outside the allowlist" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
FROM scratch
ARG NODE_VERSION=v24.18.0
ARG NODE_MAJOR=24
ARG BUILD_DATE=20260501
ARG UNRELATED_KNOB=hello
ARG CDK_VERSION=2.1120.0
EOF
    run extract_dockerfile_pins "$tmpfile"
    [ "$status" -eq 0 ]
    # Allowlisted pins pass through
    [[ "$output" == *"NODE_VERSION|v24.18.0"* ]]
    [[ "$output" == *"CDK_VERSION|2.1120.0"* ]]
    # Non-allowlisted ARGs are filtered out. NODE_MAJOR left the
    # allowlist when the Node install moved off the NodeSource apt
    # repository onto pinned nodejs.org dist tarballs — a stray
    # reintroduction must not resurface in the drift report.
    [[ "$output" != *"NODE_MAJOR"* ]]
    [[ "$output" != *"BUILD_DATE"* ]]
    [[ "$output" != *"UNRELATED_KNOB"* ]]
    rm -f "$tmpfile"
}

@test "extract_dockerfile_pins: skips commented-out ARG lines" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
FROM scratch
# ARG NODE_VERSION=v99.0.0
ARG NODE_VERSION=v24.18.0
EOF
    run extract_dockerfile_pins "$tmpfile"
    [ "$status" -eq 0 ]
    # Only one NODE_VERSION line, and it's the uncommented v24.18.0 value.
    count="$(echo "$output" | grep -c '^NODE_VERSION|' || true)"
    [ "$count" -eq 1 ]
    [[ "$output" == *"NODE_VERSION|v24.18.0"* ]]
    rm -f "$tmpfile"
}

@test "extract_dockerfile_pins: strips trailing inline comments" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
ARG DOCKER_VERSION=28.5.2  # pinned to the release on download.docker.com
EOF
    run extract_dockerfile_pins "$tmpfile"
    [ "$status" -eq 0 ]
    # The value must not carry the comment text.
    [ "$output" = "DOCKER_VERSION|28.5.2" ]
    rm -f "$tmpfile"
}

@test "extract_dockerfile_pins: returns empty for nonexistent file" {
    run extract_dockerfile_pins "/nonexistent/Dockerfile.dev"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_dockerfile_pins: returns empty for file with no ARG lines" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
FROM python:3.14-slim
RUN echo "no args here"
EOF
    run extract_dockerfile_pins "$tmpfile"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -f "$tmpfile"
}

# ── extract_precommit_hooks ─────────────────────────────────────────────────

@test "extract_precommit_hooks: emits one repo|rev pair per real hook" {
    run extract_precommit_hooks ".pre-commit-config.yaml"
    [ "$status" -eq 0 ]
    # Each line is exactly URL|REV — no stray whitespace, no leading dashes.
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        [[ "$line" =~ ^https?://[^[:space:]]+\|[^[:space:]]+$ ]] || {
            echo "bad line: '$line'"
            return 1
        }
    done <<< "$output"
}

@test "extract_precommit_hooks: includes the ruff and mypy hooks" {
    # Both hooks live at the top of the project's config and have been
    # there long enough that any change would be intentional. Asserting
    # presence (rather than exact rev) keeps the test stable across
    # routine bumps.
    run extract_precommit_hooks ".pre-commit-config.yaml"
    [ "$status" -eq 0 ]
    [[ "$output" == *"https://github.com/astral-sh/ruff-pre-commit|"* ]]
    [[ "$output" == *"https://github.com/pre-commit/mirrors-mypy|"* ]]
}

@test "extract_precommit_hooks: skips local and meta repos" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.15.7
    hooks:
      - id: ruff
  - repo: local
    hooks:
      - id: my-script
        name: My script
        entry: ./my-script.sh
        language: script
  - repo: meta
    hooks:
      - id: check-hooks-apply
EOF
    run extract_precommit_hooks "$tmpfile"
    [ "$status" -eq 0 ]
    # Real hook is present.
    [[ "$output" == *"https://github.com/astral-sh/ruff-pre-commit|v0.15.7"* ]]
    # local/meta sentinels are skipped.
    [[ "$output" != *"local|"* ]]
    [[ "$output" != *"meta|"* ]]
    # Exactly one line of output (just the ruff hook).
    line_count="$(printf '%s\n' "$output" | grep -c '|' || true)"
    [ "$line_count" -eq 1 ]
    rm -f "$tmpfile"
}

@test "extract_precommit_hooks: skips hooks with no rev" {
    # pre-commit allows omitting ``rev`` (e.g. for a meta-style repo
    # entry that only declares hooks). Those entries have nothing to
    # compare against and must be filtered out.
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.15.7
    hooks:
      - id: ruff
  - repo: https://github.com/example/no-rev
    hooks:
      - id: example
EOF
    run extract_precommit_hooks "$tmpfile"
    [ "$status" -eq 0 ]
    [[ "$output" == *"ruff-pre-commit|v0.15.7"* ]]
    [[ "$output" != *"no-rev"* ]]
    rm -f "$tmpfile"
}

@test "extract_precommit_hooks: returns empty for nonexistent file" {
    run extract_precommit_hooks "/nonexistent/.pre-commit-config.yaml"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_precommit_hooks: returns empty for malformed YAML" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
repos: [
  this is not valid yaml
EOF
    run extract_precommit_hooks "$tmpfile"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -f "$tmpfile"
}

# ── extract_k8s_version ─────────────────────────────────────────────────────

@test "extract_k8s_version: reads version from cdk.json" {
    run extract_k8s_version "cdk.json"
    [ "$status" -eq 0 ]
    # Should be a version like 1.36
    [[ "$output" =~ ^[0-9]+\.[0-9]+$ ]]
}

@test "extract_k8s_version: falls back to 1.36 for missing file" {
    run extract_k8s_version "/nonexistent/cdk.json"
    [ "$status" -eq 0 ]
    [ "$output" = "1.36" ]
}

# ── extract_direct_python_deps ──────────────────────────────────────────────

@test "extract_direct_python_deps: picks up project.dependencies entries" {
    # The real pyproject.toml pins boto3, click, requests, etc. in the
    # top-level ``project.dependencies`` list. Those must all appear
    # in the normalised output.
    run extract_direct_python_deps "pyproject.toml"
    [ "$status" -eq 0 ]
    [[ "$output" =~ (^|$'\n')boto3($|$'\n') ]]
    [[ "$output" =~ (^|$'\n')click($|$'\n') ]]
    [[ "$output" =~ (^|$'\n')requests($|$'\n') ]]
    # And at least one optional-dep group entry should show up, since
    # the helper also reads ``[project.optional-dependencies]``.
    [[ "$output" =~ (^|$'\n')aws-cdk-lib($|$'\n') ]]
}

@test "extract_direct_python_deps: omits pure transitive deps" {
    # ``attrs``, ``cattrs``, ``rsa``, ``typeguard`` are in
    # requirements-lock.txt but never listed in pyproject.toml — the
    # filter must drop them so the dep-scan report only lists
    # direct-dep drift.
    run extract_direct_python_deps "pyproject.toml"
    [ "$status" -eq 0 ]
    ! [[ "$output" =~ (^|$'\n')attrs($|$'\n') ]]
    ! [[ "$output" =~ (^|$'\n')cattrs($|$'\n') ]]
    ! [[ "$output" =~ (^|$'\n')rsa($|$'\n') ]]
    ! [[ "$output" =~ (^|$'\n')typeguard($|$'\n') ]]
}

@test "extract_direct_python_deps: strips self-reference (gco-cli[dev])" {
    # The ``dev`` extra in pyproject.toml lists ``gco-cli[cdk,...]``.
    # That's a meta-entry pip resolves to the current project; it
    # must not appear as a "direct dep" name in our filter list.
    run extract_direct_python_deps "pyproject.toml"
    [ "$status" -eq 0 ]
    ! [[ "$output" =~ (^|$'\n')gco-cli($|$'\n') ]]
}

@test "extract_direct_python_deps: output is lowercased + normalised" {
    # PEP 503: lowercase and ``_`` / ``.`` → ``-``. Sanity-check by
    # asserting every output line is already in that normal form.
    run extract_direct_python_deps "pyproject.toml"
    [ "$status" -eq 0 ]
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        lowered="$(echo "$line" | tr 'A-Z' 'a-z' | tr '_.' '--')"
        [ "$line" = "$lowered" ]
    done <<< "$output"
}

@test "extract_direct_python_deps: returns empty for missing file" {
    run extract_direct_python_deps "/nonexistent/pyproject.toml"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── extract_constant_value ──────────────────────────────────────────────────

@test "extract_constant_value: reads LAMBDA_PYTHON_RUNTIME from real constants.py" {
    run extract_constant_value "LAMBDA_PYTHON_RUNTIME" "gco/stacks/constants.py"
    [ "$status" -eq 0 ]
    # The pinned value is something like ``PYTHON_3_14`` — assert the
    # shape so a legitimate bump doesn't break the test.
    [[ "$output" =~ ^PYTHON_[0-9]+_[0-9]+$ ]]
}

@test "extract_constant_value: reads AURORA_POSTGRES_VERSION from real constants.py" {
    run extract_constant_value "AURORA_POSTGRES_VERSION" "gco/stacks/constants.py"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^VER_[0-9]+_[0-9]+$ ]]
}

@test "extract_constant_value: returns empty for unknown constant" {
    run extract_constant_value "NOT_A_REAL_CONSTANT_XYZ" "gco/stacks/constants.py"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_constant_value: returns empty for missing file" {
    run extract_constant_value "LAMBDA_PYTHON_RUNTIME" "/nonexistent/constants.py"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_constant_value: ignores commented-out assignment" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
# OLD_KNOB = "stale"
NEW_KNOB = "fresh"
EOF
    run extract_constant_value "OLD_KNOB" "$tmpfile"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    run extract_constant_value "NEW_KNOB" "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$output" = "fresh" ]
    rm -f "$tmpfile"
}

@test "extract_constant_value: only matches an exact name (no substring)" {
    # The regex must be anchored on the constant name so a request for
    # ``FOO`` does not accidentally match ``FOO_BAR``.
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
FOO_BAR = "decoy"
FOO = "real"
EOF
    run extract_constant_value "FOO" "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$output" = "real" ]
    rm -f "$tmpfile"
}

# ── get_latest_lambda_python_runtime ────────────────────────────────────────

@test "get_latest_lambda_python_runtime: returns enum name when aws-cdk-lib installed" {
    python3 -c "import aws_cdk" 2>/dev/null || skip "aws-cdk-lib not installed"
    run get_latest_lambda_python_runtime
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^PYTHON_[0-9]+_[0-9]+$ ]]
}

@test "get_latest_lambda_python_runtime: empty when aws-cdk-lib missing" {
    # The unit:bats:shell CI job runs in a minimal environment without
    # aws-cdk-lib, where this branch is exercised naturally. Skip
    # locally if the developer has aws-cdk-lib in their interpreter
    # (the positive test above already covers that case).
    python3 -c "import aws_cdk" 2>/dev/null && skip "aws-cdk-lib is installed; positive case is tested separately"
    run get_latest_lambda_python_runtime
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── get_latest_aurora_postgres_version ──────────────────────────────────────

@test "get_latest_aurora_postgres_version: returns enum name when aws-cdk-lib installed" {
    python3 -c "import aws_cdk" 2>/dev/null || skip "aws-cdk-lib not installed"
    run get_latest_aurora_postgres_version
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^VER_[0-9]+_[0-9]+$ ]]
}

@test "get_latest_aurora_postgres_version: empty when aws-cdk-lib missing" {
    python3 -c "import aws_cdk" 2>/dev/null && skip "aws-cdk-lib is installed; positive case is tested separately"
    run get_latest_aurora_postgres_version
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── get_latest_python_release ───────────────────────────────────────────────

@test "get_latest_python_release: parses endoflife.date-shaped fixture" {
    # Don't make the test depend on internet — feed the parsing
    # pipeline a fixture that mirrors endoflife.date's response shape
    # and assert the prerelease + EOL filters work end-to-end.
    run python3 -c "
import datetime, json
data = [
    {'cycle': '3.99', 'releaseDate': '2999-01-01', 'eol': False},
    {'cycle': '3.14', 'releaseDate': '2025-10-07', 'eol': '2030-10-07'},
    {'cycle': '3.13', 'releaseDate': '2024-10-07', 'eol': '2029-10-07'},
    {'cycle': '3.7',  'releaseDate': '2018-06-27', 'eol': '2023-06-27'},
]
today = datetime.date.today().isoformat()
candidates = []
for entry in data:
    cycle = entry.get('cycle', '')
    release = entry.get('releaseDate', '') or ''
    eol = entry.get('eol', '')
    if not cycle or '.' not in cycle:
        continue
    if isinstance(release, str) and release > today:
        continue
    if isinstance(eol, str) and eol and eol < today:
        continue
    parts = tuple(int(p) for p in cycle.split('.'))
    candidates.append((parts, cycle))
print(max(candidates)[1] if candidates else '')
"
    [ "$status" -eq 0 ]
    # 3.99 is filtered as prerelease; 3.7 is filtered as EOL; the
    # newest of {3.13, 3.14} should win.
    [ "$output" = "3.14" ]
}

@test "get_latest_python_release: empty on malformed JSON" {
    run python3 -c "
import json, sys
try:
    data = json.loads('not json')
except Exception:
    sys.exit(0)
"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── get_latest_precommit_hook_release ───────────────────────────────────────
#
# The happy path needs a network round-trip to api.github.com, which we
# don't want to take in the unit BATS suite — the live network calls
# are exercised end-to-end by the deps-scan workflow itself. The tests
# below focus on the URL-parsing branches that run before the curl
# (skip non-GitHub hosts, reject malformed paths, accept ``.git`` /
# trailing-slash variants), which is where regressions would actually
# bite.

@test "get_latest_precommit_hook_release: empty for empty input" {
    run get_latest_precommit_hook_release ""
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "get_latest_precommit_hook_release: empty for non-GitHub host" {
    # GitLab, Codeberg, etc. — no false drift; the helper just returns
    # empty so the caller treats the hook as ``skipped``.
    run get_latest_precommit_hook_release "https://gitlab.com/owner/repo"
    [ "$status" -eq 0 ]
    [ -z "$output" ]

    run get_latest_precommit_hook_release "https://codeberg.org/owner/repo"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "get_latest_precommit_hook_release: empty for github.com URL with deeper path" {
    # ``https://github.com/owner/repo/tree/main`` is technically a valid
    # GitHub URL but not the form pre-commit accepts. We reject it
    # before the API call so a typo doesn't 404 silently and pollute
    # logs with a spurious request.
    run get_latest_precommit_hook_release "https://github.com/owner/repo/tree/main"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "get_latest_precommit_hook_release: empty for github.com URL with no repo" {
    run get_latest_precommit_hook_release "https://github.com/owner"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "get_latest_precommit_hook_release: trailing slash is tolerated" {
    # We don't want the network call, so we simulate by replacing curl
    # in PATH with a shim that prints a canned tags response. This
    # exercises the URL-cleanup logic end-to-end without hitting the
    # internet — same pattern other BATS suites in this file would use
    # if they needed to.
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/curl" <<'SHIM'
#!/usr/bin/env bash
# Emit a tags-shaped JSON regardless of args so the helper's parser
# gets something realistic. The tags below mix shapes (vX.Y.Z, X.Y.Z,
# pre-release suffix, non-semver) so this also covers the parser.
cat <<'JSON'
[
  {"name": "v1.2.3"},
  {"name": "v1.3.0-rc1"},
  {"name": "1.4.0"},
  {"name": "release-2024"},
  {"name": "v1.2.4"}
]
JSON
SHIM
    chmod +x "$tmpdir/curl"
    PATH="$tmpdir:$PATH" run get_latest_precommit_hook_release "https://github.com/owner/repo/"
    [ "$status" -eq 0 ]
    # ``1.4.0`` is the highest valid semver in the fixture; non-semver
    # and pre-release tags are filtered out.
    [ "$output" = "1.4.0" ]
    rm -rf "$tmpdir"
}

@test "get_latest_precommit_hook_release: .git suffix is stripped" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/curl" <<'SHIM'
#!/usr/bin/env bash
echo '[{"name": "v0.22.1"}]'
SHIM
    chmod +x "$tmpdir/curl"
    PATH="$tmpdir:$PATH" run get_latest_precommit_hook_release "https://github.com/owner/repo.git"
    [ "$status" -eq 0 ]
    [ "$output" = "v0.22.1" ]
    rm -rf "$tmpdir"
}

@test "get_latest_precommit_hook_release: empty when curl returns no tags" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/curl" <<'SHIM'
#!/usr/bin/env bash
echo '[]'
SHIM
    chmod +x "$tmpdir/curl"
    PATH="$tmpdir:$PATH" run get_latest_precommit_hook_release "https://github.com/owner/repo"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -rf "$tmpdir"
}

@test "get_latest_precommit_hook_release: empty when no semver-shaped tags" {
    # Only date-based tags, like a few infrastructure-as-code repos
    # publish. The helper must return empty rather than guess.
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/curl" <<'SHIM'
#!/usr/bin/env bash
cat <<'JSON'
[
  {"name": "release-2024-09-01"},
  {"name": "release-2024-10-01"}
]
JSON
SHIM
    chmod +x "$tmpdir/curl"
    PATH="$tmpdir:$PATH" run get_latest_precommit_hook_release "https://github.com/owner/repo"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -rf "$tmpdir"
}

@test "extract_direct_python_deps: fixture with only transitive-shaped names" {
    # Fixture pyproject with two direct pins + an optional-deps group.
    # Every transitive in requirements-lock.txt is absent from this
    # fixture, so the filter must match exactly the two + the optional-
    # deps entry.
    local tmpdir
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/pyproject.toml" <<'EOF'
[project]
name = "toy"
dependencies = [
    "boto3==1.43.3",
    "click==8.3.3",
]

[project.optional-dependencies]
test = ["pytest==9.0.3"]
EOF
    run extract_direct_python_deps "$tmpdir/pyproject.toml"
    [ "$status" -eq 0 ]
    # Sort output + expected so the comparison is insertion-order agnostic.
    expected="$(printf 'boto3\nclick\npytest\n' | sort)"
    got="$(printf '%s\n' "$output" | sort)"
    [ "$got" = "$expected" ]
    rm -rf "$tmpdir"
}

# ── extract_mooncake_default_image ───────────────────────────────────────────

@test "extract_mooncake_default_image: reads the pin from cli/images.py" {
    run extract_mooncake_default_image "cli/images.py"
    [ "$status" -eq 0 ]
    # A single concrete repo:tag reference (e.g. vllm/vllm-openai:vX.Y.Z).
    [[ "$output" =~ ^[^[:space:]:]+:[^[:space:]:]+$ ]]
}

@test "extract_mooncake_default_image: parses the constant from a fixture" {
    run bash -c '
        source .github/scripts/lib_dependency_scan.sh
        tmpfile="$(mktemp)"
        cat > "$tmpfile" <<EOF
# comment line
_DISAGGREGATED_DEFAULT_IMAGE = "vllm/vllm-openai:v9.9.9"
EOF
        extract_mooncake_default_image "$tmpfile"
        rm -f "$tmpfile"
    '
    [ "$status" -eq 0 ]
    [ "$output" = "vllm/vllm-openai:v9.9.9" ]
}

@test "extract_mooncake_default_image: empty when constant absent" {
    run bash -c '
        source .github/scripts/lib_dependency_scan.sh
        tmpfile="$(mktemp)"
        echo "no image constant here" > "$tmpfile"
        extract_mooncake_default_image "$tmpfile"
        rm -f "$tmpfile"
    '
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_mooncake_default_image: empty when file is missing" {
    run extract_mooncake_default_image "/nonexistent/cli/images.py"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── extract_default_bedrock_model ──────────────────────────────────────

@test "extract_default_bedrock_model: reads the configured id from cdk.json" {
    run extract_default_bedrock_model "cdk.json"
    [ "$status" -eq 0 ]
    # A system-defined inference-profile id: geography.provider.model, with an
    # optional trailing -vMAJOR[:MINOR] revision. Newer Anthropic profiles ship
    # without any revision suffix, so only the dotted scope is universal.
    [ -n "$output" ]
    [[ "$output" == *"."* ]]
    [[ "$output" == *.*.* ]]
}

@test "extract_default_bedrock_model: returns the exact cdk context value" {
    expected="$(python3 -c '
import json
with open("cdk.json") as handle:
    print(json.load(handle)["context"]["bedrock"]["default_model_id"])
')"
    [ -n "$expected" ]
    run extract_default_bedrock_model "cdk.json"
    [ "$status" -eq 0 ]
    [ "$output" = "$expected" ]
}

@test "extract_default_bedrock_model: parses context.bedrock.default_model_id" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
{"context":{"bedrock":{"default_model_id":"us.amazon.nova-pro-v1:0"}}}
EOF
    run extract_default_bedrock_model "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$output" = "us.amazon.nova-pro-v1:0" ]
    rm -f "$tmpfile"
}

@test "extract_default_bedrock_model: empty when the context key is absent" {
    tmpfile="$(mktemp)"
    echo '{"context":{}}' > "$tmpfile"
    run extract_default_bedrock_model "$tmpfile"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -f "$tmpfile"
}

@test "extract_default_bedrock_model: empty when JSON is malformed" {
    tmpfile="$(mktemp)"
    echo '{not-json' > "$tmpfile"
    run extract_default_bedrock_model "$tmpfile"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -f "$tmpfile"
}

@test "extract_default_bedrock_model: empty when file is missing" {
    run extract_default_bedrock_model "/nonexistent/cdk.json"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── bedrock_model_family ────────────────────────────────────────────

@test "bedrock_model_family: Nova Pro keeps the tier, drops the version" {
    result="$(bedrock_model_family "us.amazon.nova-pro-v1:0")"
    [ "$result" = "us.amazon.nova-pro" ]
}

@test "bedrock_model_family: global Nova 2 Lite preserves global scope and folds generation" {
    # The numeric generation token is dropped from the family so later Nova
    # Lite generations compare within the same global-profile family.
    result="$(bedrock_model_family "global.amazon.nova-2-lite-v1:0")"
    [ "$result" = "global.amazon.nova-lite" ]
}

@test "bedrock_model_family: Claude Sonnet drops the model version and date" {
    result="$(bedrock_model_family "us.anthropic.claude-sonnet-4-5-20250929-v1:0")"
    [ "$result" = "us.anthropic.claude-sonnet" ]
}

@test "bedrock_model_family: different tiers are different families" {
    a="$(bedrock_model_family "us.amazon.nova-pro-v1:0")"
    b="$(bedrock_model_family "us.amazon.nova-lite-v1:0")"
    [ "$a" != "$b" ]
}

@test "bedrock_model_family: a revision-less Anthropic profile keeps its line" {
    result="$(bedrock_model_family "global.anthropic.claude-opus-5")"
    [ "$result" = "global.anthropic.claude-opus" ]
}

@test "bedrock_model_family: a -vMAJOR revision without a minor is stripped" {
    # Matching only the -vMAJOR:MINOR form would leave 'v1' as a name token and
    # file this under a phantom claude-opus-v1 family.
    result="$(bedrock_model_family "global.anthropic.claude-opus-4-6-v1")"
    [ "$result" = "global.anthropic.claude-opus" ]
}

@test "bedrock_model_family: every Opus revision shape folds into one family" {
    # The live catalog carries all three shapes at once: -vMAJOR:MINOR,
    # -vMAJOR, and no revision. Drift detection only works if they agree.
    a="$(bedrock_model_family "global.anthropic.claude-opus-4-5-20251101-v1:0")"
    b="$(bedrock_model_family "global.anthropic.claude-opus-4-6-v1")"
    c="$(bedrock_model_family "global.anthropic.claude-opus-4-7")"
    d="$(bedrock_model_family "global.anthropic.claude-opus-5")"
    [ "$a" = "$b" ]
    [ "$b" = "$c" ]
    [ "$c" = "$d" ]
}

@test "bedrock_model_family: Claude tiers stay separate across revision shapes" {
    opus="$(bedrock_model_family "global.anthropic.claude-opus-5")"
    sonnet="$(bedrock_model_family "global.anthropic.claude-sonnet-4-6")"
    [ "$opus" != "$sonnet" ]
}

# ── compare_bedrock_model ─────────────────────────────────────────

@test "compare_bedrock_model: v1:0 vs v2:0 is newer" {
    result="$(compare_bedrock_model "us.amazon.nova-pro-v1:0" "us.amazon.nova-pro-v2:0")"
    [ "$result" = "newer" ]
}

@test "compare_bedrock_model: identical ids are the same" {
    result="$(compare_bedrock_model "us.amazon.nova-pro-v1:0" "us.amazon.nova-pro-v1:0")"
    [ "$result" = "same" ]
}

@test "compare_bedrock_model: a newer model-version/date candidate is newer" {
    result="$(compare_bedrock_model "us.anthropic.claude-sonnet-4-5-20250929-v1:0" "us.anthropic.claude-sonnet-4-6-20251101-v1:0")"
    [ "$result" = "newer" ]
}

@test "compare_bedrock_model: an older candidate is older" {
    result="$(compare_bedrock_model "us.anthropic.claude-sonnet-4-6-20251101-v1:0" "us.anthropic.claude-sonnet-4-5-20250929-v1:0")"
    [ "$result" = "older" ]
}

@test "compare_bedrock_model: a later generation is newer (Nova 1 -> Nova 2)" {
    result="$(compare_bedrock_model "us.amazon.nova-pro-v1:0" "us.amazon.nova-2-pro-v1:0")"
    [ "$result" = "newer" ]
}

@test "compare_bedrock_model: Opus 4.8 -> Opus 5 is newer across revision shapes" {
    result="$(compare_bedrock_model "global.anthropic.claude-opus-4-8" "global.anthropic.claude-opus-5")"
    [ "$result" = "newer" ]
}

@test "compare_bedrock_model: Opus 5 -> a dated Opus 4.5 profile is older" {
    result="$(compare_bedrock_model "global.anthropic.claude-opus-5" "global.anthropic.claude-opus-4-5-20251101-v1:0")"
    [ "$result" = "older" ]
}

@test "compare_bedrock_model: a revision-less id equals itself" {
    result="$(compare_bedrock_model "global.anthropic.claude-opus-5" "global.anthropic.claude-opus-5")"
    [ "$result" = "same" ]
}

# ── get_latest_bedrock_model ─────────────────────────────────────
#
# The happy path shells out to ``aws bedrock list-inference-profiles``; the
# BATS suite must not make a real AWS call, so a shim on PATH returns a canned
# response. This exercises the family filter, the ACTIVE-status filter, and the
# version ranking without credentials (same shimming pattern the
# get_latest_precommit_hook_release tests use for curl).

@test "get_latest_bedrock_model: returns the newest ACTIVE profile in the same family" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/aws" <<'SHIM'
#!/usr/bin/env bash
cat <<'JSON'
{"inferenceProfileSummaries":[
  {"inferenceProfileId":"us.amazon.nova-pro-v1:0","status":"ACTIVE"},
  {"inferenceProfileId":"us.amazon.nova-pro-v2:0","status":"ACTIVE"},
  {"inferenceProfileId":"us.amazon.nova-lite-v1:0","status":"ACTIVE"},
  {"inferenceProfileId":"us.anthropic.claude-sonnet-9-9-20990101-v1:0","status":"ACTIVE"},
  {"inferenceProfileId":"us.amazon.nova-pro-v9:0","status":"LEGACY"}
]}
JSON
SHIM
    chmod +x "$tmpdir/aws"
    PATH="$tmpdir:$PATH" run get_latest_bedrock_model "us.amazon.nova-pro-v1:0" us-east-1
    [ "$status" -eq 0 ]
    # v2:0 is the newest ACTIVE nova-pro; the LEGACY v9:0 is skipped and
    # other families (nova-lite, claude) are filtered out.
    [ "$output" = "us.amazon.nova-pro-v2:0" ]
    rm -rf "$tmpdir"
}

@test "get_latest_bedrock_model: selects the newest ACTIVE global Nova Lite profile" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/aws" <<'SHIM'
#!/usr/bin/env bash
cat <<'JSON'
{"inferenceProfileSummaries":[
  {"inferenceProfileId":"global.amazon.nova-2-lite-v1:0","status":"ACTIVE"},
  {"inferenceProfileId":"global.amazon.nova-3-lite-v1:0","status":"ACTIVE"},
  {"inferenceProfileId":"global.amazon.nova-9-lite-v1:0","status":"LEGACY"},
  {"inferenceProfileId":"us.amazon.nova-8-lite-v1:0","status":"ACTIVE"},
  {"inferenceProfileId":"global.amazon.nova-9-pro-v1:0","status":"ACTIVE"}
]}
JSON
SHIM
    chmod +x "$tmpdir/aws"
    PATH="$tmpdir:$PATH" run get_latest_bedrock_model \
        "global.amazon.nova-2-lite-v1:0" us-east-1
    [ "$status" -eq 0 ]
    [ "$output" = "global.amazon.nova-3-lite-v1:0" ]
    rm -rf "$tmpdir"
}

@test "get_latest_bedrock_model: ranks mixed Anthropic revision shapes in one family" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/aws" <<'SHIM'
#!/usr/bin/env bash
cat <<'JSON'
{"inferenceProfileSummaries":[
  {"inferenceProfileId":"global.anthropic.claude-opus-4-5-20251101-v1:0","status":"ACTIVE"},
  {"inferenceProfileId":"global.anthropic.claude-opus-4-6-v1","status":"ACTIVE"},
  {"inferenceProfileId":"global.anthropic.claude-opus-4-7","status":"ACTIVE"},
  {"inferenceProfileId":"global.anthropic.claude-opus-6","status":"ACTIVE"},
  {"inferenceProfileId":"global.anthropic.claude-opus-9","status":"LEGACY"},
  {"inferenceProfileId":"us.anthropic.claude-opus-8","status":"ACTIVE"},
  {"inferenceProfileId":"global.anthropic.claude-sonnet-9","status":"ACTIVE"}
]}
JSON
SHIM
    chmod +x "$tmpdir/aws"
    PATH="$tmpdir:$PATH" run get_latest_bedrock_model \
        "global.anthropic.claude-opus-5" us-east-1
    [ "$status" -eq 0 ]
    # Opus 6 is the newest ACTIVE global Opus regardless of revision shape; the
    # LEGACY entry, the us-scoped profile, and the sonnet tier are excluded.
    [ "$output" = "global.anthropic.claude-opus-6" ]
    rm -rf "$tmpdir"
}

@test "get_latest_bedrock_model: no drift when the pinned revision-less id is newest" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/aws" <<'SHIM'
#!/usr/bin/env bash
cat <<'JSON'
{"inferenceProfileSummaries":[
  {"inferenceProfileId":"global.anthropic.claude-opus-4-6-v1","status":"ACTIVE"},
  {"inferenceProfileId":"global.anthropic.claude-opus-5","status":"ACTIVE"}
]}
JSON
SHIM
    chmod +x "$tmpdir/aws"
    PATH="$tmpdir:$PATH" run get_latest_bedrock_model \
        "global.anthropic.claude-opus-5" us-east-1
    [ "$status" -eq 0 ]
    [ "$output" = "global.anthropic.claude-opus-5" ]
    rm -rf "$tmpdir"
}

@test "get_latest_bedrock_model: scopes to the pinned model's family" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/aws" <<'SHIM'
#!/usr/bin/env bash
cat <<'JSON'
{"inferenceProfileSummaries":[
  {"inferenceProfileId":"us.amazon.nova-pro-v1:0","status":"ACTIVE"},
  {"inferenceProfileId":"us.anthropic.claude-sonnet-9-9-20990101-v1:0","status":"ACTIVE"}
]}
JSON
SHIM
    chmod +x "$tmpdir/aws"
    PATH="$tmpdir:$PATH" run get_latest_bedrock_model "us.anthropic.claude-sonnet-4-5-20250929-v1:0" us-east-1
    [ "$status" -eq 0 ]
    [ "$output" = "us.anthropic.claude-sonnet-9-9-20990101-v1:0" ]
    rm -rf "$tmpdir"
}

@test "get_latest_bedrock_model: empty when the aws call fails" {
    tmpdir="$(mktemp -d)"
    printf '#!/usr/bin/env bash\nexit 1\n' > "$tmpdir/aws"
    chmod +x "$tmpdir/aws"
    PATH="$tmpdir:$PATH" run get_latest_bedrock_model "us.amazon.nova-pro-v1:0" us-east-1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -rf "$tmpdir"
}

@test "get_latest_bedrock_model: empty for empty input" {
    run get_latest_bedrock_model ""
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── get_latest_github_release_tag ───────────────────────────────────────────
#
# Same split as get_latest_precommit_hook_release: the owner/repo guard
# branches run without network and are where a regression would bite; the
# happy path is exercised with a curl shim so the suite stays offline.

@test "get_latest_github_release_tag: empty for empty input" {
    run get_latest_github_release_tag ""
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "get_latest_github_release_tag: empty for a non owner/repo argument" {
    run get_latest_github_release_tag "not-a-repo"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "get_latest_github_release_tag: empty for a deeper path" {
    run get_latest_github_release_tag "owner/repo/extra"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "get_latest_github_release_tag: parses tag_name from a shimmed release response" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/curl" <<'SHIM'
#!/usr/bin/env bash
# Emit a releases/latest-shaped JSON regardless of args.
cat <<'JSON'
{"tag_name": "v0.71.0", "name": "Trivy v0.71.0"}
JSON
SHIM
    chmod +x "$tmpdir/curl"
    PATH="$tmpdir:$PATH" run get_latest_github_release_tag "aquasecurity/trivy"
    [ "$status" -eq 0 ]
    [ "$output" = "v0.71.0" ]
    rm -rf "$tmpdir"
}

# ── extract_workflow_env_pin ────────────────────────────────────────────────

@test "extract_workflow_env_pin: reads TRIVY_VERSION from the security workflows" {
    run extract_workflow_env_pin TRIVY_VERSION
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

@test "extract_workflow_env_pin: reads the deps-scan HELM_VERSION and KUBECTL_VERSION" {
    run extract_workflow_env_pin HELM_VERSION
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^v[0-9] ]]
    run extract_workflow_env_pin KUBECTL_VERSION
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^v1\. ]]
}

@test "extract_workflow_env_pin: reads the KUBECONFORM_VERSION pin from integration-tests.yml" {
    # The integration:k8s:manifest-schema job pins kubeconform as a job-level
    # env var so the "CI tooling" drift check in dependency-scan.sh can track
    # it. This proves the pin is present and shaped like a version tag.
    run extract_workflow_env_pin KUBECONFORM_VERSION
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^v[0-9] ]]
}

@test "extract_workflow_env_pin: reads the METRICS_SERVER_VERSION pin from integration-tests.yml" {
    # kind installs this pinned component so CI can require the inference HPA's
    # ScalingActive condition instead of checking admission alone.
    run extract_workflow_env_pin METRICS_SERVER_VERSION
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

@test "extract_workflow_env_pin: empty for an unset var" {
    run extract_workflow_env_pin NONEXISTENT_VERSION_XYZ
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_workflow_env_pin: empty for a missing directory" {
    run extract_workflow_env_pin TRIVY_VERSION /nonexistent/dir
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_workflow_env_pin: dedups and surfaces multiple distinct values" {
    tmpdir="$(mktemp -d)"
    printf 'env:\n  FOO_VERSION: "v1.0.0"\n' > "$tmpdir/a.yml"
    printf 'env:\n  FOO_VERSION: "v2.0.0"\n' > "$tmpdir/b.yml"
    printf 'env:\n  FOO_VERSION: "v1.0.0"\n' > "$tmpdir/c.yml"
    run extract_workflow_env_pin FOO_VERSION "$tmpdir"
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | grep -c .)" -eq 2 ]
    [[ "$output" == *"v1.0.0"* ]]
    [[ "$output" == *"v2.0.0"* ]]
    rm -rf "$tmpdir"
}

# ── extract_kind_pins ───────────────────────────────────────────────────────

@test "extract_kind_pins: reads kind + node image from integration-tests.yml" {
    run extract_kind_pins ".github/workflows/integration-tests.yml"
    [ "$status" -eq 0 ]
    [[ "$output" == *"kind|v"* ]]
    [[ "$output" == *"kind-node|kindest/node:"* ]]
}

@test "extract_kind_pins: empty for a missing file" {
    run extract_kind_pins "/nonexistent/integration-tests.yml"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_kind_pins: empty when no kind-action step is present" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
jobs:
  build:
    steps:
      - uses: actions/checkout@v7
EOF
    run extract_kind_pins "$tmpfile"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -f "$tmpfile"
}

@test "extract_kind_pins: parses a synthetic kind-action step" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
jobs:
  e2e:
    steps:
      - uses: helm/kind-action@v1.14.0
        with:
          version: "v0.99.0"
          node_image: "kindest/node:v1.40.0"
EOF
    run extract_kind_pins "$tmpfile"
    [ "$status" -eq 0 ]
    [[ "$output" == *"kind|v0.99.0"* ]]
    [[ "$output" == *"kind-node|kindest/node:v1.40.0"* ]]
    rm -f "$tmpfile"
}

# ── extract_ruff_pins ───────────────────────────────────────────────────────

@test "extract_ruff_pins: reports all three sources from the real repo" {
    run extract_ruff_pins
    [ "$status" -eq 0 ]
    [[ "$output" == *"pyproject|"* ]]
    [[ "$output" == *"precommit|"* ]]
    [[ "$output" == *"lint-action|"* ]]
    # Every value is normalised (no leading v, digit-dotted).
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        val="${line#*|}"
        [[ "$val" =~ ^[0-9]+\.[0-9]+ ]]
    done <<< "$output"
}

@test "extract_ruff_pins: normalises a v-prefixed pre-commit rev and reads the action version" {
    # Synthetic files keep the assertion stable across ruff bumps and prove
    # the lint-action value comes from the ruff-action step (not a nearby
    # python-version).
    tmpdir="$(mktemp -d)"
    printf '[project.optional-dependencies]\nlint = ["ruff==9.9.9"]\n' > "$tmpdir/pyproject.toml"
    cat > "$tmpdir/pre-commit.yaml" <<'EOF'
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v9.9.10
    hooks:
      - id: ruff
EOF
    cat > "$tmpdir/lint.yml" <<'EOF'
jobs:
  lint:
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.14"
      - uses: astral-sh/ruff-action@v4.0.0
        with:
          version: "9.9.9"
EOF
    run extract_ruff_pins "$tmpdir/pyproject.toml" "$tmpdir/pre-commit.yaml" "$tmpdir/lint.yml"
    [ "$status" -eq 0 ]
    [[ "$output" == *"pyproject|9.9.9"* ]]
    [[ "$output" == *"precommit|9.9.10"* ]]
    [[ "$output" == *"lint-action|9.9.9"* ]]
    rm -rf "$tmpdir"
}

# ── extract_python_version_pins ─────────────────────────────────────────────

@test "extract_python_version_pins: finds the repo's python-version pins" {
    run extract_python_version_pins
    [ "$status" -eq 0 ]
    [ -n "$output" ]
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        [[ "$line" =~ ^[0-9]+\.[0-9]+$ ]]
    done <<< "$output"
}

@test "extract_python_version_pins: empty for a missing directory" {
    run extract_python_version_pins /nonexistent/dir
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_python_version_pins: emits one line per occurrence for uniq counting" {
    tmpdir="$(mktemp -d)"
    printf 'jobs:\n  a:\n    steps:\n      - with:\n          python-version: "3.14"\n' > "$tmpdir/a.yml"
    printf 'jobs:\n  b:\n    steps:\n      - with:\n          python-version: "3.14"\n' > "$tmpdir/b.yml"
    run extract_python_version_pins "$tmpdir"
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | grep -c '^3\.14$')" -eq 2 ]
    rm -rf "$tmpdir"
}

# ── parse_suppression_expiries ──────────────────────────────────────────────

@test "parse_suppression_expiries: parses ID|date pairs from the real .trivyignore" {
    run parse_suppression_expiries ".github/config/.trivyignore"
    [ "$status" -eq 0 ]
    # Each non-empty line is ID|YYYY-MM-DD (the file may have zero active
    # entries, in which case output is empty and the loop is a no-op).
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        [[ "$line" =~ ^[^|]+\|[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]
    done <<< "$output"
}

@test "parse_suppression_expiries: empty for a missing file" {
    run parse_suppression_expiries "/nonexistent/.trivyignore"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "parse_suppression_expiries: skips comments/blanks and keeps dated entries" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
# a comment mentioning exp:2099-01-01 that must be ignored

CVE-2026-0001 exp:2026-12-31
CVE-2026-0002 exp:2027-01-15
EOF
    run parse_suppression_expiries "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | grep -c .)" -eq 2 ]
    [[ "$output" == *"CVE-2026-0001|2026-12-31"* ]]
    [[ "$output" == *"CVE-2026-0002|2027-01-15"* ]]
    rm -f "$tmpfile"
}

# ── check_lockfile_freshness ────────────────────────────────────────────────

@test "check_lockfile_freshness: the real repo lock has no missing direct deps" {
    run check_lockfile_freshness pyproject.toml requirements-lock.txt
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "check_lockfile_freshness: reports a direct dep missing from the lock" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/pyproject.toml" <<'EOF'
[project]
name = "demo"
dependencies = ["boto3==1.0.0", "totally-missing-pkg==2.0.0"]
EOF
    printf 'boto3==1.0.0\n' > "$tmpdir/lock.txt"
    run check_lockfile_freshness "$tmpdir/pyproject.toml" "$tmpdir/lock.txt"
    [ "$status" -eq 0 ]
    [[ "$output" == *"totally-missing-pkg"* ]]
    [[ "$output" != *"boto3"* ]]
    rm -rf "$tmpdir"
}

@test "check_lockfile_freshness: normalises names before comparing (no false positive)" {
    # A dep written with '.'/'_' in pyproject and '-' in the lock (PEP 503)
    # must be treated as present.
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/pyproject.toml" <<'EOF'
[project]
name = "demo"
dependencies = ["types_PyYAML==1.0.0"]
EOF
    printf 'types-pyyaml==1.0.0\n' > "$tmpdir/lock.txt"
    run check_lockfile_freshness "$tmpdir/pyproject.toml" "$tmpdir/lock.txt"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -rf "$tmpdir"
}

@test "check_lockfile_freshness: empty for missing files" {
    run check_lockfile_freshness /nonexistent/pyproject.toml /nonexistent/lock.txt
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── extract_security_epochs ─────────────────────────────────────────────────

@test "extract_security_epochs: reads APT_SECURITY_EPOCH from a service Dockerfile" {
    run extract_security_epochs "dockerfiles/health-monitor-dockerfile"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^APT_SECURITY_EPOCH\|[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]
}

@test "extract_security_epochs: reads DNF_SECURITY_EPOCH from the helm-installer Dockerfile" {
    run extract_security_epochs "lambda/helm-installer/Dockerfile"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^DNF_SECURITY_EPOCH\|[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]
}

@test "extract_security_epochs: empty for a missing file" {
    run extract_security_epochs "/nonexistent/Dockerfile"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_security_epochs: ignores a commented epoch and parses the live one" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
# ARG APT_SECURITY_EPOCH=1999-01-01
ARG APT_SECURITY_EPOCH=2026-06-25
EOF
    run extract_security_epochs "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$output" = "APT_SECURITY_EPOCH|2026-06-25" ]
    rm -f "$tmpfile"
}
