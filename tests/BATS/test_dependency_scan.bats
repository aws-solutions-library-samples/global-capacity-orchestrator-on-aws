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

@test "parse_image_registry: fully-qualified docker.io keeps a single registry prefix" {
    # Regression: this used to parse as repo "docker.io/library/busybox"
    # under a second docker.io, failing the tag lookup every month.
    result="$(parse_image_registry "docker.io/library/busybox")"
    [ "$result" = "docker.io|library/busybox" ]
}

@test "parse_image_registry: docker.io org repo is not given library/" {
    result="$(parse_image_registry "docker.io/nvidia/cuda")"
    [ "$result" = "docker.io|nvidia/cuda" ]
}

@test "parse_image_registry: docker.io single-segment repo gets library/ restored" {
    result="$(parse_image_registry "docker.io/python")"
    [ "$result" = "docker.io|library/python" ]
}

@test "parse_image_registry: unlisted dotted registry is honored without a code change" {
    result="$(parse_image_registry "my.registry.example/team/app")"
    [ "$result" = "my.registry.example|team/app" ]
}

@test "parse_image_registry: registry with a port is treated as a registry" {
    result="$(parse_image_registry "registry.example:5000/team/app")"
    [ "$result" = "registry.example:5000|team/app" ]
}

# ── newer_same_variant_tag ───────────────────────────────────────────────────

@test "newer_same_variant_tag: suffixed family compares within the same variant" {
    result="$(printf '%s\n' 24.01-py3 25.02-py3 26.07-py3 26.08-rockylinux9 \
        | newer_same_variant_tag "24.01-py3")"
    [ "$result" = "26.07-py3" ]
}

@test "newer_same_variant_tag: bare semver pin never matches suffixed tags" {
    result="$(printf '%s\n' 1.38.0 1.39.0 1.39.1-glibc 2.0.0-musl \
        | newer_same_variant_tag "1.38.0")"
    [ "$result" = "1.39.0" ]
}

@test "newer_same_variant_tag: multi-part variant suffix must match exactly" {
    result="$(printf '%s\n' 2.6.0-cuda12.6-cudnn9-runtime 2.13.0-cuda12.6-cudnn9-runtime \
        2.13.0-cuda12.8-cudnn9-runtime 2.13.0-cuda12.6-cudnn9-devel \
        | newer_same_variant_tag "2.6.0-cuda12.6-cudnn9-runtime")"
    [ "$result" = "2.13.0-cuda12.6-cudnn9-runtime" ]
}

@test "newer_same_variant_tag: leading v is accepted and preserved" {
    result="$(printf '%s\n' v0.5.16 v0.5.17 0.4.0 \
        | newer_same_variant_tag "v0.5.16")"
    [ "$result" = "v0.5.17" ]
}

@test "newer_same_variant_tag: empty when the pin is the family's newest" {
    result="$(printf '%s\n' 0.11.0-gpu 0.12.0-gpu 0.12.0-cpu \
        | newer_same_variant_tag "0.12.0-gpu")"
    [ -z "$result" ]
}

@test "newer_same_variant_tag: numeric comparison beats lexicographic order" {
    result="$(printf '%s\n' 9.9.9 10.0.0 \
        | newer_same_variant_tag "9.9.9")"
    [ "$result" = "10.0.0" ]
}

@test "newer_same_variant_tag: date tags never beat a release-numbered pin" {
    # Regression: alpine:3.21 was suggested the 20260805 date tag.
    result="$(printf '%s\n' 3.22 3.24.1 20260805 \
        | newer_same_variant_tag "3.21")"
    [ "$result" = "3.24.1" ]
}

@test "newer_same_variant_tag: commit-counter tags are ignored" {
    # Regression: kuberay/operator:v1.6.2 was suggested a 9831375 tag.
    result="$(printf '%s\n' v1.6.2 9831375 \
        | newer_same_variant_tag "v1.6.2")"
    [ -z "$result" ]
}

@test "newer_same_variant_tag: nightly build components are ignored" {
    # Regression: ray:2.56.1 was suggested the 2.57.0.397131 nightly.
    result="$(printf '%s\n' 2.57.0 2.57.0.397131 \
        | newer_same_variant_tag "2.56.1")"
    [ "$result" = "2.57.0" ]
}

@test "newer_same_variant_tag: a CalVer pin keeps comparing against CalVer tags" {
    result="$(printf '%s\n' 20250101 20260805 \
        | newer_same_variant_tag "20250101")"
    [ "$result" = "20260805" ]
}

# ── tag_listed ───────────────────────────────────────────────────────────────

@test "tag_listed: finds an early tag in a list larger than the pipe buffer" {
    # Regression (2026-09 scan): the old printf-into-grep -q pipeline took
    # SIGPIPE under pipefail whenever the match landed before the end of a
    # >64KiB tag list (docker.io/library/python, nvcr.io tritonserver, ...),
    # inverting "tag present" into a false INCOMPLETE. Build a list well past
    # the pipe buffer with the pinned tag near the top.
    local big_list
    big_list="$(printf '3.14.7-slim\n'; seq -f 'tag-%.0f-suffix' 1 30000)"
    set -o pipefail
    tag_listed "3.14.7-slim" "$big_list"
}

@test "tag_listed: accepts a v-prefix mismatch in either direction" {
    tag_listed "v1.2.3" "$(printf '%s\n' one 1.2.3 two)"
    tag_listed "1.2.3" "$(printf '%s\n' one v1.2.3 two)"
}

@test "tag_listed: exact match only, not substrings" {
    ! tag_listed "1.2.3" "$(printf '%s\n' 1.2.30 11.2.3 1.2.3-slim)"
}

@test "split_pinned_image_ref: decomposes a digest-pinned reference" {
    digest="fd8d9aa63ba2f0982b5304e1ee8d3b90a210bc1ffb5314d980eb6962f1a9715d"
    run split_pinned_image_ref "docker.io/library/busybox:1.38.0@sha256:${digest}"
    [ "$status" -eq 0 ]
    [ "$output" = "docker.io/library/busybox|1.38.0|sha256:${digest}" ]
}

@test "split_pinned_image_ref: rejects tag-only and digest-only references" {
    run split_pinned_image_ref "docker.io/library/busybox:1.38.0"
    [ "$status" -ne 0 ]
    [ -z "$output" ]
    run split_pinned_image_ref "docker.io/library/busybox@sha256:$(printf 'a%.0s' {1..64})"
    [ "$status" -ne 0 ]
}

@test "split_pinned_image_ref: rejects a malformed digest" {
    run split_pinned_image_ref "docker.io/library/busybox:1.38.0@sha256:deadbeef"
    [ "$status" -ne 0 ]
}

@test "split_pinned_image_ref: every committed smoke image parses" {
    # The digest-freshness scan section is only as good as its ability to
    # parse what the harness manifests actually commit.
    found=0
    while read -r ref; do
        [ -z "$ref" ] && continue
        found=1
        run split_pinned_image_ref "$ref"
        [ "$status" -eq 0 ]
    done < <(grep -rhoE \
        "image: [a-zA-Z0-9_./-]+:[a-zA-Z0-9._-]+@sha256:[0-9a-f]{64}" \
        scripts/live_release_validation/manifests/ | sed 's/^image: //' | sort -u)
    [ "$found" -eq 1 ]
}

@test "published_manifest_digest: hashes the raw manifest bytes" {
    tmpdir="$(mktemp -d)"
    printf '%s\n' '#!/bin/bash' 'printf "manifest-bytes"' > "$tmpdir/skopeo"
    chmod +x "$tmpdir/skopeo"
    expected="sha256:$(printf 'manifest-bytes' | sha256sum | awk '{print $1}')"
    PATH="$tmpdir:$PATH" run published_manifest_digest "docker.io/library/busybox:1.38.0"
    [ "$status" -eq 0 ]
    [ "$output" = "$expected" ]
    rm -rf "$tmpdir"
}

@test "published_manifest_digest: transport failure returns nonzero, no output" {
    tmpdir="$(mktemp -d)"
    printf '%s\n' '#!/bin/bash' 'exit 1' > "$tmpdir/skopeo"
    chmod +x "$tmpdir/skopeo"
    PATH="$tmpdir:$PATH" run published_manifest_digest "docker.io/library/busybox:1.38.0"
    [ "$status" -ne 0 ]
    [ -z "$output" ]
    rm -rf "$tmpdir"
}

@test "tag_listed: fails for an absent tag" {
    # The true-positive path: rayproject/ray:2.57.0 was withdrawn upstream
    # after being pinned; the INCOMPLETE for it was correct and the check
    # must keep firing when the tag is genuinely unlisted.
    ! tag_listed "2.57.0" "$(printf '%s\n' 2.56.0 2.56.1 2.57.0.106e80)"
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

@test "extract_aurora_versions: finds the pinned version via the constants module" {
    run extract_aurora_versions "gco/stacks/regional_stack.py"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]
}

# The extract_aurora_versions fallback tests shadow the installed gco package
# with one whose __init__ raises ImportError. ``python3 -c`` puts the current
# directory first on sys.path, so cd'ing into the shadow directory forces the
# import branch to fail deterministically — regardless of whether the real
# package is installed (it always is in the deps-scan workflow) — and the
# regex-over-constants.py branch is what actually gets exercised.
_aurora_fallback_shadow() {
    local tmpdir="$1"
    mkdir -p "${tmpdir}/gco"
    echo 'raise ImportError("forced by test: exercise the regex fallback")' \
        > "${tmpdir}/gco/__init__.py"
    touch "${tmpdir}/regional_stack.py"
}

@test "extract_aurora_versions: regex fallback reads the plain version string" {
    tmpdir="$(mktemp -d)"
    _aurora_fallback_shadow "$tmpdir"
    cat > "${tmpdir}/constants.py" <<'EOF'
SOMETHING_ELSE = "x"
AURORA_POSTGRES_VERSION = "16.6"
EOF
    run bash -c "
        source '$PWD/.github/scripts/lib_dependency_scan.sh'
        cd '${tmpdir}'
        extract_aurora_versions '${tmpdir}/regional_stack.py'
    "
    [ "$status" -eq 0 ]
    [ "$output" = "16.6" ]
    rm -rf "$tmpdir"
}

@test "extract_aurora_versions: non-version constant values are rejected" {
    # A refactor that reintroduces an enum name must not leak into the RDS query.
    tmpdir="$(mktemp -d)"
    _aurora_fallback_shadow "$tmpdir"
    cat > "${tmpdir}/constants.py" <<'EOF'
AURORA_POSTGRES_VERSION = "VER_17_9"
EOF
    run bash -c "
        source '$PWD/.github/scripts/lib_dependency_scan.sh'
        cd '${tmpdir}'
        extract_aurora_versions '${tmpdir}/regional_stack.py'
    "
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -rf "$tmpdir"
}

@test "extract_aurora_versions: returns empty when no constants file exists" {
    tmpdir="$(mktemp -d)"
    _aurora_fallback_shadow "$tmpdir"
    run bash -c "
        source '$PWD/.github/scripts/lib_dependency_scan.sh'
        cd '${tmpdir}'
        extract_aurora_versions '${tmpdir}/regional_stack.py'
    "
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -rf "$tmpdir"
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

# ── extract_chart_value_images ───────────────────────────────────────────────

@test "extract_chart_value_images: qualifies the trainer controller image with its registry" {
    # The regression this extractor fixes: a registry-split pin
    # (registry: ghcr.io + multi-segment repository) must emit fully
    # qualified, or the tag sweep resolves it against docker.io and the
    # monthly scan goes permanently INCOMPLETE.
    run extract_chart_value_images "lambda/helm-installer/charts.yaml"
    [ "$status" -eq 0 ]
    [[ "$output" == *"ghcr.io/kubeflow/trainer/trainer-controller-manager:v"* ]]
    [[ "$output" != *$'\n'"kubeflow/trainer/trainer-controller-manager:v"* ]]
}

@test "extract_chart_value_images: handles every pin shape in a fixture" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
charts:
  demo:
    values:
      image:
        registry: ghcr.io
        repository: org/sub/app
        tag: "v1.2.3"
      sidecar:
        image:
          repository: example/tool
          tag: "4.5.6"
      bare:
        image:
          repository: single-segment
          tag: "7.8.9"
      tagless:
        image:
          registry: ghcr.io
          repository: kedacore/keda
EOF
    run extract_chart_value_images "$tmpfile"
    [ "$status" -eq 0 ]
    [[ "$output" == *"ghcr.io/org/sub/app:v1.2.3"* ]]
    [[ "$output" == *"example/tool:4.5.6"* ]]
    # Ambiguous single-segment repositories and tag-less pins (their images
    # follow the chart appVersion, which the chart version sweep reports)
    # stay un-emitted.
    [[ "$output" != *"single-segment"* ]]
    [[ "$output" != *"kedacore/keda"* ]]
    rm -f "$tmpfile"
}

@test "extract_chart_value_images: returns empty for a missing file" {
    run extract_chart_value_images "/nonexistent/charts.yaml"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── extract_dockerfile_pins ──────────────────────────────────────────────────

@test "extract_dockerfile_pins: finds all eight pins in Dockerfile.dev" {
    run extract_dockerfile_pins "Dockerfile.dev"
    [ "$status" -eq 0 ]
    # All eight allowlisted pins should be present.
    [[ "$output" == *"NODE_VERSION|"* ]]
    [[ "$output" == *"NPM_VERSION|"* ]]
    [[ "$output" == *"CDK_VERSION|"* ]]
    [[ "$output" == *"KUBECTL_VERSION|"* ]]
    [[ "$output" == *"AWSCLI_VERSION|"* ]]
    [[ "$output" == *"DOCKER_VERSION|"* ]]
    [[ "$output" == *"BUILDX_VERSION|"* ]]
    [[ "$output" == *"UV_VERSION|"* ]]
}

@test "extract_dockerfile_pins: UV_VERSION is a bare semver (no v prefix)" {
    # astral-sh/uv tags releases with a bare semver (0.12.1) and the
    # release-asset URL embeds it verbatim. Assert the pin matches so the
    # Dockerfile download URL and the deps-scan compare line up.
    run extract_dockerfile_pins "Dockerfile.dev"
    [ "$status" -eq 0 ]
    uv_line="$(echo "$output" | grep '^UV_VERSION|')"
    value="${uv_line#UV_VERSION|}"
    [[ "$value" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
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

@test "is_full_git_commit_sha: accepts complete SHA-1 and SHA-256 ids" {
    sha1="$(printf 'a%.0s' {1..40})"
    sha256="$(printf 'b%.0s' {1..64})"
    is_full_git_commit_sha "$sha1"
    is_full_git_commit_sha "$sha256"
}

@test "is_full_git_commit_sha: rejects short hashes and mutable refs" {
    ! is_full_git_commit_sha "0123456789abcdef"
    ! is_full_git_commit_sha "main"
    ! is_full_git_commit_sha "stable"
    ! is_full_git_commit_sha "v1.2.3"
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

# ── extract_python_extras ───────────────────────────────────────────────────

@test "extract_python_extras: lists every optional-dependency group in the real pyproject" {
    # The python-drift path installs the project with every extras group so
    # extras-only pins (aws-cdk-lib lives ONLY in ``cdk``) are visible to
    # ``pip list --outdated``. Spot-check groups that exist today.
    run extract_python_extras "pyproject.toml"
    [ "$status" -eq 0 ]
    [[ "$output" =~ (^|$'\n')cdk($|$'\n') ]]
    [[ "$output" =~ (^|$'\n')diagrams($|$'\n') ]]
    [[ "$output" =~ (^|$'\n')test($|$'\n') ]]
    [[ "$output" =~ (^|$'\n')dev($|$'\n') ]]
    [[ "$output" =~ (^|$'\n')image-health-monitor($|$'\n') ]]
}

@test "extract_python_extras: emits bare group names only (no pins, no brackets)" {
    # Output feeds straight into ``pip install -e ".[a,b,c]"`` after a
    # paste-join, so every line must be a bare extras name.
    run extract_python_extras "pyproject.toml"
    [ "$status" -eq 0 ]
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        [[ "$line" =~ ^[A-Za-z0-9._-]+$ ]]
    done <<< "$output"
}

@test "extract_python_extras: agrees with tomllib's own view of the group count" {
    # Guard against the helper silently dropping groups: its line count
    # must equal the number of keys under [project.optional-dependencies].
    expected="$(python3 -c "
import tomllib
with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
print(len(data['project']['optional-dependencies']))
")"
    run extract_python_extras "pyproject.toml"
    [ "$status" -eq 0 ]
    actual="$(printf '%s\n' "$output" | sed '/^$/d' | wc -l | tr -d ' ')"
    [ "$actual" = "$expected" ]
}

@test "extract_python_extras: empty for a missing file" {
    run extract_python_extras "/nonexistent/pyproject.toml"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_python_extras: empty for malformed TOML" {
    tmpfile="$(mktemp)"
    echo '[project not toml' > "$tmpfile"
    run extract_python_extras "$tmpfile"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -f "$tmpfile"
}

# ── extract_build_system_pins ───────────────────────────────────────────────

@test "extract_build_system_pins: the real pyproject pins one exact setuptools" {
    # Policy lock for the repo itself: [build-system] requires must be
    # exactly one entry — setuptools — with an exact ==X.Y.Z pin (the
    # version resolved inside pip's build isolation must not float).
    # ``wheel`` left the list when the pin landed: setuptools >= 70.1
    # vendors its own wheel support and its docs say not to declare it.
    run extract_build_system_pins "pyproject.toml"
    [ "$status" -eq 0 ]
    count="$(echo "$output" | grep -c .)"
    [ "$count" -eq 1 ]
    [[ "$output" =~ ^setuptools\|[0-9]+\.[0-9]+(\.[0-9]+)?\|setuptools==[0-9] ]]
    ! [[ "$output" == *"wheel"* ]]
}

@test "extract_build_system_pins: exact pins carry a version, ranges do not" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
[build-system]
requires = ["setuptools>=77.0.0", "wheel", "Some_Pkg==1.2", "hatchling[extra]==2.0.0"]
build-backend = "setuptools.build_meta"
EOF
    run extract_build_system_pins "$tmpfile"
    [ "$status" -eq 0 ]
    # A range keeps its raw text but gets no version field.
    [[ "$output" == *"setuptools||setuptools>=77.0.0"* ]]
    # A bare name is not an exact pin either.
    [[ "$output" == *"wheel||wheel"* ]]
    # An exact pin is PEP-503 normalised and carries its version.
    [[ "$output" == *"some-pkg|1.2|Some_Pkg==1.2"* ]]
    # Extras make the resolved artifact set non-obvious; treat as non-exact.
    [[ "$output" == *"hatchling||hatchling[extra]==2.0.0"* ]]
    rm -f "$tmpfile"
}

@test "extract_build_system_pins: returns empty for missing or unparseable file" {
    run extract_build_system_pins "/nonexistent/pyproject.toml"
    [ "$status" -eq 0 ]
    [ -z "$output" ]

    tmpfile="$(mktemp)"
    echo "[[[not toml" > "$tmpfile"
    run extract_build_system_pins "$tmpfile"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -f "$tmpfile"
}

# ── check_lambda_requirements_pins ──────────────────────────────────────────

# Builds a synthetic repository whose central pins are boto3 1.43.85 /
# urllib3 2.7.0 (base), kubernetes 36.0.3 (optional group), and cryptography
# 50.0.0 (lock-only transitive), so each resolution tier can be exercised.
_write_pin_fixture() {
    local dir="$1"
    mkdir -p "$dir"
    cat > "$dir/pyproject.toml" <<'EOF'
[project]
name = "fixture"
dependencies = ["boto3==1.43.85", "urllib3==2.7.0"]

[project.optional-dependencies]
runtime = ["kubernetes==36.0.3"]
EOF
    printf 'boto3==1.43.85\ncryptography==50.0.0\n    # via a-transitive\n' \
        > "$dir/requirements-lock.txt"
}

@test "check_lambda_requirements_pins: the committed repository is in lockstep" {
    # Policy lock: every Lambda copy of a centrally pinned package must equal
    # the central version. This is the check whose absence let a boto3 bump
    # land in pyproject and the lock while six Lambda copies stayed behind.
    run check_lambda_requirements_pins . pyproject.toml requirements-lock.txt
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "check_lambda_requirements_pins: reports a stale pin against pyproject" {
    tmpdir="$(mktemp -d)"
    _write_pin_fixture "$tmpdir"
    mkdir -p "$tmpdir/lambda/secret-rotation"
    printf 'boto3==1.43.74\n' > "$tmpdir/lambda/secret-rotation/requirements.txt"
    run check_lambda_requirements_pins "$tmpdir" pyproject.toml requirements-lock.txt
    [ "$status" -eq 0 ]
    [ "$output" = "lambda/secret-rotation/requirements.txt|boto3==1.43.74 must match pyproject.toml 1.43.85" ]
    rm -rf "$tmpdir"
}

@test "check_lambda_requirements_pins: resolves optional groups and lock-only transitives" {
    # A Lambda may pin a package that pyproject only names inside an optional
    # group, or one that no pyproject entry names at all but the lock resolves
    # exactly (cryptography). Both must still be held to the central version.
    tmpdir="$(mktemp -d)"
    _write_pin_fixture "$tmpdir"
    mkdir -p "$tmpdir/lambda/tls-certificate-manager"
    printf 'kubernetes==35.0.0\ncryptography==49.0.0\n' \
        > "$tmpdir/lambda/tls-certificate-manager/requirements.txt"
    run check_lambda_requirements_pins "$tmpdir" pyproject.toml requirements-lock.txt
    [ "$status" -eq 0 ]
    [[ "$output" == *"kubernetes==35.0.0 must match pyproject.toml optional groups 36.0.3"* ]]
    [[ "$output" == *"cryptography==49.0.0 must match requirements-lock.txt 50.0.0"* ]]
    [ "$(printf '%s\n' "$output" | grep -c .)" -eq 2 ]
    rm -rf "$tmpdir"
}

@test "check_lambda_requirements_pins: agreeing, undeclared, and comment-only files are silent" {
    # Three tracked requirements files only document that the Lambda runtime
    # supplies boto3, and some Lambdas pin their own dependencies that have no
    # central copy — neither may become a permanent finding.
    tmpdir="$(mktemp -d)"
    _write_pin_fixture "$tmpdir"
    mkdir -p "$tmpdir/lambda/agrees" "$tmpdir/lambda/runtime-only" "$tmpdir/lambda/own-dep"
    printf 'boto3==1.43.85\nurllib3==2.7.0\n' > "$tmpdir/lambda/agrees/requirements.txt"
    printf '# boto3 and botocore are provided by the Lambda runtime.\n' \
        > "$tmpdir/lambda/runtime-only/requirements.txt"
    printf 'some-lambda-only-package==9.9.9\n' > "$tmpdir/lambda/own-dep/requirements.txt"
    run check_lambda_requirements_pins "$tmpdir" pyproject.toml requirements-lock.txt
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -rf "$tmpdir"
}

@test "check_lambda_requirements_pins: skips the generated -build staging bundles" {
    # The packaged bundles copy requirements.txt from the source directory, so
    # including them would double-report every finding when they happen to
    # exist locally and report nothing in CI, where they do not.
    tmpdir="$(mktemp -d)"
    _write_pin_fixture "$tmpdir"
    mkdir -p "$tmpdir/lambda/helm-installer-build"
    printf 'boto3==1.43.74\n' > "$tmpdir/lambda/helm-installer-build/requirements.txt"
    run check_lambda_requirements_pins "$tmpdir" pyproject.toml requirements-lock.txt
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -rf "$tmpdir"
}

@test "check_lambda_requirements_pins: an unreadable pyproject is a finding, not a pass" {
    # pyproject.toml always exists here, so nothing coming back must surface
    # rather than silently downgrading the check to a pass.
    tmpdir="$(mktemp -d)"
    mkdir -p "$tmpdir/lambda/secret-rotation"
    printf 'boto3==1.43.74\n' > "$tmpdir/lambda/secret-rotation/requirements.txt"
    printf '[[[not toml\n' > "$tmpdir/pyproject.toml"
    run check_lambda_requirements_pins "$tmpdir" pyproject.toml requirements-lock.txt
    [ "$status" -eq 0 ]
    [ "$output" = "pyproject.toml|missing or unparseable, cannot verify Lambda pins" ]

    rm -f "$tmpdir/pyproject.toml"
    run check_lambda_requirements_pins "$tmpdir" pyproject.toml requirements-lock.txt
    [ "$status" -eq 0 ]
    [ "$output" = "pyproject.toml|missing or unparseable, cannot verify Lambda pins" ]
    rm -rf "$tmpdir"
}

@test "check_lambda_requirements_pins: a missing lockfile still checks pyproject pins" {
    # The lock is the last resolution tier; losing it must narrow coverage to
    # the pyproject tiers rather than abandoning the check.
    tmpdir="$(mktemp -d)"
    _write_pin_fixture "$tmpdir"
    rm -f "$tmpdir/requirements-lock.txt"
    mkdir -p "$tmpdir/lambda/secret-rotation"
    printf 'boto3==1.43.74\ncryptography==49.0.0\n' \
        > "$tmpdir/lambda/secret-rotation/requirements.txt"
    run check_lambda_requirements_pins "$tmpdir" pyproject.toml requirements-lock.txt
    [ "$status" -eq 0 ]
    [ "$output" = "lambda/secret-rotation/requirements.txt|boto3==1.43.74 must match pyproject.toml 1.43.85" ]
    rm -rf "$tmpdir"
}

@test "extract_python_pin: reads the real repository pins CI derives" {
    # The moto server step and the Grafana dashboard job install these at
    # runtime from pyproject rather than restating the version, so a bump
    # edits one file. Assert the shape, not the value, so a legitimate bump
    # does not break the test.
    run extract_python_pin moto pyproject.toml
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]

    run extract_python_pin pyyaml pyproject.toml
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

@test "extract_python_pin: agrees with the lock so a constrained install resolves" {
    # The moto step installs with requirements-lock.txt as a constraint, so a
    # pyproject pin that disagrees with the lock makes pip fail outright.
    pinned="$(extract_python_pin moto pyproject.toml)"
    locked="$(grep -E '^moto==' requirements-lock.txt | cut -d= -f3)"
    [ -n "$pinned" ]
    [ "$pinned" = "$locked" ]
}

@test "extract_python_pin: finds base, optional-group, and extras-bearing pins" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
[project]
dependencies = ["boto3==1.2.3", "fastmcp[tasks,code_mode]==4.0.0"]

[project.optional-dependencies]
dev = ["moto==9.9.9", "Some_Pkg==1.2"]
EOF
    run extract_python_pin boto3 "$tmpfile"
    [ "$output" = "1.2.3" ]
    # A group-only pin is still the project's declared version.
    run extract_python_pin moto "$tmpfile"
    [ "$output" = "9.9.9" ]
    # Extras on the declaration are the project's business, not the version's.
    run extract_python_pin fastmcp "$tmpfile"
    [ "$output" = "4.0.0" ]
    # PEP 503: the caller may spell the name any equivalent way.
    run extract_python_pin some-pkg "$tmpfile"
    [ "$output" = "1.2" ]
    run extract_python_pin SOME_PKG "$tmpfile"
    [ "$output" = "1.2" ]
    rm -f "$tmpfile"
}

@test "extract_python_pin: every ambiguous case yields empty so callers fail loudly" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
[project]
dependencies = ["ranged>=1.0", "bare"]

[project.optional-dependencies]
a = ["split==1.0.0"]
b = ["split==2.0.0"]
EOF
    # Absent, non-exact, and bare specifiers give no version to install.
    run extract_python_pin absent "$tmpfile"
    [ -z "$output" ]
    run extract_python_pin ranged "$tmpfile"
    [ -z "$output" ]
    run extract_python_pin bare "$tmpfile"
    [ -z "$output" ]
    # Two groups disagreeing must not silently resolve to one of them.
    run extract_python_pin split "$tmpfile"
    [ -z "$output" ]
    rm -f "$tmpfile"

    run extract_python_pin moto /nonexistent/pyproject.toml
    [ "$status" -eq 0 ]
    [ -z "$output" ]

    tmpfile="$(mktemp)"
    echo "[[[not toml" > "$tmpfile"
    run extract_python_pin moto "$tmpfile"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -f "$tmpfile"
}

@test "extract_python_pin: an environment marker does not leak into the version" {
    tmpfile="$(mktemp)"
    printf '[project]\ndependencies = ["marked==3.2.1 ; python_version < \x275.0\x27"]\n' > "$tmpfile"
    run extract_python_pin marked "$tmpfile"
    [ "$output" = "3.2.1" ]
    rm -f "$tmpfile"
}

@test "check_lambda_requirements_pins: normalises names and ignores inline comments" {
    # ``PyYAML`` and ``pyyaml`` are the same distribution under PEP 503, and a
    # trailing comment must not become part of the version.
    tmpdir="$(mktemp -d)"
    _write_pin_fixture "$tmpdir"
    mkdir -p "$tmpdir/lambda/kubectl-applier-simple"
    printf 'Kubernetes==35.0.0  # pinned for the applier\n' \
        > "$tmpdir/lambda/kubectl-applier-simple/requirements.txt"
    run check_lambda_requirements_pins "$tmpdir" pyproject.toml requirements-lock.txt
    [ "$status" -eq 0 ]
    [ "$output" = "lambda/kubectl-applier-simple/requirements.txt|kubernetes==35.0.0 must match pyproject.toml optional groups 36.0.3" ]
    rm -rf "$tmpdir"
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
    # A plain version string applied via AuroraPostgresEngineVersion.of(),
    # deliberately not a VER_X_Y enum name.
    [[ "$output" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]
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

@test "extract_python_string_constant: reads the immutable AWS CLI image" {
    run extract_python_string_constant AWS_CLI_IMAGE gco/services/inference_monitor.py
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^public\.ecr\.aws/aws-cli/aws-cli:[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}$ ]]
}

@test "extract_python_string_constant: tolerates source grammar newer than system Python" {
    [ -x /usr/bin/python3 ] || skip "/usr/bin/python3 is unavailable"
    run env PATH="/usr/bin:/bin" /bin/bash -c \
        'source .github/scripts/lib_dependency_scan.sh; extract_python_string_constant AWS_CLI_IMAGE gco/services/inference_monitor.py'
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^public\.ecr\.aws/aws-cli/aws-cli:[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}$ ]]
}

@test "extract_python_string_constant: ignores unrelated unparsable source" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
IMAGE = (
    "registry.example/image:1.2.3@"
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)
this is deliberately invalid Python ???
EOF
    run extract_python_string_constant IMAGE "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$output" = "registry.example/image:1.2.3@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ]
    rm -f "$tmpfile"
}

@test "extract_python_string_constant: folds adjacent literals without importing" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
IMAGE = (
    "registry.example/image:1.2.3@"
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)
raise RuntimeError("must not execute")
EOF
    run extract_python_string_constant IMAGE "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$output" = "registry.example/image:1.2.3@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ]
    rm -f "$tmpfile"
}

@test "extract_python_string_constant: empty for missing or malformed modules" {
    run extract_python_string_constant IMAGE /nonexistent/module.py
    [ "$status" -eq 0 ]
    [ -z "$output" ]

    tmpfile="$(mktemp)"
    printf '%s\n' 'IMAGE = (' > "$tmpfile"
    run extract_python_string_constant IMAGE "$tmpfile"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
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
    print(json.load(handle)["context"]["bedrock"]["mission_default_model_id"])
')"
    [ -n "$expected" ]
    run extract_default_bedrock_model "cdk.json"
    [ "$status" -eq 0 ]
    [ "$output" = "$expected" ]
}

@test "extract_default_bedrock_model: default leaf is mission_default_model_id" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
{"context":{"bedrock":{"mission_default_model_id":"us.amazon.nova-pro-v1:0"}}}
EOF
    run extract_default_bedrock_model "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$output" = "us.amazon.nova-pro-v1:0" ]
    rm -f "$tmpfile"
}

@test "extract_default_bedrock_model: reads the capacity advisor leaf from cdk.json" {
    expected="$(python3 -c '
import json
with open("cdk.json") as handle:
    print(json.load(handle)["context"]["bedrock"]["capacity_advisor_default_model_id"])
')"
    [ -n "$expected" ]
    run extract_default_bedrock_model "cdk.json" "capacity_advisor_default_model_id"
    [ "$status" -eq 0 ]
    [ "$output" = "$expected" ]
}

@test "extract_default_bedrock_model: retired default_model_id leaf reads nothing" {
    # The pre-v6 single advisory key is no longer shipped in cdk.json; the
    # runtime fails closed on it and the scan must not resurrect it.
    run extract_default_bedrock_model "cdk.json" "default_model_id"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
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

@test "extract_default_bedrock_model: reads the claude code leaf from cdk.json" {
    expected="$(python3 -c '
import json
with open("cdk.json") as handle:
    print(json.load(handle)["context"]["bedrock"]["claude_code_default_model_id"])
')"
    [ -n "$expected" ]
    run extract_default_bedrock_model "cdk.json" "claude_code_default_model_id"
    [ "$status" -eq 0 ]
    [ "$output" = "$expected" ]
}

@test "extract_default_bedrock_model: reads the Codex leaf from cdk.json" {
    expected="$(python3 -c '
import json
with open("cdk.json") as handle:
    print(json.load(handle)["context"]["bedrock"]["codex_default_model_id"])
')"
    [ -n "$expected" ]
    run extract_default_bedrock_model "cdk.json" "codex_default_model_id"
    [ "$status" -eq 0 ]
    [ "$output" = "$expected" ]
}

@test "extract_default_bedrock_model: explicit leaf selects the requested key" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
{"context":{"bedrock":{"mission_default_model_id":"us.amazon.nova-pro-v1:0","claude_code_default_model_id":"us.anthropic.claude-sonnet-4-6"}}}
EOF
    run extract_default_bedrock_model "$tmpfile" "claude_code_default_model_id"
    [ "$status" -eq 0 ]
    [ "$output" = "us.anthropic.claude-sonnet-4-6" ]
    rm -f "$tmpfile"
}

@test "extract_default_bedrock_model: empty when the requested leaf is absent" {
    tmpfile="$(mktemp)"
    echo '{"context":{"bedrock":{"mission_default_model_id":"us.amazon.nova-pro-v1:0"}}}' > "$tmpfile"
    run extract_default_bedrock_model "$tmpfile" "claude_code_default_model_id"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -f "$tmpfile"
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

@test "bedrock_model_family: OpenAI GPT drops a dotted numeric model version" {
    current="$(bedrock_model_family "global.openai.gpt-5.6-sol")"
    candidate="$(bedrock_model_family "global.openai.gpt-5.7-sol")"
    [ "$current" = "global.openai.gpt-sol" ]
    [ "$candidate" = "$current" ]
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

@test "compare_bedrock_model: GPT 5.6 -> GPT 5.7 is newer" {
    result="$(compare_bedrock_model "global.openai.gpt-5.6-sol" "global.openai.gpt-5.7-sol")"
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

@test "get_latest_bedrock_model: selects GPT 5.7 for the GPT 5.6 Codex default" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/aws" <<'SHIM'
#!/usr/bin/env bash
cat <<'JSON'
{"inferenceProfileSummaries":[
  {"inferenceProfileId":"global.openai.gpt-5.6-sol","status":"ACTIVE"},
  {"inferenceProfileId":"global.openai.gpt-5.7-sol","status":"ACTIVE"},
  {"inferenceProfileId":"global.openai.gpt-9.9-terra","status":"ACTIVE"},
  {"inferenceProfileId":"us.openai.gpt-9.9-sol","status":"ACTIVE"},
  {"inferenceProfileId":"global.openai.gpt-8.0-sol","status":"LEGACY"}
]}
JSON
SHIM
    chmod +x "$tmpdir/aws"
    PATH="$tmpdir:$PATH" run get_latest_bedrock_model \
        "global.openai.gpt-5.6-sol" us-east-1
    [ "$status" -eq 0 ]
    [ "$output" = "global.openai.gpt-5.7-sol" ]
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

# ── get_latest_bedrock_embedding_model ──────────────────────────────────
#
# Mission memory's embedding default (context.bedrock.embedding_model_id)
# is a plain foundation model, so its drift lookup shells out to
# ``aws bedrock list-foundation-models --by-output-modality EMBEDDING``
# instead of the inference-profile listing. Same shim pattern as the
# get_latest_bedrock_model tests: family scoping, lifecycle filtering, and
# version ranking run offline against canned JSON.

@test "extract_default_bedrock_model: block argument reads vector_store from cdk.json" {
    expected="$(python3 -c '
import json
with open("cdk.json") as handle:
    print(json.load(handle)["context"]["vector_store"]["embedding_model_id"])
')"
    [ -n "$expected" ]
    run extract_default_bedrock_model "cdk.json" "embedding_model_id" "vector_store"
    [ "$status" -eq 0 ]
    [ "$output" = "$expected" ]
}

@test "extract_default_bedrock_model: block argument defaults to bedrock" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'JSON'
{"context":{"bedrock":{"embedding_model_id":"from-bedrock"},"vector_store":{"embedding_model_id":"from-vector-store"}}}
JSON
    run extract_default_bedrock_model "$tmpfile" "embedding_model_id"
    [ "$status" -eq 0 ]
    [ "$output" = "from-bedrock" ]
    run extract_default_bedrock_model "$tmpfile" "embedding_model_id" "vector_store"
    [ "$status" -eq 0 ]
    [ "$output" = "from-vector-store" ]
    rm -f "$tmpfile"
}

@test "extract_default_bedrock_model: empty when the requested block is absent" {
    tmpfile="$(mktemp)"
    echo '{"context":{"bedrock":{"embedding_model_id":"x"}}}' > "$tmpfile"
    run extract_default_bedrock_model "$tmpfile" "embedding_model_id" "vector_store"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -f "$tmpfile"
}

@test "extract_default_bedrock_model: reads the embedding leaf from cdk.json" {
    expected="$(python3 -c '
import json
with open("cdk.json") as handle:
    print(json.load(handle)["context"]["bedrock"]["embedding_model_id"])
')"
    [ -n "$expected" ]
    run extract_default_bedrock_model "cdk.json" "embedding_model_id"
    [ "$status" -eq 0 ]
    [ "$output" = "$expected" ]
}

@test "get_latest_bedrock_embedding_model: returns the newest ACTIVE same-family model" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/aws" <<'SHIM'
#!/usr/bin/env bash
cat <<'JSON'
{"modelSummaries":[
  {"modelId":"amazon.titan-embed-text-v1","modelLifecycle":{"status":"ACTIVE"}},
  {"modelId":"amazon.titan-embed-text-v2:0","modelLifecycle":{"status":"ACTIVE"}},
  {"modelId":"amazon.titan-embed-text-v3:0","modelLifecycle":{"status":"ACTIVE"}},
  {"modelId":"amazon.titan-embed-image-v9:0","modelLifecycle":{"status":"ACTIVE"}},
  {"modelId":"cohere.embed-english-v9","modelLifecycle":{"status":"ACTIVE"}},
  {"modelId":"amazon.titan-embed-text-v9:0","modelLifecycle":{"status":"LEGACY"}}
]}
JSON
SHIM
    chmod +x "$tmpdir/aws"
    PATH="$tmpdir:$PATH" run get_latest_bedrock_embedding_model \
        "amazon.titan-embed-text-v2:0" us-east-1
    [ "$status" -eq 0 ]
    # v3:0 is the newest ACTIVE titan-embed-text; the LEGACY v9:0 is skipped
    # and other families (titan-embed-image, cohere) are filtered out.
    [ "$output" = "amazon.titan-embed-text-v3:0" ]
    rm -rf "$tmpdir"
}

@test "get_latest_bedrock_embedding_model: no drift when the pin is newest" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/aws" <<'SHIM'
#!/usr/bin/env bash
cat <<'JSON'
{"modelSummaries":[
  {"modelId":"amazon.titan-embed-text-v1","modelLifecycle":{"status":"ACTIVE"}},
  {"modelId":"amazon.titan-embed-text-v2:0","modelLifecycle":{"status":"ACTIVE"}}
]}
JSON
SHIM
    chmod +x "$tmpdir/aws"
    PATH="$tmpdir:$PATH" run get_latest_bedrock_embedding_model \
        "amazon.titan-embed-text-v2:0" us-east-1
    [ "$status" -eq 0 ]
    [ "$output" = "amazon.titan-embed-text-v2:0" ]
    rm -rf "$tmpdir"
}

@test "get_latest_bedrock_embedding_model: tolerates a missing modelLifecycle" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/aws" <<'SHIM'
#!/usr/bin/env bash
cat <<'JSON'
{"modelSummaries":[
  {"modelId":"amazon.titan-embed-text-v2:0"},
  {"modelId":"amazon.titan-embed-text-v4:0"}
]}
JSON
SHIM
    chmod +x "$tmpdir/aws"
    PATH="$tmpdir:$PATH" run get_latest_bedrock_embedding_model \
        "amazon.titan-embed-text-v2:0" us-east-1
    [ "$status" -eq 0 ]
    [ "$output" = "amazon.titan-embed-text-v4:0" ]
    rm -rf "$tmpdir"
}

@test "get_latest_bedrock_embedding_model: empty when the aws call fails" {
    tmpdir="$(mktemp -d)"
    printf '#!/usr/bin/env bash\nexit 1\n' > "$tmpdir/aws"
    chmod +x "$tmpdir/aws"
    PATH="$tmpdir:$PATH" run get_latest_bedrock_embedding_model \
        "amazon.titan-embed-text-v2:0" us-east-1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -rf "$tmpdir"
}

@test "get_latest_bedrock_embedding_model: empty for empty input" {
    run get_latest_bedrock_embedding_model ""
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

# ── extract_install_trivy_pin ───────────────────────────────────────────────

@test "extract_install_trivy_pin: reads the version default from the composite action" {
    # The Trivy pin lives only in .github/actions/install-trivy/action.yml;
    # the workflows carry no TRIVY_VERSION copies of their own anymore.
    run extract_install_trivy_pin
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]
    run extract_workflow_env_pin TRIVY_VERSION
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_install_trivy_pin: empty for a missing file" {
    run extract_install_trivy_pin /nonexistent/action.yml
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_install_trivy_pin: empty when the default is absent" {
    tmpdir="$(mktemp -d)"
    printf 'inputs:\n  version:\n    required: true\n' > "$tmpdir/action.yml"
    run extract_install_trivy_pin "$tmpdir/action.yml"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -rf "$tmpdir"
}

# ── extract_helm_installer_pins ─────────────────────────────────────────────
# Load-bearing beyond the monthly scan: integration-tests.yml and
# deps-scan.yml source the library and pipe this function's output into
# GITHUB_ENV, so its four lines ARE the Helm/kubectl pins CI installs.

@test "extract_helm_installer_pins: emits all four pins from the real Dockerfile" {
    run extract_helm_installer_pins
    [ "$status" -eq 0 ]
    [ "$(echo "$output" | grep -c .)" -eq 4 ]
    [[ "$output" == *"HELM_VERSION|v"* ]]
    [[ "$output" =~ HELM_SHA256\|[0-9a-f]{64} ]]
    [[ "$output" == *"KUBECTL_VERSION|v1."* ]]
    [[ "$output" =~ KUBECTL_SHA256\|[0-9a-f]{64} ]]
}

@test "extract_helm_installer_pins: GITHUB_ENV shape survives the tr pipeline" {
    # Exactly the pipeline the workflow derive steps run.
    run bash -c 'source .github/scripts/lib_dependency_scan.sh; extract_helm_installer_pins | tr "|" "="'
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^HELM_VERSION=v[0-9]+\.[0-9]+\.[0-9]+$'\n' ]]
    [[ "$output" == *"KUBECTL_SHA256="* ]]
}

@test "extract_helm_installer_pins: empty for a missing Dockerfile" {
    run extract_helm_installer_pins /nonexistent/Dockerfile
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_helm_installer_pins: a version without its trust anchor emits no sha line" {
    # The workflow derive steps grep for every expected pin afterwards, so
    # a Dockerfile edit that drops the sha256 line fails the derive step
    # instead of installing an unauthenticated binary.
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
RUN curl -o /tmp/helm.tar.gz https://get.helm.sh/helm-v9.9.9-linux-amd64.tar.gz
EOF
    run extract_helm_installer_pins "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$output" = "HELM_VERSION|v9.9.9" ]
    rm -f "$tmpfile"
}

@test "extract_workflow_env_pin: HELM_VERSION and KUBECTL_VERSION carry no workflow copies" {
    # Both pins live only in lambda/helm-installer/Dockerfile; workflows
    # derive them into GITHUB_ENV at runtime, so the env extractor must
    # find nothing to drift.
    run extract_workflow_env_pin HELM_VERSION
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    run extract_workflow_env_pin KUBECTL_VERSION
    [ "$status" -eq 0 ]
    [ -z "$output" ]
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

@test "extract_workflow_env_pin: reads actionlint and Calico maintenance pins" {
    run extract_workflow_env_pin ACTIONLINT_VERSION
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]]

    run extract_workflow_env_pin CALICO_VERSION
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

@test "extract_kind_pins: resolves \${{ env.* }} references against workflow env" {
    # The production shape: kind-action steps reference the single
    # workflow-level declarations rather than carrying literal copies.
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
env:
  KIND_VERSION: "v0.98.0"
  KIND_NODE_IMAGE: "kindest/node:v1.41.0"
jobs:
  e2e:
    steps:
      - uses: helm/kind-action@v1.14.0
        with:
          version: "${{ env.KIND_VERSION }}"
          node_image: "${{ env.KIND_NODE_IMAGE }}"
EOF
    run extract_kind_pins "$tmpfile"
    [ "$status" -eq 0 ]
    [[ "$output" == *"kind|v0.98.0"* ]]
    [[ "$output" == *"kind-node|kindest/node:v1.41.0"* ]]
    rm -f "$tmpfile"
}

@test "extract_kind_pins: an unresolvable env reference prints nothing for that key" {
    # A template string must never reach a release lookup; the caller's
    # presence check reports the pin as missing instead.
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
jobs:
  e2e:
    steps:
      - uses: helm/kind-action@v1.14.0
        with:
          version: "${{ env.NOT_DECLARED }}"
          node_image: "kindest/node:v1.40.0"
EOF
    run extract_kind_pins "$tmpfile"
    [ "$status" -eq 0 ]
    [[ "$output" != *"kind|"* || "$output" == *"kind-node|"* ]]
    [[ "$output" != *"NOT_DECLARED"* ]]
    [[ "$output" == *"kind-node|kindest/node:v1.40.0"* ]]
    rm -f "$tmpfile"
}

@test "extract_kind_pins: identical pins across two kind-action steps print once" {
    # cluster-e2e and examples-smoke both create kind clusters; agreement on
    # the pins must collapse to one line per key so the single-value callers
    # (check_github_tool, the node-image minor scope) keep working unchanged.
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
jobs:
  e2e:
    steps:
      - uses: helm/kind-action@v1.14.0
        with:
          version: "v0.99.0"
          node_image: "kindest/node:v1.40.0"
  smoke:
    steps:
      - uses: helm/kind-action@v1.14.0
        with:
          version: "v0.99.0"
          node_image: "kindest/node:v1.40.0"
EOF
    run extract_kind_pins "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | grep -c '^kind|')" -eq 1 ]
    [ "$(printf '%s\n' "$output" | grep -c '^kind-node|')" -eq 1 ]
    rm -f "$tmpfile"
}

@test "extract_kind_pins: drifted pins across steps surface as extra lines" {
    # A second line for the same key is the drift signal the consistency
    # section of dependency-scan.sh turns into a report row.
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
jobs:
  e2e:
    steps:
      - uses: helm/kind-action@v1.14.0
        with:
          version: "v0.99.0"
          node_image: "kindest/node:v1.40.0"
  smoke:
    steps:
      - uses: helm/kind-action@v1.14.0
        with:
          version: "v0.98.0"
          node_image: "kindest/node:v1.40.0"
EOF
    run extract_kind_pins "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | grep -c '^kind|')" -eq 2 ]
    [[ "$output" == *"kind|v0.99.0"* ]]
    [[ "$output" == *"kind|v0.98.0"* ]]
    [ "$(printf '%s\n' "$output" | grep -c '^kind-node|')" -eq 1 ]
    rm -f "$tmpfile"
}

@test "extract_kind_pins: real workflow pins agree across all kind-action steps" {
    # The live guard for the two real jobs: exactly one distinct value per
    # key in integration-tests.yml, or the jobs' clusters have diverged.
    run extract_kind_pins ".github/workflows/integration-tests.yml"
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | grep -c '^kind|')" -eq 1 ]
    [ "$(printf '%s\n' "$output" | grep -c '^kind-node|')" -eq 1 ]
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

@test "extract_python_version_pins: reads the single .python-version source" {
    # CI Python is single-sourced: every setup-python step uses
    # python-version-file, so the extractor emits the .python-version pin
    # (plus any stray literal reintroduced in a workflow, for the
    # consistency check to surface).
    run extract_python_version_pins
    [ "$status" -eq 0 ]
    [ -n "$output" ]
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        [[ "$line" =~ ^[0-9]+\.[0-9]+$ ]]
    done <<< "$output"
    [ "$output" = "$(grep -oE '^[0-9]+\.[0-9]+' .python-version)" ]
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
    run extract_python_version_pins "$tmpdir" "$tmpdir/.python-version"
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | grep -c '^3\.14$')" -eq 2 ]
    rm -rf "$tmpdir"
}

@test "extract_python_version_pins: a drifted stray literal joins the version-file pin" {
    # A workflow that reintroduces a literal python-version DIFFERENT from
    # .python-version must surface as two distinct values for the
    # consistency check to report.
    tmpdir="$(mktemp -d)"
    printf '3.14\n' > "$tmpdir/.python-version"
    printf 'jobs:\n  a:\n    steps:\n      - with:\n          python-version: "3.12"\n' > "$tmpdir/a.yml"
    run extract_python_version_pins "$tmpdir" "$tmpdir/.python-version"
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | sort -u | grep -c .)" -eq 2 ]
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

# ── dependency scan completeness + lockfile freshness ───────────────────────

@test "dependency_scan_is_complete: succeeds only with no reasons or skips" {
    reasons="$(mktemp)"
    run dependency_scan_is_complete "$reasons" "" ""
    [ "$status" -eq 0 ]
    printf '%s\n' "registry lookup failed" > "$reasons"
    run dependency_scan_is_complete "$reasons" "" ""
    [ "$status" -ne 0 ]
    : > "$reasons"
    run dependency_scan_is_complete "$reasons" "" "explicit skip"
    [ "$status" -ne 0 ]
    rm -f "$reasons"
}

@test "dependency_scan_is_complete: fails closed without its reason channel" {
    run dependency_scan_is_complete /nonexistent/dependency-scan-reasons ""
    [ "$status" -ne 0 ]
}

@test "check_lockfile_freshness: the real repo lock matches all direct pins" {
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
    [ "$output" = "totally-missing-pkg|2.0.0|<missing>" ]
    rm -r "$tmpdir"
}

@test "check_lockfile_freshness: reports a stale direct version" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/pyproject.toml" <<'EOF'
[project]
name = "demo"
dependencies = ["boto3==2.0.0"]
EOF
    printf 'boto3==1.0.0\n' > "$tmpdir/lock.txt"
    run check_lockfile_freshness "$tmpdir/pyproject.toml" "$tmpdir/lock.txt"
    [ "$status" -eq 0 ]
    [ "$output" = "boto3|2.0.0|1.0.0" ]
    rm -r "$tmpdir"
}

@test "check_lockfile_freshness: normalises names before comparing" {
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
    rm -r "$tmpdir"
}

@test "check_lockfile_freshness: accepts whitespace around an exact pin" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/pyproject.toml" <<'EOF'
[project]
name = "demo"
dependencies = ["boto3 == 1.0.0"]
EOF
    printf 'boto3==1.0.0\n' > "$tmpdir/lock.txt"
    run check_lockfile_freshness "$tmpdir/pyproject.toml" "$tmpdir/lock.txt"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -r "$tmpdir"
}

@test "check_lockfile_freshness: accepts a concrete PEP 440 version" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/pyproject.toml" <<'EOF'
[project]
name = "demo"
dependencies = ["demo-pkg==1!2.0rc1.post2+cpu"]
EOF
    printf 'demo-pkg==1!2.0rc1.post2+cpu\n' > "$tmpdir/lock.txt"
    run check_lockfile_freshness "$tmpdir/pyproject.toml" "$tmpdir/lock.txt"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -r "$tmpdir"
}

@test "check_lockfile_freshness: permits marker-specific versions" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/pyproject.toml" <<'EOF'
[project]
name = "demo"
dependencies = [
    "demo-pkg == 1.0.0 ; python_version < '3.14'",
    "demo-pkg==2.0.0; python_version >= '3.14'",
]
EOF
    cat > "$tmpdir/lock.txt" <<'EOF'
demo-pkg==1.0.0;python_version < '3.14'
demo_pkg == 2.0.0 ; python_version>='3.14'
EOF
    run check_lockfile_freshness "$tmpdir/pyproject.toml" "$tmpdir/lock.txt"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -r "$tmpdir"
}

@test "check_lockfile_freshness: normalises marker whitespace" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/pyproject.toml" <<'EOF'
[project]
name = "demo"
dependencies = [
    "boto3==1.0.0 ; python_version < '3.14' and implementation_name == 'cpython'",
]
EOF
    printf "%s\n" \
        "boto3 == 1.0.0;  python_version<'3.14'  and  implementation_name == 'cpython'" \
        > "$tmpdir/lock.txt"
    run check_lockfile_freshness "$tmpdir/pyproject.toml" "$tmpdir/lock.txt"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -r "$tmpdir"
}

@test "check_lockfile_freshness: normalises equivalent marker quote styles" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/pyproject.toml" <<'EOF'
[project]
name = "demo"
dependencies = ['demo-pkg==1.0.0; python_version < "3.14"']
EOF
    printf "%s\n" "demo-pkg==1.0.0; python_version < '3.14'" > "$tmpdir/lock.txt"
    run check_lockfile_freshness "$tmpdir/pyproject.toml" "$tmpdir/lock.txt"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -r "$tmpdir"
}

@test "check_lockfile_freshness: reports stale versions by marker identity" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/pyproject.toml" <<'EOF'
[project]
name = "demo"
dependencies = [
    "demo-pkg==1.0.0; python_version < '3.14'",
    "demo-pkg==2.0.0; python_version >= '3.14'",
]
EOF
    cat > "$tmpdir/lock.txt" <<'EOF'
demo-pkg==0.9.0; python_version < '3.14'
demo-pkg==2.0.0; python_version >= '3.14'
EOF
    run check_lockfile_freshness "$tmpdir/pyproject.toml" "$tmpdir/lock.txt"
    [ "$status" -eq 0 ]
    [ "$output" = 'demo-pkg;python_version<"3.14"|1.0.0|0.9.0' ]
    rm -r "$tmpdir"
}

@test "check_lockfile_freshness: rejects non-concrete equality constraints" {
    tmpdir="$(mktemp -d)"
    for spec in \
        "demo-pkg==1.*" \
        "demo-pkg==1.0.0,!=1.0.1" \
        "demo-pkg===1.0.0"; do
        printf '[project]\nname = "demo"\ndependencies = ["%s"]\n' "$spec" \
            > "$tmpdir/pyproject.toml"
        printf '%s\n' "$spec" > "$tmpdir/lock.txt"
        run check_lockfile_freshness "$tmpdir/pyproject.toml" "$tmpdir/lock.txt"
        [ "$status" -ne 0 ]
    done
    rm -r "$tmpdir"
}

@test "check_lockfile_freshness: fails closed for missing files" {
    run check_lockfile_freshness /nonexistent/pyproject.toml /nonexistent/lock.txt
    [ "$status" -ne 0 ]
}

@test "check_lockfile_freshness: fails closed for malformed TOML" {
    tmpdir="$(mktemp -d)"
    printf '%s\n' '[project' > "$tmpdir/pyproject.toml"
    printf 'boto3==1.0.0\n' > "$tmpdir/lock.txt"
    run check_lockfile_freshness "$tmpdir/pyproject.toml" "$tmpdir/lock.txt"
    [ "$status" -ne 0 ]
    rm -r "$tmpdir"
}

@test "check_lockfile_freshness: fails closed for malformed lock records" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/pyproject.toml" <<'EOF'
[project]
name = "demo"
dependencies = ["boto3==1.0.0"]
EOF
    printf 'not a pinned requirement\n' > "$tmpdir/lock.txt"
    run check_lockfile_freshness "$tmpdir/pyproject.toml" "$tmpdir/lock.txt"
    [ "$status" -ne 0 ]
    rm -r "$tmpdir"
}

@test "check_lockfile_freshness: fails closed for non-exact direct pins" {
    tmpdir="$(mktemp -d)"
    cat > "$tmpdir/pyproject.toml" <<'EOF'
[project]
name = "demo"
dependencies = ["boto3>=1.0.0"]
EOF
    printf 'boto3==1.0.0\n' > "$tmpdir/lock.txt"
    run check_lockfile_freshness "$tmpdir/pyproject.toml" "$tmpdir/lock.txt"
    [ "$status" -ne 0 ]
    rm -r "$tmpdir"
}

# ── extract_npm_direct_pins ──────────────────────────────────────────────────

@test "extract_npm_direct_pins: reads exact pins from dependencies and devDependencies" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
{
  "dependencies": { "@aws-sdk/client-ssm": "3.1098.0" },
  "devDependencies": { "aws-cdk": "2.1134.0", "cdk-dia": "0.12.3" }
}
EOF
    run extract_npm_direct_pins "$tmpfile"
    [ "$status" -eq 0 ]
    [[ "$output" == *"@aws-sdk/client-ssm|3.1098.0"* ]]
    [[ "$output" == *"aws-cdk|2.1134.0"* ]]
    [[ "$output" == *"cdk-dia|0.12.3"* ]]
    rm -f "$tmpfile"
}

@test "extract_npm_direct_pins: emits pipe-delimited NAME|VERSION pairs only" {
    run extract_npm_direct_pins "package.json"
    [ "$status" -eq 0 ]
    [ -n "$output" ]
    while IFS= read -r line; do
        [[ "$line" =~ ^[@A-Za-z0-9/_.-]+\|[0-9]+\.[0-9]+\.[0-9]+ ]]
    done <<< "$output"
}

@test "extract_npm_direct_pins: skips range specifiers, keeping only exact pins" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
{
  "dependencies": {
    "caret": "^1.0.0",
    "tilde": "~2.0.0",
    "star": "*",
    "gte": ">=3.0.0",
    "tag": "latest",
    "exact": "1.2.3"
  }
}
EOF
    run extract_npm_direct_pins "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$output" = "exact|1.2.3" ]
    rm -f "$tmpfile"
}

@test "extract_npm_direct_pins: ignores packageManager, engines, and overrides" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
{
  "engines": { "node": "24.x" },
  "packageManager": "npm@12.0.2",
  "overrides": { "some-pkg": { "js-yaml": "5.2.2" } },
  "devDependencies": { "only-this": "4.5.6" }
}
EOF
    run extract_npm_direct_pins "$tmpfile"
    [ "$status" -eq 0 ]
    [ "$output" = "only-this|4.5.6" ]
    rm -f "$tmpfile"
}

@test "extract_npm_direct_pins: empty for a missing file" {
    run extract_npm_direct_pins "/nonexistent/package.json"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_npm_direct_pins: empty for malformed JSON" {
    tmpfile="$(mktemp)"
    echo '{ not json' > "$tmpfile"
    run extract_npm_direct_pins "$tmpfile"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    rm -f "$tmpfile"
}

@test "extract_npm_direct_pins: finds every owned graph via list_npm_package_dirs" {
    run list_npm_package_dirs .
    [ "$status" -eq 0 ]
    [[ "$output" == *"."* ]]
    [[ "$output" == *"lambda/inference-streaming-proxy"* ]]
    # And each listed graph yields at least one exact pin.
    while IFS= read -r dir; do
        run extract_npm_direct_pins "$dir/package.json"
        [ "$status" -eq 0 ]
        [ -n "$output" ]
    done <<< "$(list_npm_package_dirs .)"
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

# ── extract_claude_code_pin ──────────────────────────────────────────────────

@test "extract_claude_code_pin: reads the pin from cli/autopilot.py" {
    run extract_claude_code_pin "cli/autopilot.py"
    [ "$status" -eq 0 ]
    # An exact three-part semver, matching the reproducible-install policy.
    [[ "$output" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

@test "extract_claude_code_pin: parses the constant from a fixture" {
    run bash -c '
        source .github/scripts/lib_dependency_scan.sh
        tmpfile="$(mktemp)"
        cat > "$tmpfile" <<EOF
# comment line
CLAUDE_CODE_VERSION = "9.9.9"
EOF
        extract_claude_code_pin "$tmpfile"
        rm -f "$tmpfile"
    '
    [ "$status" -eq 0 ]
    [ "$output" = "9.9.9" ]
}

@test "extract_claude_code_pin: empty when constant absent" {
    run bash -c '
        source .github/scripts/lib_dependency_scan.sh
        tmpfile="$(mktemp)"
        echo "no pin constant here" > "$tmpfile"
        extract_claude_code_pin "$tmpfile"
        rm -f "$tmpfile"
    '
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_claude_code_pin: empty when file is missing" {
    run extract_claude_code_pin "/nonexistent/cli/autopilot.py"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── extract_codex_pin ───────────────────────────────────────────────────────

@test "extract_codex_pin: reads the pin from cli/autopilot.py" {
    run extract_codex_pin "cli/autopilot.py"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

@test "extract_codex_pin: parses the constant from a fixture" {
    run bash -c '
        source .github/scripts/lib_dependency_scan.sh
        tmpfile="$(mktemp)"
        cat > "$tmpfile" <<EOF
# comment line
CODEX_VERSION = "8.7.6"
EOF
        extract_codex_pin "$tmpfile"
        rm -f "$tmpfile"
    '
    [ "$status" -eq 0 ]
    [ "$output" = "8.7.6" ]
}

@test "extract_codex_pin: empty when constant absent" {
    run bash -c '
        source .github/scripts/lib_dependency_scan.sh
        tmpfile="$(mktemp)"
        echo "no pin constant here" > "$tmpfile"
        extract_codex_pin "$tmpfile"
        rm -f "$tmpfile"
    '
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_codex_pin: empty when file is missing" {
    run extract_codex_pin "/nonexistent/cli/autopilot.py"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── extract_companion_mcp_packages ───────────────────────────────────────────

@test "extract_companion_mcp_packages: reads the registry from cli/autopilot.py" {
    run extract_companion_mcp_packages "cli/autopilot.py"
    [ "$status" -eq 0 ]
    # Every line is name|registry|package with a known registry, and the
    # committed registry is non-trivial (a dozen-ish companions).
    local line_count=0
    while IFS='|' read -r name registry package; do
        [ -n "$name" ]
        [[ "$registry" = "npm" || "$registry" = "pypi" ]]
        [ -n "$package" ]
        line_count=$((line_count + 1))
    done <<< "$output"
    [ "$line_count" -ge 10 ]
    # The AWS docs companion anchors the format end-to-end.
    [[ "$output" == *"aws-docs|pypi|awslabs.aws-documentation-mcp-server"* ]]
}

@test "extract_companion_mcp_packages: parses entries from a fixture" {
    run bash -c '
        source .github/scripts/lib_dependency_scan.sh
        tmpfile="$(mktemp)"
        cat > "$tmpfile" <<EOF
COMPANION_MCP_SERVERS = (
    CompanionServer(
        name="alpha",
        registry="npm",
        package="@scope/alpha",
        command="npx",
    ),
    CompanionServer(
        name="beta",
        registry="pypi",
        package="beta-server",
        command="uvx",
    ),
)
EOF
        extract_companion_mcp_packages "$tmpfile"
        rm -f "$tmpfile"
    '
    [ "$status" -eq 0 ]
    [ "${lines[0]}" = "alpha|npm|@scope/alpha" ]
    [ "${lines[1]}" = "beta|pypi|beta-server" ]
}

@test "extract_companion_mcp_packages: empty when no registry blocks exist" {
    run bash -c '
        source .github/scripts/lib_dependency_scan.sh
        tmpfile="$(mktemp)"
        echo "def unrelated(): pass" > "$tmpfile"
        extract_companion_mcp_packages "$tmpfile"
        rm -f "$tmpfile"
    '
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "extract_companion_mcp_packages: empty when file is missing" {
    run extract_companion_mcp_packages "/nonexistent/cli/autopilot.py"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ── get_registry_package_status ──────────────────────────────────────────────

@test "get_registry_package_status: empty for an unknown registry" {
    run get_registry_package_status "cargo" "some-crate"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# get_registry_package_status talks to npm/PyPI through curl. These tests
# steer it with a PATH-shimmed curl that serves a canned body + HTTP code,
# so every parse branch (ok / deprecated / yanked / missing / transient)
# is exercised offline against the real function.
make_registry_curl_shim() {
    local dir="$1"
    cat > "$dir/curl" <<'SHIM'
#!/usr/bin/env bash
# Minimal stand-in for: curl -sSL --max-time N -o <file> -w '%{http_code}' <url>
out=""
url=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        -w) shift 2 ;;
        --max-time) shift 2 ;;
        -*) shift ;;
        *) url="$1"; shift ;;
    esac
done
if [ -n "${GCO_FAKE_URL_LOG:-}" ]; then printf '%s\n' "$url" >> "$GCO_FAKE_URL_LOG"; fi
if [ -n "$out" ] && [ -n "${GCO_FAKE_BODY:-}" ]; then printf '%s' "$GCO_FAKE_BODY" > "$out"; fi
printf '%s' "${GCO_FAKE_HTTP_CODE:-200}"
SHIM
    chmod +x "$dir/curl"
}

@test "get_registry_package_status: healthy npm package reports ok|version" {
    shimdir="$(mktemp -d)"
    make_registry_curl_shim "$shimdir"
    GCO_FAKE_HTTP_CODE=200 GCO_FAKE_BODY='{"version":"1.2.3"}' \
        PATH="$shimdir:$PATH" run get_registry_package_status npm some-package
    rm -rf "$shimdir"
    [ "$status" -eq 0 ]
    [ "$output" = "ok|1.2.3" ]
}

@test "get_registry_package_status: valid JSON without a version is incomplete" {
    shimdir="$(mktemp -d)"
    make_registry_curl_shim "$shimdir"
    GCO_FAKE_HTTP_CODE=200 GCO_FAKE_BODY='{}' \
        PATH="$shimdir:$PATH" run get_registry_package_status npm some-package
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    GCO_FAKE_HTTP_CODE=200 GCO_FAKE_BODY='{"info":{},"urls":[]}' \
        PATH="$shimdir:$PATH" run get_registry_package_status pypi some-package
    rm -r "$shimdir"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "get_registry_package_status: deprecated npm package reports the message" {
    shimdir="$(mktemp -d)"
    make_registry_curl_shim "$shimdir"
    GCO_FAKE_HTTP_CODE=200 \
        GCO_FAKE_BODY='{"version":"1.2.3","deprecated":"use other-package instead"}' \
        PATH="$shimdir:$PATH" run get_registry_package_status npm some-package
    rm -rf "$shimdir"
    [ "$status" -eq 0 ]
    [ "$output" = "deprecated|use other-package instead" ]
}

@test "get_registry_package_status: scoped npm package percent-encodes the slash" {
    shimdir="$(mktemp -d)"
    make_registry_curl_shim "$shimdir"
    export GCO_FAKE_URL_LOG="$shimdir/urls.log"
    GCO_FAKE_HTTP_CODE=200 GCO_FAKE_BODY='{"version":"0.1.0"}' \
        PATH="$shimdir:$PATH" run get_registry_package_status npm "@scope/name"
    url="$(cat "$GCO_FAKE_URL_LOG")"
    unset GCO_FAKE_URL_LOG
    rm -rf "$shimdir"
    [ "$status" -eq 0 ]
    [[ "$url" == *"registry.npmjs.org/@scope%2Fname/latest"* ]]
}

@test "get_registry_package_status: healthy pypi package reports ok|version" {
    shimdir="$(mktemp -d)"
    make_registry_curl_shim "$shimdir"
    GCO_FAKE_HTTP_CODE=200 \
        GCO_FAKE_BODY='{"info":{"version":"2.0.0"},"urls":[{"yanked":false}]}' \
        PATH="$shimdir:$PATH" run get_registry_package_status pypi some-package
    rm -rf "$shimdir"
    [ "$status" -eq 0 ]
    [ "$output" = "ok|2.0.0" ]
}

@test "get_registry_package_status: yanked pypi release reports yanked" {
    shimdir="$(mktemp -d)"
    make_registry_curl_shim "$shimdir"
    GCO_FAKE_HTTP_CODE=200 \
        GCO_FAKE_BODY='{"info":{"version":"2.0.0"},"urls":[{"yanked":true}]}' \
        PATH="$shimdir:$PATH" run get_registry_package_status pypi some-package
    rm -rf "$shimdir"
    [ "$status" -eq 0 ]
    [ "$output" = "yanked|2.0.0" ]
}

@test "get_registry_package_status: 404 reports missing" {
    shimdir="$(mktemp -d)"
    make_registry_curl_shim "$shimdir"
    GCO_FAKE_HTTP_CODE=404 \
        PATH="$shimdir:$PATH" run get_registry_package_status npm gone-package
    rm -rf "$shimdir"
    [ "$status" -eq 0 ]
    [ "$output" = "missing|" ]
}

@test "get_registry_package_status: transient HTTP failure prints nothing (skip)" {
    shimdir="$(mktemp -d)"
    make_registry_curl_shim "$shimdir"
    GCO_FAKE_HTTP_CODE=503 \
        PATH="$shimdir:$PATH" run get_registry_package_status npm some-package
    rm -rf "$shimdir"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "get_registry_package_status: malformed registry JSON prints nothing (skip)" {
    shimdir="$(mktemp -d)"
    make_registry_curl_shim "$shimdir"
    GCO_FAKE_HTTP_CODE=200 GCO_FAKE_BODY='not json at all' \
        PATH="$shimdir:$PATH" run get_registry_package_status npm some-package
    rm -rf "$shimdir"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}
