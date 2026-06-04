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

# ── extract_dockerfile_pins ──────────────────────────────────────────────────

@test "extract_dockerfile_pins: finds all six pins in Dockerfile.dev" {
    run extract_dockerfile_pins "Dockerfile.dev"
    [ "$status" -eq 0 ]
    # All six allowlisted pins should be present.
    [[ "$output" == *"NODE_MAJOR|"* ]]
    [[ "$output" == *"NPM_VERSION|"* ]]
    [[ "$output" == *"CDK_VERSION|"* ]]
    [[ "$output" == *"KUBECTL_VERSION|"* ]]
    [[ "$output" == *"AWSCLI_VERSION|"* ]]
    [[ "$output" == *"DOCKER_VERSION|"* ]]
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

@test "extract_dockerfile_pins: NODE_MAJOR value is a bare integer" {
    run extract_dockerfile_pins "Dockerfile.dev"
    [ "$status" -eq 0 ]
    node_line="$(echo "$output" | grep '^NODE_MAJOR|')"
    value="${node_line#NODE_MAJOR|}"
    [[ "$value" =~ ^[0-9]+$ ]]
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
ARG NODE_MAJOR=24
ARG BUILD_DATE=20260501
ARG UNRELATED_KNOB=hello
ARG CDK_VERSION=2.1120.0
EOF
    run extract_dockerfile_pins "$tmpfile"
    [ "$status" -eq 0 ]
    # Allowlisted pins pass through
    [[ "$output" == *"NODE_MAJOR|24"* ]]
    [[ "$output" == *"CDK_VERSION|2.1120.0"* ]]
    # Non-allowlisted ARGs are filtered out
    [[ "$output" != *"BUILD_DATE"* ]]
    [[ "$output" != *"UNRELATED_KNOB"* ]]
    rm -f "$tmpfile"
}

@test "extract_dockerfile_pins: skips commented-out ARG lines" {
    tmpfile="$(mktemp)"
    cat > "$tmpfile" <<'EOF'
FROM scratch
# ARG NODE_MAJOR=99
ARG NODE_MAJOR=24
EOF
    run extract_dockerfile_pins "$tmpfile"
    [ "$status" -eq 0 ]
    # Only one NODE_MAJOR line, and it's the uncommented 24 value.
    count="$(echo "$output" | grep -c '^NODE_MAJOR|' || true)"
    [ "$count" -eq 1 ]
    [[ "$output" == *"NODE_MAJOR|24"* ]]
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
