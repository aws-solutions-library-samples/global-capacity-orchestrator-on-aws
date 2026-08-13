#!/usr/bin/env bash
# =============================================================================
# dependency-scan.sh — check Python, Node.js, Docker, Helm, and EKS versions
# =============================================================================
#
# Invoked by .github/workflows/deps-scan.yml (monthly schedule).
#
# Checks for drift across:
#
#   - Python packages pinned in pyproject.toml
#   - Docker images referenced from workflows, K8s manifests, examples,
#     local live-validation manifests, and Helm chart values
#   - Helm chart versions from charts.yaml
#   - EKS add-on versions from gco/stacks/constants.py (AWS creds)
#   - EKS Kubernetes minor from cdk.json (AWS creds)
#   - Aurora PostgreSQL engine versions (AWS creds)
#   - EMR Serverless release labels (AWS creds)
#   - Bedrock default model ids from cdk.json
#     context.bedrock.mission_default_model_id (Mission sampling),
#     context.bedrock.capacity_advisor_default_model_id (capacity advisor),
#     context.bedrock.claude_code_default_model_id (autopilot),
#     context.bedrock.embedding_model_id (Mission memory), and
#     context.vector_store.embedding_model_id (workload RAG corpus), each
#     compared against the newest same-family release — inference profiles
#     for the generation keys, EMBEDDING foundation models for the embedding
#     keys (AWS creds)
#   - Accelerator catalog and Karpenter NodePool policy (offline), plus live
#     NVIDIA GPU / AWS Neuron EC2 catalog drift across enabled Regions (AWS creds)
#   - Dockerfile.dev ARG pins (Node LTS major, npm, CDK CLI, kubectl,
#     AWS CLI v2, Docker CLI, Docker Buildx, uv) and the immutable AWS CLI
#     runtime image in gco/services/inference_monitor.py — public registries,
#     no AWS creds needed
#   - GCO Autopilot pins from cli/autopilot.py: the CLAUDE_CODE_VERSION
#     install pin vs the npm latest dist-tag, and companion MCP server
#     liveness (missing/deprecated/yanked on npm or PyPI) — public
#     endpoints, no AWS creds needed
#   - Pre-commit hook revisions in .pre-commit-config.yaml compared
#     against the latest tag published upstream (GitHub API)
#   - CDK enum constants from gco/stacks/constants.py compared against the
#     installed aws-cdk-lib (LAMBDA_PYTHON_RUNTIME, LAMBDA_NODEJS_RUNTIME;
#     the Aurora engine is a plain version string checked against live RDS)
#   - Latest stable Python release from endoflife.date — public endpoint
#   - CI tooling the workflows install by hand: Trivy (TRIVY_VERSION),
#     actionlint (ACTIONLINT_VERSION), Helm and kubectl (HELM_VERSION /
#     KUBECTL_VERSION), kubeconform (KUBECONFORM_VERSION), Calico
#     (CALICO_VERSION), Metrics Server (METRICS_SERVER_VERSION), and kind + its
#     node image — public endpoints, no AWS creds
#   - Version consistency: ruff (pyproject / pre-commit / lint workflow),
#     Python and Node runtime pins, npm packageManager + CDK CLI pins, every
#     owned npm graph's lockfile/Dependabot coverage, and duplicated *_VERSION
#     workflow environment pins
#   - Base-image security epochs (APT_SECURITY_EPOCH / DNF_SECURITY_EPOCH)
#     older than SECURITY_EPOCH_STALE_DAYS
#   - Suppression expiries: .trivyignore / .pip-audit-ignore /
#     .npm-audit-ignore entries expiring within SUPPRESSION_EXPIRY_WARN_DAYS
#     (before the CI validator hard-fails)
#   - Lockfile freshness: direct deps in pyproject.toml missing from or pinned
#     differently in requirements-lock.txt
#
# Ports the `.dependency-scan-script` YAML anchor from the retired
# GitLab pipeline into a standalone shell script. Two behavior changes:
#
# 1. Workflow file input. The GitLab version grepped `.gitlab-ci.yml` for
#    CI image tags. This version scans every file under
#    `$WORKFLOWS_DIR` (default: `.github/workflows`).
# 2. Reporting. The GitLab version POSTed directly to the GitLab issues
#    API. This version writes a Markdown report to a file and emits
#    `has_drift=true|false`, `scan_complete=true|false`, and `report_path=…`
#    on $GITHUB_OUTPUT so the calling workflow can manage a rolling issue.
#
# Environment inputs:
#   WORKFLOWS_DIR  default: .github/workflows
#
# Outputs (via $GITHUB_OUTPUT):
#   has_drift     "true" when any version is outdated, else "false"
#   scan_complete "false" when any check fails or is explicitly skipped,
#                 else "true"
#   report_path   path to the Markdown report (only set when has_drift=true)
# =============================================================================
set -uo pipefail

WORKFLOWS_DIR="${WORKFLOWS_DIR:-.github/workflows}"
REPORT_FILE="$(mktemp -t dep-scan-XXXXXX.md 2>/dev/null || mktemp --suffix=.md)"
INCOMPLETE_REASONS_FILE="$(mktemp -t dep-scan-incomplete-XXXXXX 2>/dev/null || mktemp)"

# Persist incomplete reasons to a file because many checks run in pipeline
# subshells. A shell variable assignment made there would be lost, while an
# append to this channel survives and is consumed by the final completeness
# predicate. Messages go to stderr so they cannot corrupt command substitutions.
mark_scan_incomplete() {
  local reason="$1"
  printf '%s\n' "$reason" >> "$INCOMPLETE_REASONS_FILE"
  echo "  INCOMPLETE: $reason" >&2
}

join_scan_incomplete_reasons() {
  sort -u "$INCOMPLETE_REASONS_FILE" \
    | awk 'BEGIN { first = 1 } { if (!first) printf "; "; printf "%s", $0; first = 0 } END { print "" }'
}

# Source shared functions (also used by BATS tests)
SCAN_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/scripts/lib_dependency_scan.sh
source "${SCAN_SCRIPT_DIR}/lib_dependency_scan.sh"

# ---------------------------------------------------------------------------
# Report helpers
#
# Small Markdown emitters shared by every section of the drift report so the
# table formatting lives in one place — previously each section hand-rolled
# its own ``| … |`` header + separator, which drifted as sections were added.
# ``emit_md_table`` turns a pipe-delimited results file into a GitHub table;
# ``md_anchor`` builds the in-page heading slug the top-of-report summary
# links to.
#
# Thresholds for the recurring-hygiene checks. Tunable in one place:
#   SUPPRESSION_EXPIRY_WARN_DAYS  surface .trivyignore / .pip-audit-ignore /
#                                 .npm-audit-ignore entries expiring within
#                                 this many days
#                                 (the CI validator still hard-fails on the
#                                 day itself — this is the early warning).
#   SECURITY_EPOCH_STALE_DAYS     flag a Dockerfile APT/DNF security epoch
#                                 older than this many days.
# ---------------------------------------------------------------------------
SUPPRESSION_EXPIRY_WARN_DAYS="${SUPPRESSION_EXPIRY_WARN_DAYS:-30}"
SECURITY_EPOCH_STALE_DAYS="${SECURITY_EPOCH_STALE_DAYS:-45}"

# md_anchor <title> — GitHub heading slug (lowercase, punctuation dropped,
# spaces → hyphens). Close enough to GitHub's own algorithm for the
# summary-table links to resolve.
md_anchor() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9 -]//g; s/ /-/g'
}

# emit_md_table <header> <results-file> [wrap]
#
#   <header>       pipe-delimited column labels, e.g. "Package|Current|Latest"
#   <results-file> file of pipe-delimited rows with the same column count
#   [wrap]         when "code", every non-empty cell is wrapped in backticks
#
# Prints a GitHub-flavoured Markdown table. With no wrap, cells are emitted
# verbatim, so a caller that wants a link in a cell just writes the
# ``[text](url)`` markdown straight into the results file.
emit_md_table() {
  local header="$1" file="$2" wrap="${3:-}"
  local -a cells cols
  IFS='|' read -r -a cells <<< "$header"
  local head="|" sep="|" c
  for c in "${cells[@]}"; do
    head+=" ${c} |"
    sep+="---|"
  done
  printf '%s\n%s\n' "$head" "$sep"
  while IFS='|' read -r -a cols; do
    [ "${#cols[@]}" -eq 0 ] && continue
    local row="|" cell
    for cell in "${cols[@]}"; do
      if [ "$wrap" = "code" ] && [ -n "$cell" ]; then
        row+=" \`${cell}\` |"
      else
        row+=" ${cell} |"
      fi
    done
    printf '%s\n' "$row"
  done < "$file"
}

# days_until <YYYY-MM-DD> — integer days from today to the given date
# (negative when the date is in the past). Empty output on a malformed date.
days_until() {
  python3 -c "
import datetime, sys
try:
    d = datetime.date.fromisoformat(sys.argv[1])
except Exception:
    sys.exit(0)
print((d - datetime.date.today()).days)
" "$1" 2>/dev/null
}

# days_since <YYYY-MM-DD> — integer days from the given date to today
# (negative when the date is in the future). Empty output on a malformed date.
days_since() {
  python3 -c "
import datetime, sys
try:
    d = datetime.date.fromisoformat(sys.argv[1])
except Exception:
    sys.exit(0)
print((datetime.date.today() - d).days)
" "$1" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Python packages
#
# We run ``pip list --outdated`` on the installed interpreter, but
# filter the JSON result down to packages we pin *directly* in
# ``pyproject.toml::[project.dependencies]`` or the
# ``[project.optional-dependencies]`` groups. Every other outdated
# entry is a transitive dependency — its version is controlled by
# something we pin (``jsii``, ``aws-cdk-lib``, ``fastmcp``,
# ``botocore``, …) and bumping it ourselves either does nothing or
# breaks the resolver. Leaving those entries in the monthly scan
# report was creating noise: the operator had no action to take on
# them beyond "wait for upstream". Filter them out so the report
# only lists packages we can act on.
#
# The ``[build-system]`` requires pins (the exact-pinned build backend)
# are appended to the same surface below via a direct PyPI lookup — pip
# resolves them inside build isolation, so ``pip list`` never sees them.
# ---------------------------------------------------------------------------
echo "=== Checking for outdated Python dependencies ==="

# Install the project with EVERY optional-dependency group, not just the
# base dependencies: ``pip list --outdated`` can only report packages that
# are installed, so a base-only install silently dropped pins that live
# exclusively in an extras group (``aws-cdk-lib`` in ``cdk``, ``playwright``
# in ``diagrams``, ``mypy`` in ``typecheck``, ...) even though
# ``extract_direct_python_deps`` already includes them in the direct-pin
# filter below. Groups are enumerated from pyproject.toml so a new extras
# group joins the surface automatically; if enumeration fails, fall back to
# the old base-only install rather than dropping the report section.
PYTHON_EXTRAS="$(extract_python_extras pyproject.toml | paste -sd, -)"
if [ -z "$PYTHON_EXTRAS" ]; then
  mark_scan_incomplete "Could not enumerate optional dependency groups from pyproject.toml."
fi
if [ -n "$PYTHON_EXTRAS" ]; then
  echo "Installing with extras: [${PYTHON_EXTRAS}]"
  if ! pip install -e ".[${PYTHON_EXTRAS}]" --quiet --root-user-action=ignore; then
    mark_scan_incomplete "Python dependency installation failed."
  fi
else
  if ! pip install -e . --quiet --root-user-action=ignore; then
    mark_scan_incomplete "Python dependency installation failed and optional groups could not be enumerated."
  fi
fi
if ! OUTDATED_RAW="$(pip list --outdated --format=json)"; then
  mark_scan_incomplete "pip list --outdated failed."
  OUTDATED_RAW="[]"
elif ! printf '%s' "$OUTDATED_RAW" | jq -e 'type == "array"' >/dev/null 2>&1; then
  mark_scan_incomplete "pip list --outdated returned malformed JSON."
  OUTDATED_RAW="[]"
fi

# Build a newline-separated list of PEP-503-normalised direct-dep names.
# An empty list disables the filter so we never silently hide drift when
# the TOML parse breaks. In practice the file always parses — we just
# can't risk a dropped report section.
DIRECT_DEPS="$(extract_direct_python_deps pyproject.toml)"
if [ -z "$DIRECT_DEPS" ]; then
  mark_scan_incomplete "Could not parse direct Python dependencies from pyproject.toml."
fi

if ! OUTDATED="$(printf '%s' "$OUTDATED_RAW" | python3 -c "
import json, re, sys
raw = sys.stdin.read()
direct = set(
    line.strip() for line in '''$DIRECT_DEPS'''.splitlines() if line.strip()
)
try:
    data = json.loads(raw) if raw else []
except json.JSONDecodeError:
    raise SystemExit(1)
if direct:
    data = [
        e for e in data
        if re.sub(r'[-_.]+', '-', e.get('name', '')).lower() in direct
    ]
print(json.dumps(data))
")"; then
  mark_scan_incomplete "Could not parse or filter pip's outdated-package response."
  OUTDATED="[]"
fi

# Build-backend pins ([build-system] requires) are Python dependencies
# too, but they are invisible to ``pip list --outdated``: pip resolves
# them inside build isolation, not in this venv (a Python 3.14 venv does
# not even ship setuptools). Compare each exact pin against its PyPI
# latest and report it through the same Python-packages surface as every
# other pyproject pin. Non-exact entries are skipped here — the
# version-consistency section flags those as a policy finding.
BUILD_SYSTEM_PINS="$(extract_build_system_pins pyproject.toml)"
if [ -z "$BUILD_SYSTEM_PINS" ]; then
  mark_scan_incomplete "Could not parse [build-system] requirements from pyproject.toml."
else
  while IFS='|' read -r bs_name bs_version bs_raw; do
    # Only exact pins are compared; non-exact entries surface through
    # the version-consistency policy check instead.
    if [ -z "$bs_name" ] || [ -z "$bs_version" ]; then
      continue
    fi
    if ! bs_latest="$(curl -fsSL --max-time 15 \
      "https://pypi.org/pypi/${bs_name}/json" 2>/dev/null \
      | jq -r '.info.version // empty' 2>/dev/null)" || [ -z "$bs_latest" ]; then
      mark_scan_incomplete "PyPI lookup failed or returned an invalid version for build dependency ${bs_name}."
      continue
    fi
    if [ "$(compare_semver "$bs_version" "$bs_latest")" = "newer" ]; then
      OUTDATED="$(echo "$OUTDATED" | jq \
        --arg name "$bs_name" --arg cur "$bs_version" --arg latest "$bs_latest" \
        '. + [{"name": $name, "version": $cur, "latest_version": $latest}]')"
    fi
  done <<< "$BUILD_SYSTEM_PINS"
fi

PYTHON_COUNT="$(echo "$OUTDATED" | jq 'length')"
if [ "$PYTHON_COUNT" -eq 0 ]; then
  echo "All Python dependencies are up to date."
  PYTHON_OUTDATED=""
else
  echo "Found $PYTHON_COUNT outdated Python package(s) (direct dependencies only — transitive bumps are upstream's job)"
  echo "$OUTDATED" | jq -r '.[] | "  - \(.name): \(.version) -> \(.latest_version)"'
  PYTHON_OUTDATED="$OUTDATED"
fi

# ---------------------------------------------------------------------------
# npm packages (every repository-owned graph)
# ---------------------------------------------------------------------------
# Direct npm dependencies never had a drift surface of their own: aws-cdk and
# markdownlint-cli2 only leaked into the report indirectly (via the
# Dockerfile.dev ARG and the pre-commit hook rev), and the
# inference-streaming-proxy's @aws-sdk clients were reported nowhere at all.
# This walks the same repository-owned graphs the npm package-management
# check validates and compares each exact direct pin against the registry's
# ``latest`` dist-tag — the npm analogue of the Python Packages surface.
echo ""
echo "=== Checking for outdated npm packages ==="

NPM_RESULTS="$(mktemp)"
NPM_COUNT=0

NPM_PACKAGE_DIRS="$(list_npm_package_dirs .)"
if [ -z "$NPM_PACKAGE_DIRS" ]; then
  mark_scan_incomplete "Could not enumerate repository-owned npm package manifests."
fi
while IFS= read -r package_dir; do
  [ -n "$package_dir" ] || continue
  manifest="${package_dir}/package.json"
  package_pins="$(extract_npm_direct_pins "$manifest")"
  if [ -z "$package_pins" ]; then
    mark_scan_incomplete "Could not parse exact npm dependency pins from ${manifest}."
    continue
  fi
  while IFS='|' read -r pkg_name pkg_version; do
    [ -n "$pkg_name" ] || continue
    # Scoped names carry a '/', which must be encoded in the registry URL.
    encoded_name="$(printf '%s' "$pkg_name" | sed 's|/|%2F|g')"
    if ! pkg_latest="$(curl -fsSL --max-time 15 \
      "https://registry.npmjs.org/${encoded_name}/latest" 2>/dev/null \
      | jq -r '.version // empty' 2>/dev/null)" || [ -z "$pkg_latest" ]; then
      mark_scan_incomplete "npm registry lookup failed or returned an invalid version for ${pkg_name}."
      continue
    fi
    if [ "$(compare_semver "$pkg_version" "$pkg_latest")" = "newer" ]; then
      echo "  - ${package_dir}: ${pkg_name} ${pkg_version} -> ${pkg_latest}"
      echo "${package_dir}|${pkg_name}|${pkg_version}|${pkg_latest}" >> "$NPM_RESULTS"
    fi
  done <<< "$package_pins"
done <<< "$NPM_PACKAGE_DIRS"

NPM_COUNT="$(wc -l < "$NPM_RESULTS" | tr -d ' ')"
if [ "$NPM_COUNT" -eq 0 ]; then
  echo "All npm direct dependencies are up to date."
else
  echo "Found $NPM_COUNT outdated npm package pin(s) across the owned graphs"
fi

# ---------------------------------------------------------------------------
# Docker image tags
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking for outdated Docker images ==="

DOCKER_RESULTS="$(mktemp)"
ALL_IMAGES="$(mktemp)"

check_image() {
  local image="$1"
  local current_tag="$2"

  # Only handle semver tags
  if ! is_semver_tag "$current_tag"; then
    return
  fi
  # Skip images we build in this project
  if is_project_image "$image"; then
    return
  fi

  local parsed registry repo
  parsed="$(parse_image_registry "$image")"
  registry="$(echo "$parsed" | cut -d'|' -f1)"
  repo="$(echo "$parsed" | cut -d'|' -f2)"

  # Fetch and filter separately. A registry/network failure marks the scan
  # incomplete, while "the registry answered and nothing newer exists in
  # this variant family" is an up-to-date pin. The previous single pipeline
  # conflated the two under pipefail: the strict bare-semver grep matched
  # nothing for suffix-tagged repositories (…-py3, …-cuda…, …-ubuntu…), so
  # they reported "tag lookup failed" every month even though the registry
  # was fine.
  local raw_tags=""
  if ! raw_tags="$(skopeo list-tags --retry-times 3 "docker://${registry}/${repo}" 2>/dev/null \
    | jq -r '.Tags[]' 2>/dev/null)" || [ -z "$raw_tags" ]; then
    mark_scan_incomplete "Container registry tag lookup failed for ${registry}/${repo}."
    return
  fi

  local latest_tag
  latest_tag="$(printf '%s\n' "$raw_tags" | newer_same_variant_tag "$current_tag")" || latest_tag=""

  if [ -n "$latest_tag" ]; then
    echo "  - ${image}:${current_tag} -> ${latest_tag}"
    echo "${image}|${current_tag}|${latest_tag}" >> "$DOCKER_RESULTS"
    return
  fi

  # No newer family member. If the pinned tag itself is no longer listed,
  # the pin points at something the registry stopped advertising (renamed
  # variant scheme, withdrawn tag) — that deserves eyes, not silence.
  # tag_listed reads the list from an argument, not a printf pipe: under
  # pipefail, grep -q's early exit gave printf SIGPIPE on large tag lists
  # and inverted "tag present" into a false INCOMPLETE (2026-09 scan).
  if ! tag_listed "$current_tag" "$raw_tags"; then
    mark_scan_incomplete "Pinned tag ${current_tag} is no longer listed by ${registry}/${repo}."
  fi
}

# Collect image:tag pairs from workflow files (bare `image:` references in
# container specs and `uses: …@sha` are handled by Dependabot; here we look
# for free-form image references in run steps).
echo "Checking workflow files in $WORKFLOWS_DIR..."
if [ -d "$WORKFLOWS_DIR" ]; then
  grep -rhoE "image: [a-zA-Z0-9_./-]+:[a-zA-Z0-9._-]+" "$WORKFLOWS_DIR" 2>/dev/null \
    | sed 's/image: //' >> "$ALL_IMAGES" || true
  grep -rhoE "[a-zA-Z0-9_./-]+:[a-zA-Z0-9._-]+" "$WORKFLOWS_DIR" 2>/dev/null \
    | grep -E '^(alpine|hadolint|koalaman|semgrep|bridgecrew|checkmarx|trufflesecurity|zricethezav|aquasec|bats|python):' \
    | sed 's/[[:space:]]*$//' >> "$ALL_IMAGES" || true
fi

echo "Checking K8s manifest images..."
grep -rhoE "image: [a-zA-Z0-9_./-]+:[a-zA-Z0-9._-]+" lambda/kubectl-applier-simple/manifests/ 2>/dev/null \
  | grep -v '{{' | sed 's/image: //' >> "$ALL_IMAGES" || true

echo "Checking example manifest images..."
grep -rhoE "image: [a-zA-Z0-9_./-]+:[a-zA-Z0-9._-]+" examples/ 2>/dev/null \
  | sed 's/image: //' >> "$ALL_IMAGES" || true

echo "Checking local live-validation manifest images..."
grep -rhoE "image: [a-zA-Z0-9_./-]+:[a-zA-Z0-9._-]+" scripts/live_release_validation/manifests/ 2>/dev/null \
  | sed 's/image: //' >> "$ALL_IMAGES" || true

echo "Checking Helm chart value images..."
CHART_VALUE_IMAGES=""
if ! CHART_VALUE_IMAGES="$(python3 - <<'PY'
import yaml
with open('lambda/helm-installer/charts.yaml') as f:
    data = yaml.safe_load(f)


def find_images(d):
    if isinstance(d, dict):
        repo = d.get('repository', '')
        tag = d.get('tag', '')
        if repo and tag and '/' in repo:
            print(f'{repo}:{tag}')
        for v in d.values():
            find_images(v)
    elif isinstance(d, list):
        for item in d:
            find_images(item)


for name, cfg in (data or {}).get('charts', {}).items():
    find_images(cfg.get('values', {}))
PY
)"; then
  mark_scan_incomplete "Could not parse Helm chart value images."
elif [ -n "$CHART_VALUE_IMAGES" ]; then
  printf '%s\n' "$CHART_VALUE_IMAGES" >> "$ALL_IMAGES"
fi

# Mooncake default image — pinned as a Python constant in cli/images.py
# (_DISAGGREGATED_DEFAULT_IMAGE), so it is invisible to Dependabot (docker
# ecosystem) and to the manifest/workflow sweeps above. Add it here so a newer
# upstream vLLM release shows up in the monthly drift report — the cue to
# validate and bump the pin.
echo "Checking Mooncake default image (cli/images.py)..."
MOONCAKE_IMAGE="$(extract_mooncake_default_image cli/images.py)"
if [ -n "$MOONCAKE_IMAGE" ]; then
  echo "$MOONCAKE_IMAGE" >> "$ALL_IMAGES"
else
  mark_scan_incomplete "Could not parse the Mooncake default image from cli/images.py."
fi

# The model-sync init container uses an official AWS CLI image pinned by both
# version and manifest-list digest. Strip only the digest for the registry tag
# lookup; the offline provenance contract separately requires the digest.
echo "Checking AWS CLI runtime image (gco/services/inference_monitor.py)..."
AWS_CLI_RUNTIME_IMAGE="$(extract_python_string_constant \
  AWS_CLI_IMAGE gco/services/inference_monitor.py)"
# check_pinned_digest <repo:tag@sha256:digest> <origin label>
#
# Shared digest-freshness check for every digest-pinned image the repository
# commits: verify the tag's currently published manifest-list digest still
# equals the committed one. A moved digest is a drift row (the tag was
# re-pushed upstream — the pin is stale); an unreachable registry or an
# implausible response marks the scan incomplete.
check_pinned_digest() {
  local pinned_ref="$1" origin="$2" parts repository tag committed published
  if ! parts="$(split_pinned_image_ref "$pinned_ref")"; then
    mark_scan_incomplete "Could not parse an immutable image reference from ${origin}."
    return
  fi
  repository="$(echo "$parts" | cut -d'|' -f1)"
  tag="$(echo "$parts" | cut -d'|' -f2)"
  committed="$(echo "$parts" | cut -d'|' -f3)"
  printf '%s:%s\n' "$repository" "$tag" >> "$ALL_IMAGES"

  if ! published="$(published_manifest_digest "${repository}:${tag}")"; then
    mark_scan_incomplete "Container manifest lookup failed for ${repository}:${tag}."
    return
  fi
  if [ "$committed" != "$published" ]; then
    echo "  - ${repository}:${tag}: committed digest does not match the tag (${origin})"
    echo "${repository}|${tag}@${committed}|${tag}@${published}" >> "$DOCKER_RESULTS"
  fi
}

if [[ "$AWS_CLI_RUNTIME_IMAGE" =~ ^[^@]+:[^@]+@sha256:[0-9a-f]{64}$ ]]; then
  check_pinned_digest "$AWS_CLI_RUNTIME_IMAGE" "gco/services/inference_monitor.py"
else
  mark_scan_incomplete "Could not parse an immutable AWS_CLI_IMAGE from gco/services/inference_monitor.py."
fi

# The live-validation smoke manifests pin every image by tag AND manifest-list
# digest (tests/test_live_release_validation.py enforces the shape). The tag
# half already rides the normal drift check above; this pass keeps the digest
# half honest too, so an upstream same-tag re-push shows up as drift instead
# of silently diverging from what a validation run would actually pull.
echo "Checking live-validation smoke image digests (scripts/live_release_validation/manifests)..."
SMOKE_PINNED_REFS="$(grep -rhoE \
  "image: [a-zA-Z0-9_./-]+:[a-zA-Z0-9._-]+@sha256:[0-9a-f]{64}" \
  scripts/live_release_validation/manifests/ 2>/dev/null | sed 's/^image: //' | sort -u)"
if [ -z "$SMOKE_PINNED_REFS" ]; then
  mark_scan_incomplete "No digest-pinned smoke images found under scripts/live_release_validation/manifests/."
else
  while read -r pinned_ref; do
    [ -z "$pinned_ref" ] && continue
    check_pinned_digest "$pinned_ref" "live-validation smoke manifest"
  done <<< "$SMOKE_PINNED_REFS"
fi

sort -u "$ALL_IMAGES" | while read -r img; do
  [ -z "$img" ] && continue
  image="$(echo "$img" | cut -d':' -f1)"
  tag="$(echo "$img" | cut -d':' -f2)"
  check_image "$image" "$tag"
done
rm -f "$ALL_IMAGES"

DOCKER_COUNT="$(wc -l < "$DOCKER_RESULTS" | tr -d ' ')"
[ -z "$DOCKER_COUNT" ] && DOCKER_COUNT=0

# ---------------------------------------------------------------------------
# Helm chart versions
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking Helm chart versions ==="

HELM_RESULTS="$(mktemp)"
CHARTS_FILE="lambda/helm-installer/charts.yaml"

if [ -f "$CHARTS_FILE" ]; then
  CHART_ENTRIES="$(extract_helm_charts "$CHARTS_FILE")"
  if [ -z "$CHART_ENTRIES" ]; then
    mark_scan_incomplete "Could not parse Helm chart pins from ${CHARTS_FILE}."
  fi
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    chart_name="$(echo "$entry" | jq -r '.name // empty')"
    repo_url="$(echo "$entry" | jq -r '.repo_url // empty')"
    chart="$(echo "$entry" | jq -r '.chart // empty')"
    current="$(echo "$entry" | jq -r '.version // empty')"
    use_oci="$(echo "$entry" | jq -r '.use_oci // false')"
    if [ -z "$chart_name" ] || [ -z "$repo_url" ] || [ -z "$chart" ] || [ -z "$current" ]; then
      mark_scan_incomplete "Helm chart parser emitted an incomplete record from ${CHARTS_FILE}."
      continue
    fi

    latest=""
    if [ "$use_oci" = "true" ]; then
      if ! latest="$(helm show chart "${repo_url}/${chart}" 2>/dev/null | grep '^version:' | awk '{print $2}')" \
         || [ -z "$latest" ]; then
        mark_scan_incomplete "Helm OCI lookup failed for ${repo_url}/${chart}."
        continue
      fi
    else
      if ! helm repo add "$chart_name" "$repo_url" --force-update > /dev/null 2>&1; then
        mark_scan_incomplete "Helm repository refresh failed for ${chart_name} (${repo_url})."
        continue
      fi
      if ! latest="$(helm search repo "${chart_name}/${chart}" --output json 2>/dev/null \
        | jq -r '.[0].version // empty')" || [ -z "$latest" ]; then
        mark_scan_incomplete "Helm chart lookup failed for ${chart_name}/${chart}."
        continue
      fi
    fi

    if [ "$current" != "$latest" ]; then
      current_stripped="${current#v}"
      latest_stripped="${latest#v}"
      if [ "$current_stripped" != "$latest_stripped" ]; then
        echo "  - ${chart_name} (${chart}): ${current} -> ${latest}"
        echo "${chart_name}|${chart}|${current}|${latest}" >> "$HELM_RESULTS"
      fi
    fi
  done <<< "$CHART_ENTRIES"
else
  mark_scan_incomplete "${CHARTS_FILE} is missing."
fi

HELM_COUNT="$(wc -l < "$HELM_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$HELM_COUNT" ] && HELM_COUNT=0

# ---------------------------------------------------------------------------
# EKS add-on versions (best-effort — requires AWS credentials)
#
# Pre-flight: probe for usable AWS credentials. If `sts get-caller-identity`
# fails the scan is skipped entirely and a one-line note goes into both the
# console log and the Markdown report — this is more honest than silently
# dropping the section. Wire AWS creds through OIDC (see the deps-scan
# section in .github/CI.md) to enable the check.
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking EKS add-on versions ==="

ADDON_RESULTS="$(mktemp)"
ADDON_SKIP_REASON=""
K8S_VERSION="$(extract_k8s_version "")"

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  ADDON_SKIP_REASON="No AWS credentials available (scan needs eks:DescribeAddonVersions). Configure OIDC to enable."
  echo "  $ADDON_SKIP_REASON"
else
  EKS_ADDONS="$(extract_eks_addons "gco/stacks/regional_stack.py")"
  if [ -z "$EKS_ADDONS" ]; then
    ADDON_SKIP_REASON="Could not read EKS add-on pins from gco/stacks/regional_stack.py."
    echo "  $ADDON_SKIP_REASON"
  else
    while IFS='|' read -r addon_name current_version; do
      [ -z "$addon_name" ] && continue
      latest="$(aws eks describe-addon-versions \
        --addon-name "$addon_name" \
        --kubernetes-version "$K8S_VERSION" \
        --query 'addons[0].addonVersions[0].addonVersion' \
        --output text 2>/dev/null)" || latest=""

      if ! [[ "$latest" =~ ^v[0-9]+\.[0-9]+\.[0-9]+-eksbuild\.[0-9]+$ ]]; then
        ADDON_SKIP_REASON="EKS add-on lookup failed or returned an invalid version for ${addon_name}."
        echo "  $ADDON_SKIP_REASON"
        break
      fi
      if [ "$current_version" != "$latest" ]; then
        echo "  - ${addon_name}: ${current_version} -> ${latest}"
        echo "${addon_name}|${current_version}|${latest}" >> "$ADDON_RESULTS"
      fi
    done <<< "$EKS_ADDONS"
  fi
fi

ADDON_COUNT="$(wc -l < "$ADDON_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$ADDON_COUNT" ] && ADDON_COUNT=0

# ---------------------------------------------------------------------------
# EKS Kubernetes version (best-effort — requires AWS credentials)
#
# Compares ``kubernetes_version`` in cdk.json against the latest minor
# available from ``aws eks describe-cluster-versions`` (filtered to
# ``STANDARD_SUPPORT``). If a newer minor is available, we also report
# when standard support for the currently-pinned minor ends so the
# upgrade urgency is visible in the PR.
#
# IAM action: ``eks:DescribeClusterVersions``. Same credential preflight
# as the EKS add-on / Aurora / EMR checks.
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking EKS Kubernetes version ==="

EKS_K8S_RESULTS="$(mktemp)"
EKS_K8S_SKIP_REASON=""

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  EKS_K8S_SKIP_REASON="No AWS credentials available (scan needs eks:DescribeClusterVersions). Configure OIDC to enable."
  echo "  $EKS_K8S_SKIP_REASON"
else
  # ``--version-status STANDARD_SUPPORT`` returns every minor still in
  # standard support — we don't want to flag the extended-support
  # lifecycle as "newer." It must be the only selector: the API rejects
  # combining it with ``--include-all`` ("Only one of the defaultOnly,
  # clusterVersions, includeAll or status request parameters is accepted
  # at a time"), which is exactly how this check silently broke once.
  # stderr is captured into the skip reason so the next API-shape change
  # is diagnosable from the report instead of reading as a generic skip.
  EKS_K8S_ERR_FILE="$(mktemp)"
  CLUSTER_VERSIONS_JSON="$(aws eks describe-cluster-versions \
    --version-status STANDARD_SUPPORT \
    --output json 2>"$EKS_K8S_ERR_FILE")" || CLUSTER_VERSIONS_JSON=""

  if [ -z "$CLUSTER_VERSIONS_JSON" ]; then
    EKS_K8S_ERR="$(head -n 1 "$EKS_K8S_ERR_FILE" 2>/dev/null | tr -d '\r')"
    EKS_K8S_SKIP_REASON="EKS Kubernetes version lookup failed${EKS_K8S_ERR:+: ${EKS_K8S_ERR}}"
    [ -z "$EKS_K8S_ERR" ] && EKS_K8S_SKIP_REASON="EKS Kubernetes version lookup returned an empty response."
    echo "  $EKS_K8S_SKIP_REASON"
    rm -f "$EKS_K8S_ERR_FILE"
  else
    rm -f "$EKS_K8S_ERR_FILE"
    # Max of ``clusterVersion`` across all rows is the newest standard-
    # support minor. We use Python for a proper numeric sort so 1.10
    # beats 1.9 (sort -V already does this, but Python keeps the data
    # wrangling in one place).
    LATEST_K8S="$(echo "$CLUSTER_VERSIONS_JSON" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
versions = sorted(
    {row["clusterVersion"] for row in data.get("clusterVersions", [])},
    key=lambda v: tuple(int(p) for p in v.split(".")),
)
print(versions[-1] if versions else "")
' 2>/dev/null)" || LATEST_K8S=""

    if [ -z "$LATEST_K8S" ]; then
      EKS_K8S_SKIP_REASON="EKS Kubernetes version response contained no parseable standard-support versions."
      echo "  $EKS_K8S_SKIP_REASON"
    else
      CURRENT_K8S="$(extract_k8s_version "cdk.json")"

      if [ "$CURRENT_K8S" != "$LATEST_K8S" ] \
         && [ "$(compare_semver "$CURRENT_K8S" "$LATEST_K8S")" = "newer" ]; then
        # Grab the standard-support end date for the currently-pinned
        # minor. Blank when EKS hasn't published one yet (brand-new release).
        EOS_DATE="$(echo "$CLUSTER_VERSIONS_JSON" | python3 -c "
import json, sys
cv = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for row in data.get('clusterVersions', []):
    if row.get('clusterVersion') == cv:
        ts = row.get('endOfStandardSupportDate', '')
        # Strip time-of-day; the date is what the report cares about.
        print(str(ts).split('T', 1)[0].split(' ', 1)[0])
        break
" "$CURRENT_K8S" 2>/dev/null)" || EOS_DATE=""

        echo "  - kubernetes_version: ${CURRENT_K8S} -> ${LATEST_K8S} (std support ends ${EOS_DATE:-unknown})"
        echo "kubernetes_version|${CURRENT_K8S}|${LATEST_K8S}|${EOS_DATE:-unknown}" >> "$EKS_K8S_RESULTS"
      fi
    fi
  fi
fi

EKS_K8S_COUNT="$(wc -l < "$EKS_K8S_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$EKS_K8S_COUNT" ] && EKS_K8S_COUNT=0

# ---------------------------------------------------------------------------
# Aurora PostgreSQL engine versions (best-effort — requires AWS credentials)
#
# Checks whether the Aurora PostgreSQL engine version pinned in
# regional_stack.py has a newer minor or major release available.
# Uses the same credential gate as the EKS add-on check above.
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking Aurora PostgreSQL engine versions ==="

AURORA_RESULTS="$(mktemp)"
AURORA_SKIP_REASON=""

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  AURORA_SKIP_REASON="No AWS credentials available (scan needs rds:DescribeDBEngineVersions). Configure OIDC to enable."
  echo "  $AURORA_SKIP_REASON"
else
  # The pinned Aurora PostgreSQL version (AURORA_POSTGRES_VERSION in
  # gco/stacks/constants.py — a plain version string applied through
  # AuroraPostgresEngineVersion.of(), so no CDK enum is involved).
  AURORA_VERSIONS="$(extract_aurora_versions "gco/stacks/regional_stack.py")"
  if [ -z "$AURORA_VERSIONS" ]; then
    AURORA_SKIP_REASON="Could not read AURORA_POSTGRES_VERSION from gco/stacks/constants.py."
    echo "  $AURORA_SKIP_REASON"
  else
    while read -r current_ver; do
      [ -z "$current_ver" ] && continue
      major="$(echo "$current_ver" | cut -d. -f1)"

      # Query the latest available engine version for this major line.
      latest="$(aws rds describe-db-engine-versions \
        --engine aurora-postgresql \
        --query "DBEngineVersions[?starts_with(EngineVersion, '${major}.')].EngineVersion" \
        --output text 2>/dev/null \
        | tr '\t' '\n' | sort -V | tail -1)" || latest=""

      if ! [[ "$latest" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
        AURORA_SKIP_REASON="Aurora PostgreSQL engine lookup failed or returned an invalid version for major ${major}."
        echo "  $AURORA_SKIP_REASON"
        break
      fi
      if [ "$current_ver" != "$latest" ]; then
        echo "  - aurora-postgresql: ${current_ver} -> ${latest}"
        echo "aurora-postgresql|${current_ver}|${latest}" >> "$AURORA_RESULTS"
      fi
    done <<< "$AURORA_VERSIONS"
  fi
fi

AURORA_COUNT="$(wc -l < "$AURORA_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$AURORA_COUNT" ] && AURORA_COUNT=0

# ---------------------------------------------------------------------------
# EMR Serverless release labels (best-effort — requires AWS credentials)
#
# Checks whether the EMR Serverless release label pinned in
# gco/stacks/constants.py has a newer release available. Uses the same
# credential gate as the EKS add-on / Aurora checks above.
#
# AWS CLI note: the `list-release-labels` subcommand lives on the classic
# `aws emr` service, not on `aws emr-serverless`. Classic EMR and EMR
# Serverless share the same release-label namespace (e.g. emr-7.13.0),
# so calling the classic service returns the labels usable by Serverless
# applications. The IAM action is ``elasticmapreduce:ListReleaseLabels``
# (which is what the OIDC policy grants) and is shared between the two
# services — the CLI routing is just a surface-level difference.
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking EMR Serverless release labels ==="

EMR_RESULTS="$(mktemp)"
EMR_SKIP_REASON=""

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  EMR_SKIP_REASON="No AWS credentials available (scan needs elasticmapreduce:ListReleaseLabels). Configure OIDC to enable."
  echo "  $EMR_SKIP_REASON"
else
  EMR_VERSIONS="$(extract_emr_versions "gco/stacks/constants.py")"
  if [ -z "$EMR_VERSIONS" ]; then
    EMR_SKIP_REASON="Could not read the EMR Serverless release-label pin from gco/stacks/constants.py."
    echo "  $EMR_SKIP_REASON"
  else
    while read -r current_label; do
      [ -z "$current_label" ] && continue
      # current_label looks like "emr-7.13.0". Filter labels to ones that
      # start with "emr-<major>." and take the latest by semver-ish sort.
      # Skip preview/nightly tags (``-preview``, ``-beta``, ``-rc*``). The
      # latest release label is what we compare against.
      major="$(echo "$current_label" | sed -E 's/^emr-([0-9]+)\..*/\1/')"
      release_labels="$(aws emr list-release-labels \
        --region us-east-1 \
        --query 'ReleaseLabels[]' --output text 2>/dev/null)" || release_labels=""

      if [ -z "$release_labels" ] || [ "$release_labels" = "None" ]; then
        EMR_SKIP_REASON="EMR release-label lookup failed or returned an empty response."
        echo "  $EMR_SKIP_REASON"
        break
      fi

      latest="$(echo "$release_labels" \
        | tr '\t' '\n' \
        | grep -E "^emr-${major}\.[0-9]+\.[0-9]+$" \
        | sort -V | tail -1)" || true

      # Also check whether a newer major release line exists.
      latest_any="$(echo "$release_labels" \
        | tr '\t' '\n' \
        | grep -E "^emr-[0-9]+\.[0-9]+\.[0-9]+$" \
        | sort -V | tail -1)" || true

      if [ -z "$latest_any" ]; then
        EMR_SKIP_REASON="EMR release-label response contained no parseable stable releases."
        echo "  $EMR_SKIP_REASON"
        break
      fi
      if [ -n "$latest" ] && [ "$current_label" != "$latest" ]; then
        echo "  - emr-serverless: ${current_label} -> ${latest}"
        echo "emr-serverless|${current_label}|${latest}" >> "$EMR_RESULTS"
      elif [ "$current_label" != "$latest_any" ] \
           && [ "$(compare_semver "${current_label#emr-}" "${latest_any#emr-}")" = "newer" ]; then
        # Same minor — no new release in our pinned major — but a new
        # major exists.
        echo "  - emr-serverless: ${current_label} -> ${latest_any} (new major available)"
        echo "emr-serverless|${current_label}|${latest_any}" >> "$EMR_RESULTS"
      fi
    done <<< "$EMR_VERSIONS"
  fi
fi

EMR_COUNT="$(wc -l < "$EMR_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$EMR_COUNT" ] && EMR_COUNT=0

# ---------------------------------------------------------------------------
# Bedrock default model (best-effort — requires AWS credentials)
#
# Compares each configured Bedrock model default in cdk.json —
# context.bedrock.mission_default_model_id (Mission sampling),
# context.bedrock.capacity_advisor_default_model_id (the capacity
# advisor), context.bedrock.claude_code_default_model_id (the session
# model gco autopilot hands to Claude Code), and
# context.bedrock.embedding_model_id (Mission memory's text-embedding
# model) — against the newest release in the SAME model family. The three
# generation keys compare against system-defined inference profiles
# (aws bedrock list-inference-profiles); the embedding key is a plain
# foundation model, so it compares against
# aws bedrock list-foundation-models --by-output-modality EMBEDDING.
# Every consumer resolves its key through gco.bedrock, so the scan and
# the runtime paths cannot silently diverge; the keys are independent
# knobs and each gets its own drift row.
#
# Same-family scoping (see bedrock_model_family) means we only flag a newer
# release of the same model line (e.g. a newer global Amazon Nova Lite) — never a
# different tier or provider, since switching those is a human decision,
# not drift. When a newer release is reported, update the flagged key in
# cdk.json; for the Mission key also re-capture the scaffold
# fixture with scripts/capture_scaffold_fixtures.py. For the embedding
# key, remember stored vectors are only comparable to vectors from the
# same model: adopting a newer embedding model means re-embedding or
# segregating existing Mission-memory data, not just bumping the pin.
#
# IAM actions: bedrock:ListInferenceProfiles and
# bedrock:ListFoundationModels. Pinned to us-east-1 (the advisor +
# Mission sampling + Mission memory default region) regardless of the
# workflow's configured region. Same credential preflight as the EKS
# add-on / Aurora / EMR checks.
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking Bedrock default model ==="

BEDROCK_MODEL_RESULTS="$(mktemp)"
BEDROCK_MODEL_SKIP_REASON=""

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  BEDROCK_MODEL_SKIP_REASON="No AWS credentials available (scan needs bedrock:ListInferenceProfiles). Configure OIDC to enable."
  echo "  $BEDROCK_MODEL_SKIP_REASON"
else
  for BEDROCK_MODEL_LEAF in mission_default_model_id capacity_advisor_default_model_id claude_code_default_model_id embedding_model_id; do
    CURRENT_BEDROCK_MODEL="$(extract_default_bedrock_model cdk.json "$BEDROCK_MODEL_LEAF")"
    if [ -z "$CURRENT_BEDROCK_MODEL" ]; then
      BEDROCK_MODEL_SKIP_REASON="Could not read context.bedrock.${BEDROCK_MODEL_LEAF} from cdk.json."
      echo "  $BEDROCK_MODEL_SKIP_REASON"
      continue
    fi
    if [ "$BEDROCK_MODEL_LEAF" = "embedding_model_id" ]; then
      # Embedding defaults are foundation models, not inference profiles.
      LATEST_BEDROCK_MODEL="$(get_latest_bedrock_embedding_model "$CURRENT_BEDROCK_MODEL" us-east-1)" || LATEST_BEDROCK_MODEL=""
    else
      LATEST_BEDROCK_MODEL="$(get_latest_bedrock_model "$CURRENT_BEDROCK_MODEL" us-east-1)" || LATEST_BEDROCK_MODEL=""
    fi
    if [ -z "$LATEST_BEDROCK_MODEL" ]; then
      BEDROCK_MODEL_SKIP_REASON="Bedrock model lookup failed or returned no active release in the model family of context.bedrock.${BEDROCK_MODEL_LEAF}."
      echo "  $BEDROCK_MODEL_SKIP_REASON"
    elif [ "$CURRENT_BEDROCK_MODEL" != "$LATEST_BEDROCK_MODEL" ] \
         && [ "$(compare_bedrock_model "$CURRENT_BEDROCK_MODEL" "$LATEST_BEDROCK_MODEL")" = "newer" ]; then
      echo "  - bedrock ${BEDROCK_MODEL_LEAF}: ${CURRENT_BEDROCK_MODEL} -> ${LATEST_BEDROCK_MODEL}"
      echo "context.bedrock.${BEDROCK_MODEL_LEAF}|${CURRENT_BEDROCK_MODEL}|${LATEST_BEDROCK_MODEL}" >> "$BEDROCK_MODEL_RESULTS"
    fi
  done
  # The vector store keeps its own embedding model at
  # context.vector_store.embedding_model_id (independent of mission memory's
  # bedrock.embedding_model_id by design). Same foundation-model drift check,
  # same re-embed caveat: adopting a newer model means re-ingesting the corpus.
  # The key is optional (the block ships with defaults), so absence is not a
  # skip condition for the whole check.
  VECTOR_STORE_MODEL="$(extract_default_bedrock_model cdk.json embedding_model_id vector_store)"
  if [ -n "$VECTOR_STORE_MODEL" ]; then
    LATEST_VECTOR_STORE_MODEL="$(get_latest_bedrock_embedding_model "$VECTOR_STORE_MODEL" us-east-1)" || LATEST_VECTOR_STORE_MODEL=""
    if [ -z "$LATEST_VECTOR_STORE_MODEL" ]; then
      BEDROCK_MODEL_SKIP_REASON="Bedrock model lookup failed or returned no active release in the model family of context.vector_store.embedding_model_id."
      echo "  $BEDROCK_MODEL_SKIP_REASON"
    elif [ "$VECTOR_STORE_MODEL" != "$LATEST_VECTOR_STORE_MODEL" ] \
         && [ "$(compare_bedrock_model "$VECTOR_STORE_MODEL" "$LATEST_VECTOR_STORE_MODEL")" = "newer" ]; then
      echo "  - vector_store embedding_model_id: ${VECTOR_STORE_MODEL} -> ${LATEST_VECTOR_STORE_MODEL}"
      echo "context.vector_store.embedding_model_id|${VECTOR_STORE_MODEL}|${LATEST_VECTOR_STORE_MODEL}" >> "$BEDROCK_MODEL_RESULTS"
    fi
  fi
fi

BEDROCK_MODEL_COUNT="$(wc -l < "$BEDROCK_MODEL_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$BEDROCK_MODEL_COUNT" ] && BEDROCK_MODEL_COUNT=0


# ---------------------------------------------------------------------------
# Dockerfile.dev ARG pins
#
# Checks the tooling versions pinned in ``Dockerfile.dev`` (Node.js
# release, AWS CDK CLI, kubectl, AWS CLI v2, Docker CLI, Docker Buildx). These ARGs sit
# outside the main dependency surfaces above — Dependabot watches the
# ``FROM python:…`` base image but not the ARG pins — so drift here has
# historically gone undetected until someone rebuilt the image.
#
# Each pin has its own upstream:
#
#   NODE_VERSION   github://nodejs/Release → schedule.json (active LTS
#                  major) + nodejs.org/dist/index.json (newest release
#                  on that major)
#   NPM_VERSION    registry.npmjs.org/npm/latest
#   CDK_VERSION    registry.npmjs.org/aws-cdk/latest
#   KUBECTL_VERSION https://dl.k8s.io/release/stable-<minor>.txt
#                  (minor from cdk.json::kubernetes_version)
#   AWSCLI_VERSION github://aws/aws-cli/tags (v2.x.y semver, no GitHub Releases)
#   DOCKER_VERSION github://moby/moby/releases/latest (``docker-v<ver>``)
#   BUILDX_VERSION github://docker/buildx/releases/latest (v<ver>)
#   UV_VERSION     github://astral-sh/uv/releases/latest (bare semver)
#
# All endpoints are public — no AWS credentials needed.
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking Dockerfile.dev ARG pins ==="

DOCKERFILE_RESULTS="$(mktemp)"
DOCKERFILE_PIN_FILE="Dockerfile.dev"

check_dockerfile_pin() {
  local name="$1" current="$2" latest=""
  case "$name" in
    NODE_VERSION)
      # Two drift signals folded into one compare. First pick the
      # highest major with an active LTS window from the release
      # schedule (lts <= today AND (end missing or end > today)),
      # then resolve that major's newest release from the official
      # dist index — the same origin the Dockerfile downloads from.
      # A new LTS line and a new patch on the current line both
      # surface as ``newer``.
      local lts_major
      lts_major="$(curl -fsSL --max-time 15 \
        "https://raw.githubusercontent.com/nodejs/Release/main/schedule.json" 2>/dev/null \
        | python3 -c '
import sys, json, datetime
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
today = datetime.date.today().isoformat()
candidates = []
for k, v in data.items():
    if not k.startswith("v") or "lts" not in v:
        continue
    if v["lts"] > today:
        continue
    if v.get("end", "9999-12-31") <= today:
        continue
    try:
        candidates.append(int(k[1:]))
    except ValueError:
        continue
if candidates:
    print(max(candidates))
' 2>/dev/null)" || true
      if [ -z "$lts_major" ]; then
        mark_scan_incomplete "Node.js release-schedule lookup failed or returned no active LTS major."
        return
      fi
      # index.json is newest-first per line, so the first entry whose
      # version sits on the LTS major is that major's latest release.
      latest="$(curl -fsSL --max-time 15 \
        "https://nodejs.org/dist/index.json" 2>/dev/null \
        | jq -r --arg prefix "v${lts_major}." \
          '[.[].version | select(startswith($prefix))][0] // empty' 2>/dev/null)" || true
      ;;
    CDK_VERSION)
      latest="$(curl -fsSL --max-time 15 \
        "https://registry.npmjs.org/aws-cdk/latest" 2>/dev/null \
        | jq -r '.version // empty' 2>/dev/null)" || true
      ;;
    NPM_VERSION)
      # ``npm`` is part of the dev container's pinned tooling — the
      # npm bundled inside the Node dist tarball is fixed per Node
      # release but lags npm's own line, so the Dockerfile installs a
      # specific ``npm@X.Y.Z`` to keep rebuilds reproducible (same
      # rationale as CDK_VERSION above). The canonical "latest" is the
      # ``latest`` dist-tag on npmjs.org, same source CDK uses.
      latest="$(curl -fsSL --max-time 15 \
        "https://registry.npmjs.org/npm/latest" 2>/dev/null \
        | jq -r '.version // empty' 2>/dev/null)" || true
      ;;
    KUBECTL_VERSION)
      # Match the minor line already committed to cdk.json so the pin
      # and the EKS cluster stay within the ±1 minor skew policy.
      local k8s_minor
      k8s_minor="$(extract_k8s_version "cdk.json")"
      latest="$(curl -fsSL --max-time 15 \
        "https://dl.k8s.io/release/stable-${k8s_minor}.txt" 2>/dev/null | tr -d '[:space:]')" || true
      ;;
    AWSCLI_VERSION)
      # aws/aws-cli doesn't publish GitHub Releases for v2; tags are the
      # canonical source. First page (per_page=20) is newest-first;
      # filter to 2.x.y semver and take the top match.
      latest="$(curl -fsSL --max-time 15 \
        "https://api.github.com/repos/aws/aws-cli/tags?per_page=20" 2>/dev/null \
        | jq -r '[.[].name | select(test("^2\\.[0-9]+\\.[0-9]+$"))][0] // empty' 2>/dev/null)" || true
      ;;
    DOCKER_VERSION)
      # moby/moby tags releases as ``docker-v<semver>``; strip the
      # prefix so compare_semver can handle the value.
      latest="$(curl -fsSL --max-time 15 \
        "https://api.github.com/repos/moby/moby/releases/latest" 2>/dev/null \
        | jq -r '.tag_name // empty' 2>/dev/null \
        | sed -E 's/^(docker-)?v//')" || true
      ;;
    BUILDX_VERSION)
      # docker/buildx publishes GitHub Releases tagged v<semver>
      # (e.g. v0.35.0). The release tag is the canonical source;
      # compare_semver strips the leading v on both sides. The
      # Dockerfile installs the plugin binary buildx-<tag>.linux-<arch>.
      latest="$(curl -fsSL --max-time 15 \
        "https://api.github.com/repos/docker/buildx/releases/latest" 2>/dev/null \
        | jq -r '.tag_name // empty' 2>/dev/null)" || true
      ;;
    UV_VERSION)
      # astral-sh/uv publishes GitHub Releases tagged with a bare semver
      # (e.g. 0.12.1). The dev container ships uv/uvx for the `gco
      # autopilot` companion MCP servers; bump the two SHA256 ARGs in
      # lockstep from the per-artifact *.sha256 release files.
      latest="$(curl -fsSL --max-time 15 \
        "https://api.github.com/repos/astral-sh/uv/releases/latest" 2>/dev/null \
        | jq -r '.tag_name // empty' 2>/dev/null)" || true
      ;;
    *)
      return
      ;;
  esac

  if [ -z "$latest" ]; then
    mark_scan_incomplete "Upstream version lookup failed for Dockerfile.dev pin ${name}."
    return
  fi

  # Every pin is a semver, NODE_VERSION included. compare_semver strips
  # a leading ``v`` on both sides, matching the kubectl and buildx pins
  # that keep the prefix.
  local relation
  relation="$(compare_semver "$current" "$latest")"

  if [ "$relation" = "newer" ]; then
    echo "  - ${name}: ${current} -> ${latest}"
    echo "${name}|${current}|${latest}" >> "$DOCKERFILE_RESULTS"
  fi
}

if [ -f "$DOCKERFILE_PIN_FILE" ]; then
  DOCKERFILE_PINS="$(extract_dockerfile_pins "$DOCKERFILE_PIN_FILE")"
  if [ -z "$DOCKERFILE_PINS" ]; then
    mark_scan_incomplete "Could not parse tooling pins from ${DOCKERFILE_PIN_FILE}."
  fi
  while IFS='|' read -r pin_name pin_value; do
    [ -z "$pin_name" ] && continue
    check_dockerfile_pin "$pin_name" "$pin_value"
  done <<< "$DOCKERFILE_PINS"
else
  mark_scan_incomplete "$DOCKERFILE_PIN_FILE is missing."
fi

DOCKERFILE_COUNT="$(wc -l < "$DOCKERFILE_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$DOCKERFILE_COUNT" ] && DOCKERFILE_COUNT=0

# ---------------------------------------------------------------------------
# GCO Autopilot pins (Claude Code release + companion MCP servers)
#
# ``gco autopilot`` (cli/autopilot.py) carries two dependency surfaces that
# live in Python constants, invisible to Dependabot and to every sweep above:
#
#   CLAUDE_CODE_VERSION      the exact @anthropic-ai/claude-code release the
#                            command installs on first use. Compared against
#                            the npm ``latest`` dist-tag — the same source and
#                            compare_semver treatment as the Dockerfile.dev
#                            npm-installed pins.
#   COMPANION_MCP_SERVERS    the npx/uvx-launched companion MCP servers wired
#                            into every autopilot session. Nothing pins them
#                            (they resolve at launch), so the risk isn't
#                            staleness — it's disappearance: a package that is
#                            unpublished, deprecated, or yanked breaks every
#                            new session. Each is resolved on its registry and
#                            reported when unhealthy. This is exactly how
#                            mcp-server-fetch and mcp-server-calculator broke
#                            before being pruned in 2026-08.
#
# Remediation for companion findings: replace or drop the server in
# cli/autopilot.py *and* the "Recommended Companion MCP Servers" tables in
# gco_mcp/README.md — tests/test_cli_autopilot.py fails the PR until the two
# agree. All endpoints are public — no AWS credentials needed.
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking GCO Autopilot pins ==="

AUTOPILOT_RESULTS="$(mktemp)"
AUTOPILOT_SKIP_REASON=""
AUTOPILOT_SOURCE="cli/autopilot.py"

if [ ! -f "$AUTOPILOT_SOURCE" ]; then
  AUTOPILOT_SKIP_REASON="${AUTOPILOT_SOURCE} not found."
  echo "  $AUTOPILOT_SKIP_REASON"
else
  CLAUDE_CODE_PIN="$(extract_claude_code_pin "$AUTOPILOT_SOURCE")"
  if [ -z "$CLAUDE_CODE_PIN" ]; then
    AUTOPILOT_SKIP_REASON="CLAUDE_CODE_VERSION not found in ${AUTOPILOT_SOURCE}."
    echo "  $AUTOPILOT_SKIP_REASON"
  else
    CLAUDE_CODE_STATUS="$(get_registry_package_status npm "@anthropic-ai/claude-code")"
    if [ -z "$CLAUDE_CODE_STATUS" ]; then
      AUTOPILOT_SKIP_REASON="npm lookup for @anthropic-ai/claude-code failed (network)."
      echo "  $AUTOPILOT_SKIP_REASON"
    else
      CLAUDE_CODE_LATEST="${CLAUDE_CODE_STATUS#*|}"
      case "$CLAUDE_CODE_STATUS" in
        ok\|*)
          if [ -n "$CLAUDE_CODE_LATEST" ] \
             && [ "$(compare_semver "$CLAUDE_CODE_PIN" "$CLAUDE_CODE_LATEST")" = "newer" ]; then
            echo "  - CLAUDE_CODE_VERSION: ${CLAUDE_CODE_PIN} -> ${CLAUDE_CODE_LATEST}"
            echo "@anthropic-ai/claude-code (CLAUDE_CODE_VERSION)|${CLAUDE_CODE_PIN}|${CLAUDE_CODE_LATEST}|https://www.npmjs.com/package/@anthropic-ai/claude-code" >> "$AUTOPILOT_RESULTS"
          fi
          ;;
        *)
          # The install pin itself is deprecated/unpublished — always drift.
          echo "  - @anthropic-ai/claude-code: ${CLAUDE_CODE_STATUS%%|*}"
          echo "@anthropic-ai/claude-code (CLAUDE_CODE_VERSION)|${CLAUDE_CODE_PIN}|${CLAUDE_CODE_STATUS%%|*}|https://www.npmjs.com/package/@anthropic-ai/claude-code" >> "$AUTOPILOT_RESULTS"
          ;;
      esac
    fi
  fi

  # Companion MCP server liveness. Missing/deprecated/yanked is drift; a
  # network failure marks the scan incomplete rather than inventing findings.
  while IFS='|' read -r companion_name companion_registry companion_package; do
    [ -z "$companion_name" ] && continue
    companion_status="$(get_registry_package_status "$companion_registry" "$companion_package")"
    if [ -z "$companion_status" ]; then
      if [ -z "$AUTOPILOT_SKIP_REASON" ]; then
        AUTOPILOT_SKIP_REASON="Registry lookup failed for ${companion_package} (${companion_registry}); companion liveness incomplete."
        echo "  $AUTOPILOT_SKIP_REASON"
      fi
      continue
    fi
    case "$companion_status" in
      ok\|*)
        ;;
      *)
        companion_verdict="${companion_status%%|*}"
        companion_detail="${companion_status#*|}"
        [ -n "$companion_detail" ] && companion_verdict="${companion_verdict}: ${companion_detail}"
        if [ "$companion_registry" = "npm" ]; then
          companion_url="https://www.npmjs.com/package/${companion_package}"
        else
          companion_url="https://pypi.org/project/${companion_package}/"
        fi
        echo "  - companion ${companion_name}: ${companion_verdict}"
        echo "companion ${companion_name} (${companion_registry}: ${companion_package})|launch-time (unpinned)|${companion_verdict}|${companion_url}" >> "$AUTOPILOT_RESULTS"
        ;;
    esac
  done < <(extract_companion_mcp_packages "$AUTOPILOT_SOURCE")
fi

AUTOPILOT_COUNT="$(wc -l < "$AUTOPILOT_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$AUTOPILOT_COUNT" ] && AUTOPILOT_COUNT=0

# ---------------------------------------------------------------------------
# Pre-commit hook revisions
#
# Compares the ``rev:`` pinned for each ``repo:`` block in
# ``.pre-commit-config.yaml`` against the latest semver-shaped tag
# published by the upstream Git host. This catches drift Dependabot
# can't see — pre-commit pins live in YAML, not in the package
# ecosystems Dependabot monitors — and matters in practice because
# stale hook pins quietly miss new lint rules and bug fixes.
#
# Each hook's repo URL is resolved to a tag list via the GitHub API
# (the only host we use today). Full SHA-1/SHA-256 object ids are accepted as
# immutable exemptions. Other unsupported refs (branches, floating labels,
# prereleases) mark the scan incomplete rather than silently disappearing.
# Calls are unauthenticated; we make one request per hook, which is well below
# the 60 req/h public limit.
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking pre-commit hook revisions ==="

PRECOMMIT_RESULTS="$(mktemp)"
PRECOMMIT_CONFIG=".pre-commit-config.yaml"

if [ -f "$PRECOMMIT_CONFIG" ]; then
  PRECOMMIT_HOOKS="$(extract_precommit_hooks "$PRECOMMIT_CONFIG")"
  if [ -z "$PRECOMMIT_HOOKS" ]; then
    mark_scan_incomplete "Could not parse hook pins from ${PRECOMMIT_CONFIG}."
  fi
  while IFS='|' read -r repo current_rev; do
    [ -z "$repo" ] && continue
    [ -z "$current_rev" ] && continue
    # Complete Git object ids are immutable and intentionally have no release
    # drift lookup. Every other non-semver ref may move or cannot be compared
    # safely, so it makes this scan incomplete instead of receiving a blanket
    # "SHA" exemption.
    if is_full_git_commit_sha "$current_rev"; then
      continue
    fi
    if ! [[ "$current_rev" =~ ^v?[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
      mark_scan_incomplete "Unsupported mutable or non-semver pre-commit rev '${current_rev}' for ${repo}."
      continue
    fi

    latest_rev="$(get_latest_precommit_hook_release "$repo")"
    if [ -z "$latest_rev" ]; then
      mark_scan_incomplete "Pre-commit tag lookup failed for ${repo}."
      continue
    fi

    # Strip ``v`` so compare_semver ranks ``v0.22.1`` vs ``v0.22.2``
    # (and the rare unprefixed ``1.38.0`` from yamllint historically)
    # consistently. We keep the original ``current_rev`` / ``latest_rev``
    # strings in the report so the operator copy-pastes the exact
    # value pre-commit expects.
    if [ "$current_rev" != "$latest_rev" ] \
       && [ "$(compare_semver "$current_rev" "$latest_rev")" = "newer" ]; then
      echo "  - ${repo}: ${current_rev} -> ${latest_rev}"
      echo "${repo}|${current_rev}|${latest_rev}" >> "$PRECOMMIT_RESULTS"
    fi
  done <<< "$PRECOMMIT_HOOKS"
else
  mark_scan_incomplete "$PRECOMMIT_CONFIG is missing."
fi

PRECOMMIT_COUNT="$(wc -l < "$PRECOMMIT_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$PRECOMMIT_COUNT" ] && PRECOMMIT_COUNT=0

# ---------------------------------------------------------------------------
# CDK enum constants
#
# Compares the CDK-enum-name constants pinned in
# ``gco/stacks/constants.py`` against the highest enum members exposed
# by the installed ``aws-cdk-lib``. This catches the case where
# aws-cdk-lib already supports a newer enum (because we bumped the
# library, or simply because the latest published release added one)
# but ``constants.py`` still pins an older one.
#
# Two enums are tracked today:
#
#   - ``LAMBDA_PYTHON_RUNTIME`` → ``aws_cdk.aws_lambda.Runtime.PYTHON_X_Y``
#   - ``LAMBDA_NODEJS_RUNTIME`` → ``aws_cdk.aws_lambda.Runtime.NODEJS_<major>_X``
#
# The Aurora engine deliberately is NOT an enum: constants.py pins a plain
# version string applied through ``AuroraPostgresEngineVersion.of()``, and
# the "Aurora PostgreSQL engine" section validates it against the live RDS
# API — the authoritative source — instead of the CDK library's catalog.
#
# The deps-scan workflow installs the latest ``aws-cdk-lib`` for this
# section; locally the helper just reflects whatever's already on the
# active interpreter. If aws-cdk-lib isn't importable we skip with a
# one-line note (mirrors the AWS-creds skip pattern used elsewhere).
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking CDK enum constants ==="

CDK_ENUM_RESULTS="$(mktemp)"
CDK_ENUM_SKIP_REASON=""

if ! python3 -c "import aws_cdk" 2>/dev/null; then
  CDK_ENUM_SKIP_REASON="aws-cdk-lib not importable. Install with 'pip install aws-cdk-lib' to enable."
  echo "  $CDK_ENUM_SKIP_REASON"
else
  # Lambda Python runtime enum
  LAMBDA_RT_CURRENT="$(extract_constant_value LAMBDA_PYTHON_RUNTIME)"
  LAMBDA_RT_LATEST="$(get_latest_lambda_python_runtime)"
  if [ -z "$LAMBDA_RT_CURRENT" ] || [ -z "$LAMBDA_RT_LATEST" ]; then
    mark_scan_incomplete "Could not parse the current or latest Lambda Python runtime enum."
  elif [ "$LAMBDA_RT_CURRENT" != "$LAMBDA_RT_LATEST" ]; then
    # Convert PYTHON_3_14 → 3.14 so compare_semver can rank them.
    cur_v="$(echo "$LAMBDA_RT_CURRENT" | sed -E 's/^PYTHON_([0-9]+)_([0-9]+)$/\1.\2/')"
    lat_v="$(echo "$LAMBDA_RT_LATEST"  | sed -E 's/^PYTHON_([0-9]+)_([0-9]+)$/\1.\2/')"
    if [ "$(compare_semver "$cur_v" "$lat_v")" = "newer" ]; then
      echo "  - LAMBDA_PYTHON_RUNTIME: ${LAMBDA_RT_CURRENT} -> ${LAMBDA_RT_LATEST}"
      echo "LAMBDA_PYTHON_RUNTIME|aws_lambda.Runtime|${LAMBDA_RT_CURRENT}|${LAMBDA_RT_LATEST}" >> "$CDK_ENUM_RESULTS"
    fi
  fi

  # Lambda Node.js runtime enum
  LAMBDA_NODE_RT_CURRENT="$(extract_constant_value LAMBDA_NODEJS_RUNTIME)"
  LAMBDA_NODE_RT_LATEST="$(get_latest_lambda_nodejs_runtime)"
  if [ -z "$LAMBDA_NODE_RT_CURRENT" ] || [ -z "$LAMBDA_NODE_RT_LATEST" ]; then
    mark_scan_incomplete "Could not parse the current or latest Lambda Node.js runtime enum."
  elif [ "$LAMBDA_NODE_RT_CURRENT" != "$LAMBDA_NODE_RT_LATEST" ]; then
    cur_major="${LAMBDA_NODE_RT_CURRENT#NODEJS_}"
    cur_major="${cur_major%_X}"
    lat_major="${LAMBDA_NODE_RT_LATEST#NODEJS_}"
    lat_major="${lat_major%_X}"
    if [[ "$cur_major" =~ ^[0-9]+$ ]] && [[ "$lat_major" =~ ^[0-9]+$ ]] \
       && [ "$lat_major" -gt "$cur_major" ]; then
      echo "  - LAMBDA_NODEJS_RUNTIME: ${LAMBDA_NODE_RT_CURRENT} -> ${LAMBDA_NODE_RT_LATEST}"
      echo "LAMBDA_NODEJS_RUNTIME|aws_lambda.Runtime|${LAMBDA_NODE_RT_CURRENT}|${LAMBDA_NODE_RT_LATEST}" >> "$CDK_ENUM_RESULTS"
    fi
  fi

fi

CDK_ENUM_COUNT="$(wc -l < "$CDK_ENUM_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$CDK_ENUM_COUNT" ] && CDK_ENUM_COUNT=0

# ---------------------------------------------------------------------------
# Python release
#
# Compares the Lambda Python runtime constant (which encodes the major
# Python version we standardise on across every Lambda in the project)
# against the latest stable Python release on endoflife.date.
#
# This is informational drift — Lambda may not ship support for a brand-
# new Python release for several months — but the signal is useful so
# the operator knows when to start planning a runtime bump. It also
# complements the CDK-enum check above: that check answers "what does
# aws-cdk-lib expose?", this one answers "what has python.org actually
# shipped?".
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking Python release ==="

PYTHON_RELEASE_RESULTS="$(mktemp)"
PYTHON_RELEASE_SKIP_REASON=""

# Re-read in case the CDK section was skipped and never set the var.
LAMBDA_RT_CURRENT="${LAMBDA_RT_CURRENT:-$(extract_constant_value LAMBDA_PYTHON_RUNTIME)}"
LATEST_PYTHON="$(get_latest_python_release)"

if [ -z "$LATEST_PYTHON" ]; then
  PYTHON_RELEASE_SKIP_REASON="endoflife.date query failed (network or schema change)."
  echo "  $PYTHON_RELEASE_SKIP_REASON"
elif [ -z "$LAMBDA_RT_CURRENT" ]; then
  PYTHON_RELEASE_SKIP_REASON="Could not parse LAMBDA_PYTHON_RUNTIME for the Python release comparison."
  echo "  $PYTHON_RELEASE_SKIP_REASON"
else
  cur_v="$(echo "$LAMBDA_RT_CURRENT" | sed -E 's/^PYTHON_([0-9]+)_([0-9]+)$/\1.\2/')"
  if [ "$cur_v" != "$LATEST_PYTHON" ] \
     && [ "$(compare_semver "$cur_v" "$LATEST_PYTHON")" = "newer" ]; then
    echo "  - python (LAMBDA_PYTHON_RUNTIME): ${cur_v} -> ${LATEST_PYTHON}"
    echo "python|${cur_v}|${LATEST_PYTHON}" >> "$PYTHON_RELEASE_RESULTS"
  fi
fi

PYTHON_RELEASE_COUNT="$(wc -l < "$PYTHON_RELEASE_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$PYTHON_RELEASE_COUNT" ] && PYTHON_RELEASE_COUNT=0

# ---------------------------------------------------------------------------
# CI tooling pins (public endpoints — no AWS creds)
#
# The workflows install their own pinned tooling — Trivy (cve-scan.yml /
# security.yml), actionlint (lint.yml), Helm + kubectl (deps-scan.yml), and
# kubeconform, Calico, and Metrics Server (integration-tests.yml) — from plain
# ``*_VERSION`` env strings. The integration workflow also pins kind + its node
# image on the ``helm/kind-action`` step. None are ``uses:`` refs or Dockerfile
# ``FROM`` lines, so Dependabot never sees them. Compare each against upstream.
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking CI tooling pins ==="

CI_TOOLING_RESULTS="$(mktemp)"

# check_github_tool <display-name> <current-pin> <owner/repo> <ref-url>
# Records drift when the pinned semver is behind the latest GitHub Release.
check_github_tool() {
  local name="$1" current="$2" repo="$3" url="$4" latest=""
  if [ -z "$current" ]; then
    mark_scan_incomplete "Could not parse the committed version pin for ${name}."
    return 0
  fi
  latest="$(get_latest_github_release_tag "$repo")"
  if [ -z "$latest" ]; then
    mark_scan_incomplete "GitHub release lookup failed for ${name} (${repo})."
    return 0
  fi
  if [ "$current" != "$latest" ] \
     && [ "$(compare_semver "$current" "$latest")" = "newer" ]; then
    echo "  - ${name}: ${current} -> ${latest}"
    echo "${name}|${current}|${latest}|${url}" >> "$CI_TOOLING_RESULTS"
  fi
}

# Trivy (aquasecurity/trivy) — TRIVY_VERSION in the security workflows.
TRIVY_PIN="$(extract_workflow_env_pin TRIVY_VERSION | head -1)"
check_github_tool "Trivy (TRIVY_VERSION)" "$TRIVY_PIN" "aquasecurity/trivy" \
  "https://github.com/aquasecurity/trivy/releases"

# actionlint (rhysd/actionlint) — lint.yml downloads this release archive.
ACTIONLINT_PIN="$(extract_workflow_env_pin ACTIONLINT_VERSION | head -1)"
check_github_tool "actionlint (ACTIONLINT_VERSION)" "$ACTIONLINT_PIN" "rhysd/actionlint" \
  "https://github.com/rhysd/actionlint/releases"

# Helm (helm/helm) — HELM_VERSION the deps-scan workflow installs.
HELM_PIN="$(extract_workflow_env_pin HELM_VERSION | head -1)"
check_github_tool "Helm (HELM_VERSION)" "$HELM_PIN" "helm/helm" \
  "https://github.com/helm/helm/releases"

# kubeconform (yannh/kubeconform) — KUBECONFORM_VERSION the
# integration:k8s:manifest-schema job installs to schema-validate the K8s
# manifests. Plain env pin Dependabot doesn't watch; a stale kubeconform
# silently validates against outdated Kubernetes schemas.
KUBECONFORM_PIN="$(extract_workflow_env_pin KUBECONFORM_VERSION | head -1)"
check_github_tool "kubeconform (KUBECONFORM_VERSION)" "$KUBECONFORM_PIN" "yannh/kubeconform" \
  "https://github.com/yannh/kubeconform/releases"

# Metrics Server (kubernetes-sigs/metrics-server) — kind installs this pin so
# the inference proxy HPA must reach ScalingActive, mirroring the EKS managed
# add-on contract used in production.
METRICS_SERVER_PIN="$(extract_workflow_env_pin METRICS_SERVER_VERSION | head -1)"
check_github_tool \
  "Metrics Server (METRICS_SERVER_VERSION)" \
  "$METRICS_SERVER_PIN" \
  "kubernetes-sigs/metrics-server" \
  "https://github.com/kubernetes-sigs/metrics-server/releases"

# Calico (projectcalico/calico) — kind installs the authenticated release
# manifest so NetworkPolicy behavior is exercised by the E2E job.
CALICO_PIN="$(extract_workflow_env_pin CALICO_VERSION | head -1)"
check_github_tool "Calico (CALICO_VERSION)" "$CALICO_PIN" "projectcalico/calico" \
  "https://github.com/projectcalico/calico/releases"

# kind (kubernetes-sigs/kind) — the kind binary on the kind-action step.
KIND_PIN="$(extract_kind_pins .github/workflows/integration-tests.yml | awk -F'|' '$1=="kind"{print $2}')"
check_github_tool "kind" "$KIND_PIN" "kubernetes-sigs/kind" \
  "https://github.com/kubernetes-sigs/kind/releases"

# kubectl (workflow env) — compare against the stable release for its own
# minor line (dl.k8s.io), the same source the Dockerfile.dev kubectl pin uses.
KUBECTL_WF_PIN="$(extract_workflow_env_pin KUBECTL_VERSION | head -1)"
if [ -n "$KUBECTL_WF_PIN" ]; then
  kubectl_minor="$(echo "${KUBECTL_WF_PIN#v}" | cut -d. -f1-2)"
  if ! kubectl_latest="$(curl -fsSL --max-time 15 \
    "https://dl.k8s.io/release/stable-${kubectl_minor}.txt" 2>/dev/null | tr -d '[:space:]')" \
     || [ -z "$kubectl_latest" ]; then
    mark_scan_incomplete "kubectl stable-version lookup failed for minor ${kubectl_minor}."
  elif [ "$KUBECTL_WF_PIN" != "$kubectl_latest" ] \
     && [ "$(compare_semver "$KUBECTL_WF_PIN" "$kubectl_latest")" = "newer" ]; then
    echo "  - kubectl (KUBECTL_VERSION): ${KUBECTL_WF_PIN} -> ${kubectl_latest}"
    echo "kubectl (KUBECTL_VERSION)|${KUBECTL_WF_PIN}|${kubectl_latest}|https://kubernetes.io/releases/" >> "$CI_TOOLING_RESULTS"
  fi
else
  mark_scan_incomplete "Could not parse KUBECTL_VERSION from the workflows."
fi

# kind node image (kindest/node) — report a newer PATCH within the pinned K8s
# minor only. Jumping minors is governed by the kind release, not free drift,
# so scoping to the same minor avoids false "upgrade" noise.
KIND_NODE_PIN="$(extract_kind_pins .github/workflows/integration-tests.yml | awk -F'|' '$1=="kind-node"{print $2}')"
if [ -n "$KIND_NODE_PIN" ]; then
  node_tag="${KIND_NODE_PIN##*:}"
  node_minor="$(echo "${node_tag#v}" | cut -d. -f1-2)"
  if ! node_latest="$(skopeo list-tags "docker://docker.io/kindest/node" 2>/dev/null \
    | jq -r '.Tags[]' 2>/dev/null \
    | grep -E "^v?${node_minor}\.[0-9]+$" \
    | sort -V | tail -1)" || [ -z "$node_latest" ]; then
    mark_scan_incomplete "Container registry lookup failed for kindest/node minor ${node_minor}."
  elif [ "$node_tag" != "$node_latest" ] \
     && [ "$(compare_semver "$node_tag" "$node_latest")" = "newer" ]; then
    echo "  - kind node image (kindest/node): ${node_tag} -> ${node_latest}"
    echo "kind node image (kindest/node)|${node_tag}|${node_latest}|https://hub.docker.com/r/kindest/node/tags" >> "$CI_TOOLING_RESULTS"
  fi
else
  mark_scan_incomplete "Could not parse the kind node-image pin from integration-tests.yml."
fi

CI_TOOLING_COUNT="$(wc -l < "$CI_TOOLING_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$CI_TOOLING_COUNT" ] && CI_TOOLING_COUNT=0

# ---------------------------------------------------------------------------
# Version consistency (no network)
#
# Some versions are pinned in more than one place and must move together.
# The other sections answer "is this pin behind upstream?"; this one answers
# "do the copies of this pin agree with each other?" — a class of drift that
# otherwise only surfaces when a formatter/linter behaves differently in CI
# than it does locally.
#
#   - ruff: pyproject (dev install) vs the pre-commit hook vs the prebuilt-
#     binary ruff-action step in lint.yml.
#   - python-version across the workflows vs the project's canonical Python
#     (the LAMBDA_PYTHON_RUNTIME the Lambdas ship on).
#   - Node major across LAMBDA_NODEJS_RUNTIME, .nvmrc, every package engine,
#     and Dockerfile.dev; npm across every packageManager and Dockerfile.dev.
#   - AWS CDK CLI across the locked root npm graph and Dockerfile.dev.
#   - every repository-owned package.json has a lockfile, exact direct pins,
#     and a matching npm entry in Dependabot.
#   - the same tool env pin (TRIVY_VERSION/HELM_VERSION/KUBECTL_VERSION)
#     resolving to different values in different workflow files.
#   - every [build-system] requires entry in pyproject.toml is an exact
#     ``==`` pin (the drift itself reports through the Python surface).
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking version consistency ==="

CONSISTENCY_RESULTS="$(mktemp)"

RUFF_PINS="$(extract_ruff_pins pyproject.toml .pre-commit-config.yaml .github/workflows/lint.yml)"
if [ -n "$RUFF_PINS" ]; then
  ruff_distinct="$(echo "$RUFF_PINS" | cut -d'|' -f2 | sort -u | grep -c .)"
  if [ "$ruff_distinct" -gt 1 ]; then
    detail="$(echo "$RUFF_PINS" | awk -F'|' '{printf "%s=%s ", $1, $2}' | sed 's/ *$//')"
    echo "  - ruff pins disagree: $detail"
    echo "ruff (pyproject / pre-commit / lint action)|${detail}" >> "$CONSISTENCY_RESULTS"
  fi
fi

CANON_PY="$(echo "${LAMBDA_RT_CURRENT:-}" | sed -E 's/^PYTHON_([0-9]+)_([0-9]+)$/\1.\2/')"
[ -z "$CANON_PY" ] && CANON_PY="$(extract_constant_value LAMBDA_PYTHON_RUNTIME | sed -E 's/^PYTHON_([0-9]+)_([0-9]+)$/\1.\2/')"
PY_PINS_UNIQUE="$(extract_python_version_pins "$WORKFLOWS_DIR" | sort -u)"
if [ -n "$PY_PINS_UNIQUE" ]; then
  py_distinct="$(echo "$PY_PINS_UNIQUE" | grep -c .)"
  py_list="$(echo "$PY_PINS_UNIQUE" | paste -sd',' -)"
  if [ "$py_distinct" -gt 1 ] || { [ -n "$CANON_PY" ] && [ "$py_list" != "$CANON_PY" ]; }; then
    echo "  - python-version pins: ${py_list} (project runtime: ${CANON_PY:-unknown})"
    echo "python-version (CI vs runtime)|CI: ${py_list}; runtime: ${CANON_PY:-unknown}" >> "$CONSISTENCY_RESULTS"
  fi
fi

NPM_PACKAGE_SOURCES="$(
  list_npm_package_dirs . | while IFS= read -r package_dir; do
    [ -n "$package_dir" ] || continue
    if [ "$package_dir" = "." ]; then
      echo "package.json"
    else
      echo "${package_dir}/package.json"
    fi
  done
)"

NODE_PINS="$(extract_node_major_pins . gco/stacks/constants.py .nvmrc Dockerfile.dev | sort -u)"
EXPECTED_NODE_SOURCES="$(
  {
    printf '%s\n' gco/stacks/constants.py .nvmrc Dockerfile.dev
    printf '%s\n' "$NPM_PACKAGE_SOURCES"
  } | sed '/^$/d' | sort -u
)"
NODE_PIN_SOURCES="$(printf '%s\n' "$NODE_PINS" | cut -d'|' -f1 | sed '/^$/d' | sort -u)"
NODE_MISSING="$(comm -23 <(printf '%s\n' "$EXPECTED_NODE_SOURCES") <(printf '%s\n' "$NODE_PIN_SOURCES"))"
node_distinct="$(printf '%s\n' "$NODE_PINS" | cut -d'|' -f2 | sed '/^$/d' | sort -u | grep -c .)"
if [ -n "$NODE_MISSING" ] || [ "$node_distinct" -gt 1 ]; then
  node_detail="$(printf '%s\n' "$NODE_PINS" | awk -F'|' '{printf "%s=%s ", $1, $2}' | sed 's/ *$//')"
  if [ -n "$NODE_MISSING" ]; then
    node_missing_list="$(printf '%s\n' "$NODE_MISSING" | paste -sd',' -)"
    node_detail="${node_detail}; missing=${node_missing_list}"
  fi
  echo "  - Node.js major pins disagree or are missing: ${node_detail}"
  echo "Node.js major (runtime / packages / dev container)|${node_detail}" >> "$CONSISTENCY_RESULTS"
fi

NPM_PINS="$(extract_npm_version_pins . Dockerfile.dev | sort -u)"
EXPECTED_NPM_SOURCES="$(
  {
    printf '%s\n' Dockerfile.dev
    printf '%s\n' "$NPM_PACKAGE_SOURCES"
  } | sed '/^$/d' | sort -u
)"
NPM_PIN_SOURCES="$(printf '%s\n' "$NPM_PINS" | cut -d'|' -f1 | sed '/^$/d' | sort -u)"
NPM_MISSING="$(comm -23 <(printf '%s\n' "$EXPECTED_NPM_SOURCES") <(printf '%s\n' "$NPM_PIN_SOURCES"))"
npm_distinct="$(printf '%s\n' "$NPM_PINS" | cut -d'|' -f2 | sed '/^$/d' | sort -u | grep -c .)"
if [ -n "$NPM_MISSING" ] || [ "$npm_distinct" -gt 1 ]; then
  npm_detail="$(printf '%s\n' "$NPM_PINS" | awk -F'|' '{printf "%s=%s ", $1, $2}' | sed 's/ *$//')"
  if [ -n "$NPM_MISSING" ]; then
    npm_missing_list="$(printf '%s\n' "$NPM_MISSING" | paste -sd',' -)"
    npm_detail="${npm_detail}; missing=${npm_missing_list}"
  fi
  echo "  - npm pins disagree or are missing: ${npm_detail}"
  echo "npm (packageManager / dev container)|${npm_detail}" >> "$CONSISTENCY_RESULTS"
fi

CDK_CLI_PINS="$(extract_cdk_cli_pins . Dockerfile.dev | sort -u)"
CDK_CLI_MISSING="$(comm -23 \
  <(printf '%s\n' Dockerfile.dev package.json | sort -u) \
  <(printf '%s\n' "$CDK_CLI_PINS" | cut -d'|' -f1 | sed '/^$/d' | sort -u))"
cdk_cli_distinct="$(printf '%s\n' "$CDK_CLI_PINS" | cut -d'|' -f2 | sed '/^$/d' | sort -u | grep -c .)"
if [ -n "$CDK_CLI_MISSING" ] || [ "$cdk_cli_distinct" -gt 1 ]; then
  cdk_detail="$(printf '%s\n' "$CDK_CLI_PINS" | awk -F'|' '{printf "%s=%s ", $1, $2}' | sed 's/ *$//')"
  if [ -n "$CDK_CLI_MISSING" ]; then
    cdk_missing_list="$(printf '%s\n' "$CDK_CLI_MISSING" | paste -sd',' -)"
    cdk_detail="${cdk_detail}; missing=${cdk_missing_list}"
  fi
  echo "  - AWS CDK CLI pins disagree or are missing: ${cdk_detail}"
  echo "AWS CDK CLI (package / dev container)|${cdk_detail}" >> "$CONSISTENCY_RESULTS"
fi

NPM_MANAGEMENT_PROBLEMS="$(check_npm_package_management . .github/dependabot.yml)"
if [ -n "$NPM_MANAGEMENT_PROBLEMS" ]; then
  while IFS='|' read -r manifest problem; do
    [ -n "$manifest" ] || continue
    echo "  - npm dependency management: ${manifest}: ${problem}"
    echo "npm dependency management|${manifest}: ${problem}" >> "$CONSISTENCY_RESULTS"
  done <<< "$NPM_MANAGEMENT_PROBLEMS"
fi

for consistency_var in TRIVY_VERSION HELM_VERSION KUBECTL_VERSION; do
  cvals="$(extract_workflow_env_pin "$consistency_var")"
  cnum="$(echo "$cvals" | grep -c .)"
  if [ "$cnum" -gt 1 ]; then
    clist="$(echo "$cvals" | paste -sd',' -)"
    echo "  - ${consistency_var} disagrees across workflows: ${clist}"
    echo "${consistency_var} (across workflows)|${clist}" >> "$CONSISTENCY_RESULTS"
  fi
done

# helm / kubectl pins hardcoded in lambda/helm-installer/Dockerfile RUN-line
# URLs must agree with the workflow env pins (and, for kubectl, with the
# Dockerfile.dev ARG). These URL literals were previously invisible to every
# check — the integration-tests workflow even carries a comment noting the
# Lambda copy "isn't caught by the consistency check". Now it is.
INSTALLER_PINS="$(extract_helm_installer_pins lambda/helm-installer/Dockerfile)"
if [ -n "$INSTALLER_PINS" ]; then
  for tool_var in HELM_VERSION KUBECTL_VERSION; do
    installer_val="$(printf '%s\n' "$INSTALLER_PINS" | awk -F'|' -v v="$tool_var" '$1==v{print $2}')"
    [ -n "$installer_val" ] || continue
    all_vals="$installer_val"
    wf_vals="$(extract_workflow_env_pin "$tool_var")"
    [ -n "$wf_vals" ] && all_vals="$(printf '%s\n%s' "$all_vals" "$wf_vals")"
    if [ "$tool_var" = "KUBECTL_VERSION" ]; then
      dev_val="$(extract_dockerfile_pins Dockerfile.dev | awk -F'|' '$1=="KUBECTL_VERSION"{print $2}')"
      [ -n "$dev_val" ] && all_vals="$(printf '%s\n%s' "$all_vals" "$dev_val")"
    fi
    tool_distinct="$(printf '%s\n' "$all_vals" | sed '/^$/d' | sort -u | grep -c .)"
    if [ "$tool_distinct" -gt 1 ]; then
      tool_list="$(printf '%s\n' "$all_vals" | sed '/^$/d' | sort -u | paste -sd',' -)"
      echo "  - ${tool_var} disagrees between helm-installer Dockerfile, workflows, and Dockerfile.dev: ${tool_list}"
      echo "${tool_var} (helm-installer Dockerfile / workflows / Dockerfile.dev)|${tool_list}" >> "$CONSISTENCY_RESULTS"
    fi
  done
fi

# Build-backend pins must use the same exact ``==`` shape as every other
# Python dependency in pyproject.toml, or the version resolved inside
# pip's build isolation floats with upstream releases. An empty result is
# itself a finding — [build-system] always exists here, so nothing coming
# back means the table was removed or the TOML no longer parses, and a
# parse break must not silently drop the check.
BUILD_SYSTEM_PINS="${BUILD_SYSTEM_PINS:-$(extract_build_system_pins pyproject.toml)}"
if [ -z "$BUILD_SYSTEM_PINS" ]; then
  echo "  - pyproject.toml [build-system] requires is missing or unparseable"
  echo "build-system requires (pyproject.toml)|missing or unparseable" >> "$CONSISTENCY_RESULTS"
else
  while IFS='|' read -r bs_name bs_version bs_raw; do
    [ -n "$bs_raw" ] || continue
    if [ -z "$bs_version" ]; then
      echo "  - build-system requires entry is not an exact ==X.Y.Z pin: ${bs_raw}"
      echo "build-system requires (pyproject.toml)|'${bs_raw}' must be an exact ==X.Y.Z pin" >> "$CONSISTENCY_RESULTS"
    fi
  done <<< "$BUILD_SYSTEM_PINS"
fi

CONSISTENCY_COUNT="$(wc -l < "$CONSISTENCY_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$CONSISTENCY_COUNT" ] && CONSISTENCY_COUNT=0

# ---------------------------------------------------------------------------
# Base-image security epochs (no network)
#
# The service images (Debian) and the helm-installer Lambda (AL2023) pull OS
# security patches at build time behind a hand-bumped ``*_SECURITY_EPOCH``
# ARG that busts the CI layer cache. Nothing else reminds anyone to move the
# date, so a stale epoch silently reuses an old upgrade layer. Trivy's
# container scan is the backstop; this flags an epoch older than the window
# as the proactive nudge. Only the real Dockerfiles are scanned — the
# generated ``*-build`` staging copies are skipped.
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking base-image security epochs ==="

EPOCH_RESULTS="$(mktemp)"
EPOCH_FILES=(dockerfiles/*-dockerfile Dockerfile.dev lambda/helm-installer/Dockerfile)
for df in "${EPOCH_FILES[@]}"; do
  [ -f "$df" ] || continue
  extract_security_epochs "$df" | while IFS='|' read -r epoch_arg epoch_date; do
    [ -z "$epoch_date" ] && continue
    epoch_age="$(days_since "$epoch_date")"
    [ -z "$epoch_age" ] && continue
    if [ "$epoch_age" -gt "$SECURITY_EPOCH_STALE_DAYS" ]; then
      echo "  - ${df} (${epoch_arg}): ${epoch_date} (${epoch_age} days old)"
      echo "${df}|${epoch_arg}|${epoch_date}|${epoch_age}" >> "$EPOCH_RESULTS"
    fi
  done
done

EPOCH_COUNT="$(wc -l < "$EPOCH_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$EPOCH_COUNT" ] && EPOCH_COUNT=0

# ---------------------------------------------------------------------------
# Suppression expiries (no network)
#
# ``.trivyignore`` / ``.pip-audit-ignore`` / ``.npm-audit-ignore`` entries
# carry an ``exp:YYYY-MM-DD`` marker. The CI validators hard-fail a PR on the
# day an entry expires; this surfaces entries expiring *soon* so they get
# re-evaluated (fixed upstream? extend with a new justification?) before they
# break a build.
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking suppression expiries ==="

SUPPRESSION_RESULTS="$(mktemp)"
for supfile in .github/config/.trivyignore .github/config/.pip-audit-ignore .github/config/.npm-audit-ignore; do
  [ -f "$supfile" ] || continue
  supbase="$(basename "$supfile")"
  parse_suppression_expiries "$supfile" | while IFS='|' read -r sup_id sup_date; do
    [ -z "$sup_date" ] && continue
    sup_left="$(days_until "$sup_date")"
    [ -z "$sup_left" ] && continue
    if [ "$sup_left" -le "$SUPPRESSION_EXPIRY_WARN_DAYS" ]; then
      echo "  - ${supbase}: ${sup_id} expires ${sup_date} (${sup_left} days)"
      echo "${supbase}|${sup_id}|${sup_date}|${sup_left}" >> "$SUPPRESSION_RESULTS"
    fi
  done
done

SUPPRESSION_COUNT="$(wc -l < "$SUPPRESSION_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$SUPPRESSION_COUNT" ] && SUPPRESSION_COUNT=0

# ---------------------------------------------------------------------------
# Lockfile freshness (no network)
#
# ``requirements-lock.txt`` is compiled from ``pyproject.toml`` with
# ``pip-compile --all-extras``. Every direct dependency must use an exact,
# concrete pin and is matched to the lock by normalized name, canonical marker
# identity, and version. Missing or mismatched records mean the lock is stale;
# unrelated transitive pins are ignored.
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking lockfile freshness ==="

LOCKFILE_RESULTS="$(mktemp)"
LOCKFILE_RAW="$(mktemp)"
if check_lockfile_freshness pyproject.toml requirements-lock.txt > "$LOCKFILE_RAW"; then
  while IFS='|' read -r lock_name expected_version locked_version; do
    [ -z "$lock_name" ] && continue
    if [ "$locked_version" = "<missing>" ]; then
      echo "  - direct dep missing from requirements-lock.txt: ${lock_name}==${expected_version}"
    else
      echo "  - direct dep version mismatch: ${lock_name}==${expected_version} (lock has ${locked_version})"
    fi
    echo "${lock_name}|${expected_version}|${locked_version}" >> "$LOCKFILE_RESULTS"
  done < "$LOCKFILE_RAW"
else
  mark_scan_incomplete "Lockfile freshness validation failed; inspect its error above."
fi
rm -f "$LOCKFILE_RAW"

LOCKFILE_COUNT="$(wc -l < "$LOCKFILE_RESULTS" 2>/dev/null | tr -d ' ')"
[ -z "$LOCKFILE_COUNT" ] && LOCKFILE_COUNT=0

# ---------------------------------------------------------------------------
# Accelerator catalog and Karpenter NodePools
#
# The deterministic check always runs and validates the reviewed catalog
# against every NodePool plus the exact cdk.json capacity-history watch list.
# With AWS credentials, the monthly job also compares the catalog against the
# union of NVIDIA GPU / AWS Neuron types returned across enabled commercial
# Regions. Ordinary policy/catalog drift joins the rolling dependency issue;
# execution or parser failures become one operational finding, never a
# false-clean result.
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking accelerator catalog and Karpenter NodePools ==="

ACCELERATOR_OFFLINE_REPORT="$(mktemp)"
ACCELERATOR_ONLINE_REPORT="$(mktemp)"
ACCELERATOR_ONLINE_SUMMARY="$(mktemp)"
ACCELERATOR_OFFLINE_ERROR="$(mktemp)"
ACCELERATOR_ONLINE_ERROR="$(mktemp)"
ACCELERATOR_OFFLINE_COUNT=0
ACCELERATOR_ONLINE_COUNT=0
ACCELERATOR_SKIP_REASON=""
ACCELERATOR_SUMMARY_SKIP_REASON=""

write_accelerator_operational_report() {
  local report_path="$1" title="$2" detail="$3" error_path="$4"
  {
    echo "## ${title}"
    echo ""
    echo "**Status: OPERATIONAL ERROR.**"
    echo ""
    echo "### Accelerator maintenance check could not complete"
    echo ""
    echo "- **Why:** ${detail}"
    echo "- **Recommended change:** Re-run the command locally, repair the tool or credentials, and do not treat this scan as current until it succeeds."
    if [ -s "$error_path" ]; then
      echo "- **Tool output:**"
      sed 's/^/    /' "$error_path"
    fi
  } > "$report_path"
}

record_accelerator_operational_error() {
  local report_path="$1" title="$2" detail="$3" error_path="$4"
  write_accelerator_operational_report "$report_path" "$title" "$detail" "$error_path"
  mark_scan_incomplete "${title}: ${detail}"
}

python3 scripts/accelerator_catalog.py validate \
  --format markdown \
  --output "$ACCELERATOR_OFFLINE_REPORT" \
  2>"$ACCELERATOR_OFFLINE_ERROR"
ACCELERATOR_OFFLINE_STATUS=$?
if [ "$ACCELERATOR_OFFLINE_STATUS" -eq 0 ]; then
  echo "  Offline NodePool/watch-list policy is current."
elif [ "$ACCELERATOR_OFFLINE_STATUS" -eq 1 ]; then
  ACCELERATOR_OFFLINE_COUNT="$(grep -c '^### ' "$ACCELERATOR_OFFLINE_REPORT" || true)"
  if ! [[ "$ACCELERATOR_OFFLINE_COUNT" =~ ^[1-9][0-9]*$ ]]; then
    ACCELERATOR_OFFLINE_COUNT=1
    record_accelerator_operational_error \
      "$ACCELERATOR_OFFLINE_REPORT" \
      "Offline accelerator catalog validation" \
      "The validator reported drift but emitted no parseable actionable findings." \
      "$ACCELERATOR_OFFLINE_ERROR"
  else
    echo "  Found ${ACCELERATOR_OFFLINE_COUNT} offline accelerator policy finding(s)."
  fi
else
  ACCELERATOR_OFFLINE_COUNT=1
  record_accelerator_operational_error \
    "$ACCELERATOR_OFFLINE_REPORT" \
    "Offline accelerator catalog validation" \
    "The deterministic validator exited with status ${ACCELERATOR_OFFLINE_STATUS}." \
    "$ACCELERATOR_OFFLINE_ERROR"
  echo "  Offline accelerator validator failed operationally."
fi

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  ACCELERATOR_SKIP_REASON="No AWS credentials available for the online EC2 catalog check (needs ec2:DescribeRegions and ec2:DescribeInstanceTypes); offline policy validation still ran."
  echo "  $ACCELERATOR_SKIP_REASON"
else
  python3 scripts/accelerator_catalog.py check-online \
    --report "$ACCELERATOR_ONLINE_REPORT" \
    --json-summary \
    >"$ACCELERATOR_ONLINE_SUMMARY" \
    2>"$ACCELERATOR_ONLINE_ERROR"
  ACCELERATOR_ONLINE_STATUS=$?
  if [ "$ACCELERATOR_ONLINE_STATUS" -eq 0 ] || [ "$ACCELERATOR_ONLINE_STATUS" -eq 1 ]; then
    if ACCELERATOR_ONLINE_COUNT="$(parse_accelerator_drift_count "$ACCELERATOR_ONLINE_SUMMARY")"; then
      if { [ "$ACCELERATOR_ONLINE_STATUS" -eq 0 ] && [ "$ACCELERATOR_ONLINE_COUNT" -ne 0 ]; } \
         || { [ "$ACCELERATOR_ONLINE_STATUS" -eq 1 ] && [ "$ACCELERATOR_ONLINE_COUNT" -eq 0 ]; }; then
        ACCELERATOR_ONLINE_COUNT=1
        record_accelerator_operational_error \
          "$ACCELERATOR_ONLINE_REPORT" \
          "Online EC2 accelerator catalog drift" \
          "The command exit status disagreed with its JSON drift summary." \
          "$ACCELERATOR_ONLINE_ERROR"
        echo "  Online accelerator scan returned an inconsistent result."
      elif [ "$ACCELERATOR_ONLINE_COUNT" -eq 0 ]; then
        echo "  Live EC2 accelerator catalog is current."
      else
        echo "  Found ${ACCELERATOR_ONLINE_COUNT} live EC2 catalog drift finding(s)."
      fi
    else
      ACCELERATOR_ONLINE_COUNT=1
      record_accelerator_operational_error \
        "$ACCELERATOR_ONLINE_REPORT" \
        "Online EC2 accelerator catalog drift" \
        "The online scanner emitted a missing or malformed JSON summary." \
        "$ACCELERATOR_ONLINE_ERROR"
      echo "  Online accelerator scan summary could not be parsed."
    fi
  else
    ACCELERATOR_ONLINE_COUNT=1
    record_accelerator_operational_error \
      "$ACCELERATOR_ONLINE_REPORT" \
      "Online EC2 accelerator catalog drift" \
      "The online scanner exited with status ${ACCELERATOR_ONLINE_STATUS}." \
      "$ACCELERATOR_ONLINE_ERROR"
    echo "  Online accelerator scanner failed operationally."
  fi
fi

ACCELERATOR_COUNT=$((ACCELERATOR_OFFLINE_COUNT + ACCELERATOR_ONLINE_COUNT))
if [ -n "$ACCELERATOR_SKIP_REASON" ] && [ "$ACCELERATOR_COUNT" -eq 0 ]; then
  ACCELERATOR_SUMMARY_SKIP_REASON="$ACCELERATOR_SKIP_REASON"
fi

# ---------------------------------------------------------------------------
# Summary + Markdown report
# ---------------------------------------------------------------------------
echo ""
echo "=== Summary ==="
echo "Python packages outdated: $PYTHON_COUNT"
echo "Docker images outdated:   $DOCKER_COUNT"
echo "Helm charts outdated:     $HELM_COUNT"
if [ -n "$ADDON_SKIP_REASON" ]; then
  echo "EKS add-ons outdated:     (skipped)"
else
  echo "EKS add-ons outdated:     $ADDON_COUNT"
fi
if [ -n "$EKS_K8S_SKIP_REASON" ]; then
  echo "EKS Kubernetes version:   (skipped)"
else
  echo "EKS Kubernetes version:   $EKS_K8S_COUNT"
fi
if [ -n "$AURORA_SKIP_REASON" ]; then
  echo "Aurora PostgreSQL:        (skipped)"
else
  echo "Aurora PostgreSQL:        $AURORA_COUNT"
fi
if [ -n "$EMR_SKIP_REASON" ]; then
  echo "EMR Serverless release:   (skipped)"
else
  echo "EMR Serverless release:   $EMR_COUNT"
fi
if [ -n "$BEDROCK_MODEL_SKIP_REASON" ]; then
  echo "Bedrock default model:    (skipped)"
else
  echo "Bedrock default model:    $BEDROCK_MODEL_COUNT"
fi
if [ -n "$ACCELERATOR_SKIP_REASON" ]; then
  echo "Accelerator catalog:      ${ACCELERATOR_COUNT} (online skipped)"
else
  echo "Accelerator catalog:      $ACCELERATOR_COUNT"
fi
echo "Dockerfile.dev pins:      $DOCKERFILE_COUNT"
echo "Pre-commit hooks:         $PRECOMMIT_COUNT"
if [ -n "$CDK_ENUM_SKIP_REASON" ]; then
  echo "CDK enum constants:       (skipped)"
else
  echo "CDK enum constants:       $CDK_ENUM_COUNT"
fi
if [ -n "$PYTHON_RELEASE_SKIP_REASON" ]; then
  echo "Python release:           (skipped)"
else
  echo "Python release:           $PYTHON_RELEASE_COUNT"
fi
if [ -n "$AUTOPILOT_SKIP_REASON" ]; then
  echo "GCO autopilot pins:       (skipped)"
else
  echo "GCO autopilot pins:       $AUTOPILOT_COUNT"
fi
echo "CI tooling pins:          $CI_TOOLING_COUNT"
echo "Version consistency:      $CONSISTENCY_COUNT"
echo "Base-image epochs:        $EPOCH_COUNT"
echo "Suppression expiries:     $SUPPRESSION_COUNT"
echo "Lockfile freshness:       $LOCKFILE_COUNT"
INCOMPLETE_LOOKUP_COUNT="$(sort -u "$INCOMPLETE_REASONS_FILE" 2>/dev/null | grep -c . || true)"
echo "Incomplete lookups:       ${INCOMPLETE_LOOKUP_COUNT:-0}"

SCAN_COMPLETE=true
if ! dependency_scan_is_complete \
  "$INCOMPLETE_REASONS_FILE" \
  "$ADDON_SKIP_REASON" \
  "$EKS_K8S_SKIP_REASON" \
  "$AURORA_SKIP_REASON" \
  "$EMR_SKIP_REASON" \
  "$BEDROCK_MODEL_SKIP_REASON" \
  "$ACCELERATOR_SKIP_REASON" \
  "$AUTOPILOT_SKIP_REASON" \
  "$CDK_ENUM_SKIP_REASON" \
  "$PYTHON_RELEASE_SKIP_REASON"; then
  SCAN_COMPLETE=false
fi

if [ "$PYTHON_COUNT" -eq 0 ] && [ "$NPM_COUNT" -eq 0 ] && [ "$DOCKER_COUNT" -eq 0 ] \
   && [ "$HELM_COUNT" -eq 0 ] && [ "$ADDON_COUNT" -eq 0 ] \
   && [ "$EKS_K8S_COUNT" -eq 0 ] \
   && [ "$AURORA_COUNT" -eq 0 ] && [ "$EMR_COUNT" -eq 0 ] \
   && [ "$DOCKERFILE_COUNT" -eq 0 ] \
   && [ "$AUTOPILOT_COUNT" -eq 0 ] \
   && [ "$PRECOMMIT_COUNT" -eq 0 ] \
   && [ "$CDK_ENUM_COUNT" -eq 0 ] \
   && [ "$PYTHON_RELEASE_COUNT" -eq 0 ] \
   && [ "$BEDROCK_MODEL_COUNT" -eq 0 ] \
   && [ "$ACCELERATOR_COUNT" -eq 0 ] \
   && [ "$CI_TOOLING_COUNT" -eq 0 ] \
   && [ "$CONSISTENCY_COUNT" -eq 0 ] \
   && [ "$EPOCH_COUNT" -eq 0 ] \
   && [ "$SUPPRESSION_COUNT" -eq 0 ] \
   && [ "$LOCKFILE_COUNT" -eq 0 ]; then
  echo ""
  SKIP_NOTES=""
  if [ -n "$ADDON_SKIP_REASON" ]; then
    SKIP_NOTES="EKS add-ons skipped: $ADDON_SKIP_REASON"
  fi
  if [ -n "$EKS_K8S_SKIP_REASON" ]; then
    [ -n "$SKIP_NOTES" ] && SKIP_NOTES="$SKIP_NOTES; "
    SKIP_NOTES="${SKIP_NOTES}EKS Kubernetes skipped: $EKS_K8S_SKIP_REASON"
  fi
  if [ -n "$AURORA_SKIP_REASON" ]; then
    [ -n "$SKIP_NOTES" ] && SKIP_NOTES="$SKIP_NOTES; "
    SKIP_NOTES="${SKIP_NOTES}Aurora engine skipped: $AURORA_SKIP_REASON"
  fi
  if [ -n "$EMR_SKIP_REASON" ]; then
    [ -n "$SKIP_NOTES" ] && SKIP_NOTES="$SKIP_NOTES; "
    SKIP_NOTES="${SKIP_NOTES}EMR Serverless skipped: $EMR_SKIP_REASON"
  fi
  if [ -n "$BEDROCK_MODEL_SKIP_REASON" ]; then
    [ -n "$SKIP_NOTES" ] && SKIP_NOTES="$SKIP_NOTES; "
    SKIP_NOTES="${SKIP_NOTES}Bedrock model skipped: $BEDROCK_MODEL_SKIP_REASON"
  fi
  if [ -n "$ACCELERATOR_SKIP_REASON" ]; then
    [ -n "$SKIP_NOTES" ] && SKIP_NOTES="$SKIP_NOTES; "
    SKIP_NOTES="${SKIP_NOTES}Online accelerator catalog skipped: $ACCELERATOR_SKIP_REASON"
  fi
  if [ -n "$AUTOPILOT_SKIP_REASON" ]; then
    [ -n "$SKIP_NOTES" ] && SKIP_NOTES="$SKIP_NOTES; "
    SKIP_NOTES="${SKIP_NOTES}GCO autopilot pins skipped: $AUTOPILOT_SKIP_REASON"
  fi
  if [ -n "$CDK_ENUM_SKIP_REASON" ]; then
    [ -n "$SKIP_NOTES" ] && SKIP_NOTES="$SKIP_NOTES; "
    SKIP_NOTES="${SKIP_NOTES}CDK enums skipped: $CDK_ENUM_SKIP_REASON"
  fi
  if [ -n "$PYTHON_RELEASE_SKIP_REASON" ]; then
    [ -n "$SKIP_NOTES" ] && SKIP_NOTES="$SKIP_NOTES; "
    SKIP_NOTES="${SKIP_NOTES}Python release skipped: $PYTHON_RELEASE_SKIP_REASON"
  fi
  if [ -s "$INCOMPLETE_REASONS_FILE" ]; then
    [ -n "$SKIP_NOTES" ] && SKIP_NOTES="$SKIP_NOTES; "
    SKIP_NOTES="${SKIP_NOTES}Incomplete checks: $(join_scan_incomplete_reasons)"
  fi
  if [ "$SCAN_COMPLETE" != true ]; then
    STATUS_MESSAGE="No drift was found in completed checks, but the scan is incomplete."
  else
    STATUS_MESSAGE="All dependencies are up to date."
  fi
  echo "$STATUS_MESSAGE"
  rm -f "$NPM_RESULTS" "$DOCKER_RESULTS" "$HELM_RESULTS" "$ADDON_RESULTS" "$EKS_K8S_RESULTS" "$AURORA_RESULTS" "$EMR_RESULTS" "$DOCKERFILE_RESULTS" "$AUTOPILOT_RESULTS" "$PRECOMMIT_RESULTS" "$CDK_ENUM_RESULTS" "$PYTHON_RELEASE_RESULTS" "$BEDROCK_MODEL_RESULTS" "$CI_TOOLING_RESULTS" "$CONSISTENCY_RESULTS" "$EPOCH_RESULTS" "$SUPPRESSION_RESULTS" "$LOCKFILE_RESULTS" "$ACCELERATOR_OFFLINE_REPORT" "$ACCELERATOR_ONLINE_REPORT" "$ACCELERATOR_ONLINE_SUMMARY" "$ACCELERATOR_OFFLINE_ERROR" "$ACCELERATOR_ONLINE_ERROR" "$INCOMPLETE_REASONS_FILE"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
      echo "# Dependency Update Report"
      echo ""
      echo "$STATUS_MESSAGE"
      if [ -n "$SKIP_NOTES" ]; then
        echo ""
        echo "_Incomplete or skipped checks: ${SKIP_NOTES}_"
      fi
    } >> "$GITHUB_STEP_SUMMARY"
  fi
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
      echo "has_drift=false"
      echo "scan_complete=$SCAN_COMPLETE"
    } >> "$GITHUB_OUTPUT"
  fi
  exit 0
fi

# summary_row <title> <count> <skip_reason> <urgency>
# Emits one row of the top-of-report TL;DR table. Surfaces with drift link to
# their detailed section; skipped surfaces are marked and get no urgency.
summary_row() {
  local title="$1" count="$2" skip="$3" urgency="$4"
  local anchor label
  anchor="$(md_anchor "$title")"
  if [ -n "$skip" ]; then
    label="skipped"
    urgency="—"
  elif [ "$count" -gt 0 ]; then
    label="${count} update(s)"
    title="[${title}](#${anchor})"
  elif [ "$SCAN_COMPLETE" != true ]; then
    label="no drift found (incomplete scan)"
    urgency="—"
  else
    label="up to date"
    urgency="—"
  fi
  echo "| ${title} | ${label} | ${urgency} |"
}

{
  echo "# Dependency Update Report"
  echo ""
  echo "_Generated $(date -u '+%Y-%m-%d %H:%M UTC') by the \`deps-scan\` workflow._"
  echo ""
  if [ "$SCAN_COMPLETE" != true ]; then
    echo "> [!WARNING]"
    echo "> **Incomplete scan.** Zero-count surfaces are provisional, not confirmed current."
    if [ -s "$INCOMPLETE_REASONS_FILE" ]; then
      echo "> Recorded failures: $(join_scan_incomplete_reasons)"
    else
      echo "> One or more credential-dependent or optional checks were skipped; see the workflow log."
    fi
    echo ""
  fi

  # ----- TL;DR summary -----
  echo "## Summary"
  echo ""
  echo "| Surface | Status | Urgency |"
  echo "|---------|--------|---------|"
  summary_row "Python Packages"          "$PYTHON_COUNT"         ""                          "routine"
  summary_row "npm Packages"             "$NPM_COUNT"            ""                          "routine"
  summary_row "Docker Images"            "$DOCKER_COUNT"         ""                          "routine"
  summary_row "Helm Charts"              "$HELM_COUNT"           ""                          "routine"
  summary_row "EKS Add-ons"              "$ADDON_COUNT"          "$ADDON_SKIP_REASON"        "routine"
  summary_row "EKS Kubernetes Version"   "$EKS_K8S_COUNT"        "$EKS_K8S_SKIP_REASON"      "act soon"
  summary_row "Aurora PostgreSQL Engine" "$AURORA_COUNT"         "$AURORA_SKIP_REASON"       "routine"
  summary_row "EMR Serverless"           "$EMR_COUNT"            "$EMR_SKIP_REASON"          "routine"
  summary_row "Bedrock Default Model"    "$BEDROCK_MODEL_COUNT"  "$BEDROCK_MODEL_SKIP_REASON" "routine"
  summary_row "Accelerator Catalog and NodePools" "$ACCELERATOR_COUNT" "$ACCELERATOR_SUMMARY_SKIP_REASON" "act soon"
  summary_row "Dockerfile.dev Pins"      "$DOCKERFILE_COUNT"     ""                          "routine"
  summary_row "GCO Autopilot Pins"       "$AUTOPILOT_COUNT"      "$AUTOPILOT_SKIP_REASON"    "act soon"
  summary_row "Pre-commit Hooks"         "$PRECOMMIT_COUNT"      ""                          "routine"
  summary_row "CDK Enum Constants"       "$CDK_ENUM_COUNT"       "$CDK_ENUM_SKIP_REASON"     "routine"
  summary_row "Python Release"           "$PYTHON_RELEASE_COUNT" "$PYTHON_RELEASE_SKIP_REASON" "informational"
  summary_row "CI Tooling"               "$CI_TOOLING_COUNT"     ""                          "act soon"
  summary_row "Version Consistency"      "$CONSISTENCY_COUNT"    ""                          "routine"
  summary_row "Base-image Security Epochs" "$EPOCH_COUNT"        ""                          "act soon"
  summary_row "Suppression Expiries"     "$SUPPRESSION_COUNT"    ""                          "act soon"
  summary_row "Lockfile Freshness"       "$LOCKFILE_COUNT"       ""                          "routine"
  echo ""
  echo "_Urgency is a hint: **act soon** = security or a support/cost deadline;"
  echo "**routine** = bump at leisure; **informational** = no action yet. Only"
  echo "surfaces with drift have a detailed section below._"
  echo ""

  if [ "$PYTHON_COUNT" -gt 0 ]; then
    echo "## Python Packages"
    echo ""
    echo "Direct dependencies pinned in \`pyproject.toml\` (transitive-only drift"
    echo "is excluded — those versions are controlled by upstream pins and bumping"
    echo "them ourselves either no-ops or breaks the resolver)."
    echo ""
    echo "| Package | Current | Latest | Ref |"
    echo "|---------|---------|--------|-----|"
    echo "$PYTHON_OUTDATED" | jq -r '.[] | "| \(.name) | \(.version) | \(.latest_version) | [PyPI](https://pypi.org/project/\(.name)/) |"'
    echo ""
  fi

  if [ "$NPM_COUNT" -gt 0 ]; then
    echo "## npm Packages"
    echo ""
    echo "Exact direct pins in every repository-owned \`package.json\` (the root"
    echo "tooling graph and \`lambda/inference-streaming-proxy\`), compared against"
    echo "each package's \`latest\` dist-tag. Transitives are excluded — those are"
    echo "controlled by the lockfiles. Bump the pin, then regenerate the graph's"
    echo "\`package-lock.json\` with the pinned npm."
    echo ""
    npm_disp="$(mktemp)"
    while IFS='|' read -r graph pkg cur lat; do
      echo "\`${graph}\`|${pkg}|${cur}|${lat}|[npm](https://www.npmjs.com/package/${pkg})"
    done < "$NPM_RESULTS" > "$npm_disp"
    emit_md_table "Graph|Package|Current|Latest|Ref" "$npm_disp"
    rm -f "$npm_disp"
    echo ""
  fi

  if [ "$DOCKER_COUNT" -gt 0 ]; then
    echo "## Docker Images"
    echo ""
    emit_md_table "Image|Current|Latest" "$DOCKER_RESULTS"
    echo ""
  fi

  if [ "$HELM_COUNT" -gt 0 ]; then
    echo "## Helm Charts"
    echo ""
    helm_disp="$(mktemp)"
    while IFS='|' read -r cname chart cur lat; do
      echo "${cname}|${chart}|${cur}|${lat}|[ArtifactHub](https://artifacthub.io/packages/search?ts_query_web=${chart})"
    done < "$HELM_RESULTS" > "$helm_disp"
    emit_md_table "Chart|Name|Current|Latest|Ref" "$helm_disp"
    rm -f "$helm_disp"
    echo ""
  fi

  if [ "$ADDON_COUNT" -gt 0 ]; then
    echo "## EKS Add-ons"
    echo ""
    emit_md_table "Add-on|Current|Latest" "$ADDON_RESULTS"
    echo ""
  fi

  if [ "$EKS_K8S_COUNT" -gt 0 ]; then
    echo "## EKS Kubernetes Version"
    echo ""
    echo "The Kubernetes minor pinned in \`cdk.json::kubernetes_version\` is behind"
    echo "the latest release still in EKS **standard support**. Upgrade before"
    echo "standard support ends to avoid the extended-support pricing uplift."
    echo ""
    eks_disp="$(mktemp)"
    while IFS='|' read -r pin cur lat eos; do
      echo "\`${pin}\`|${cur}|${lat}|${eos}"
    done < "$EKS_K8S_RESULTS" > "$eks_disp"
    emit_md_table "Pin|Current|Latest (standard support)|Std support ends" "$eks_disp"
    rm -f "$eks_disp"
    echo ""
  fi

  if [ "$AURORA_COUNT" -gt 0 ]; then
    echo "## Aurora PostgreSQL Engine"
    echo ""
    emit_md_table "Engine|Current|Latest" "$AURORA_RESULTS"
    echo ""
  fi

  if [ "$EMR_COUNT" -gt 0 ]; then
    echo "## EMR Serverless"
    echo ""
    emit_md_table "Release|Current|Latest" "$EMR_RESULTS"
    echo ""
  fi

  if [ "$BEDROCK_MODEL_COUNT" -gt 0 ]; then
    echo "## Bedrock Default Model"
    echo ""
    echo "A Bedrock model default configured in \`cdk.json\` is behind a newer"
    echo "release in the same model family. For \`bedrock.mission_default_model_id\`,"
    echo "update the value and re-capture the scaffold fixture"
    echo "(\`scripts/capture_scaffold_fixtures.py\`). For"
    echo "\`bedrock.capacity_advisor_default_model_id\` and"
    echo "\`bedrock.claude_code_default_model_id\`, updating the value is enough."
    echo "For the embedding keys — \`bedrock.embedding_model_id\` (Mission memory)"
    echo "and \`vector_store.embedding_model_id\` (workload RAG corpus) — stored"
    echo "vectors are only comparable to vectors from the same model: plan to"
    echo "re-embed (for the vector store, re-ingest the corpus) or segregate"
    echo "existing data before adopting the newer model."
    echo ""
    emit_md_table "Configuration key|Current|Latest" "$BEDROCK_MODEL_RESULTS"
    echo ""
  fi

  if [ "$ACCELERATOR_COUNT" -gt 0 ]; then
    echo "## Accelerator Catalog and NodePools"
    echo ""
    echo "The offline check keeps reviewed lifecycle/generation policy, Karpenter"
    echo "NodePools, \`historical.watch_instance_types\`, and the Spot Placement"
    echo "Score instance pools synchronized. The online check compares the catalog"
    echo "with EC2 across enabled commercial Regions."
    echo "Follow each recommended change; review family metadata before refreshing"
    echo "the checked-in catalog."
    echo ""
    if [ -s "$ACCELERATOR_OFFLINE_REPORT" ]; then
      sed -E 's/^### /#### /; s/^## /### /' "$ACCELERATOR_OFFLINE_REPORT"
      echo ""
    fi
    if [ -s "$ACCELERATOR_ONLINE_REPORT" ]; then
      sed -E 's/^### /#### /; s/^## /### /' "$ACCELERATOR_ONLINE_REPORT"
      echo ""
    fi
  fi

  if [ "$DOCKERFILE_COUNT" -gt 0 ]; then
    echo "## Dockerfile.dev Pins"
    echo ""
    echo "Tooling versions pinned as build-time ARGs in \`Dockerfile.dev\`."
    echo ""
    dockerfile_disp="$(mktemp)"
    while IFS='|' read -r pin cur lat; do
      echo "\`${pin}\`|${cur}|${lat}"
    done < "$DOCKERFILE_RESULTS" > "$dockerfile_disp"
    emit_md_table "Pin|Current|Latest" "$dockerfile_disp"
    rm -f "$dockerfile_disp"
    echo ""
  fi

  if [ "$AUTOPILOT_COUNT" -gt 0 ]; then
    echo "## GCO Autopilot Pins"
    echo ""
    echo "\`gco autopilot\`'s dependency surfaces in \`cli/autopilot.py\`: the"
    echo "pinned \`CLAUDE_CODE_VERSION\` it installs (compared against the npm"
    echo "\`latest\` dist-tag) and the launch-time companion MCP servers"
    echo "(reported when a package is missing, deprecated, or yanked on its"
    echo "registry — an unhealthy companion breaks every new session, so treat"
    echo "it like the removals documented in \`gco_mcp/README.md\`). When"
    echo "changing the companion set, update \`cli/autopilot.py\` and the"
    echo "\`gco_mcp/README.md\` tables together; \`tests/test_cli_autopilot.py\`"
    echo "enforces the lockstep."
    echo ""
    autopilot_disp="$(mktemp)"
    while IFS='|' read -r surface cur stat url; do
      echo "${surface}|\`${cur}\`|${stat}|[registry](${url})"
    done < "$AUTOPILOT_RESULTS" > "$autopilot_disp"
    emit_md_table "Surface|Current|Latest / status|Ref" "$autopilot_disp"
    rm -f "$autopilot_disp"
    echo ""
  fi

  if [ "$PRECOMMIT_COUNT" -gt 0 ]; then
    echo "## Pre-commit Hooks"
    echo ""
    echo "Hook \`rev:\` pins in \`.pre-commit-config.yaml\` are behind the latest"
    echo "tag published by their upstream repos. Bump in \`.pre-commit-config.yaml\`,"
    echo "then run \`pre-commit autoupdate\` locally (or edit by hand) and verify"
    echo "the hooks still pass."
    echo ""
    precommit_disp="$(mktemp)"
    while IFS='|' read -r repo cur lat; do
      echo "${repo}|\`${cur}\`|\`${lat}\`|[releases](${repo}/releases)"
    done < "$PRECOMMIT_RESULTS" > "$precommit_disp"
    emit_md_table "Repo|Current|Latest|Ref" "$precommit_disp"
    rm -f "$precommit_disp"
    echo ""
  fi

  if [ "$CDK_ENUM_COUNT" -gt 0 ]; then
    echo "## CDK Enum Constants"
    echo ""
    echo "Enum-name constants in \`gco/stacks/constants.py\` are behind the highest"
    echo "enum member exposed by the installed \`aws-cdk-lib\`. Update the constant"
    echo "in \`constants.py\` (and any related deployment notes) so new stacks"
    echo "construct the latest CDK enum."
    echo ""
    emit_md_table "Constant|CDK enum class|Current|Latest" "$CDK_ENUM_RESULTS" code
    echo ""
  fi

  if [ "$PYTHON_RELEASE_COUNT" -gt 0 ]; then
    echo "## Python Release"
    echo ""
    echo "A newer stable Python release is available on python.org than the version"
    echo "encoded by \`LAMBDA_PYTHON_RUNTIME\`. AWS Lambda may lag the upstream"
    echo "release by a few months — wait for the matching \`Runtime.PYTHON_X_Y\`"
    echo "enum to appear in \`aws-cdk-lib\` (tracked by the **CDK Enum Constants**"
    echo "section above) before bumping. See <https://www.python.org/downloads/>."
    echo ""
    emit_md_table "Surface|Current|Latest" "$PYTHON_RELEASE_RESULTS"
    echo ""
  fi

  # ----- New coverage surfaces -----

  if [ "$CI_TOOLING_COUNT" -gt 0 ]; then
    echo "## CI Tooling"
    echo ""
    echo "Tool versions the workflows install by hand — not \`uses:\` refs or"
    echo "Dockerfile \`FROM\` lines, so Dependabot doesn't watch them. A stale"
    echo "**Trivy** in particular means the CVE scan silently misses newer"
    echo "detections; bump the \`*_VERSION\` env / kind-action inputs in lockstep."
    echo ""
    ci_disp="$(mktemp)"
    while IFS='|' read -r name cur lat url; do
      echo "${name}|\`${cur}\`|\`${lat}\`|[releases](${url})"
    done < "$CI_TOOLING_RESULTS" > "$ci_disp"
    emit_md_table "Tool|Current|Latest|Ref" "$ci_disp"
    rm -f "$ci_disp"
    echo ""
  fi

  if [ "$CONSISTENCY_COUNT" -gt 0 ]; then
    echo "## Version Consistency"
    echo ""
    echo "These versions and dependency-management surfaces must move together."
    echo "The rows below identify missing coverage or disagreement across runtime,"
    echo "package, dev-container, pre-commit, and CI pins."
    echo ""
    emit_md_table "What|Pinned values" "$CONSISTENCY_RESULTS"
    echo ""
  fi

  if [ "$EPOCH_COUNT" -gt 0 ]; then
    echo "## Base-image Security Epochs"
    echo ""
    echo "The build-time \`*_SECURITY_EPOCH\` ARGs bust the CI layer cache so a"
    echo "fresh OS-security-upgrade layer is built. An epoch older than"
    echo "${SECURITY_EPOCH_STALE_DAYS} days may be reusing a stale upgrade layer —"
    echo "bump the date to force a rebuild that pulls current patches."
    echo ""
    epoch_disp="$(mktemp)"
    while IFS='|' read -r df arg edate eage; do
      echo "\`${df}\`|\`${arg}\`|${edate}|${eage}"
    done < "$EPOCH_RESULTS" > "$epoch_disp"
    emit_md_table "Dockerfile|ARG|Epoch|Age (days)" "$epoch_disp"
    rm -f "$epoch_disp"
    echo ""
  fi

  if [ "$SUPPRESSION_COUNT" -gt 0 ]; then
    echo "## Suppression Expiries"
    echo ""
    echo "\`.trivyignore\` / \`.pip-audit-ignore\` / \`.npm-audit-ignore\` entries"
    echo "expiring within ${SUPPRESSION_EXPIRY_WARN_DAYS} days (the CI validator hard-fails a PR on"
    echo "the expiry date). Re-evaluate each: drop it if the CVE is fixed upstream,"
    echo "or extend with a fresh justification if not."
    echo ""
    sup_disp="$(mktemp)"
    while IFS='|' read -r sbase sid sdate sleft; do
      echo "\`${sbase}\`|\`${sid}\`|${sdate}|${sleft}"
    done < "$SUPPRESSION_RESULTS" > "$sup_disp"
    emit_md_table "File|ID|Expires|Days left" "$sup_disp"
    rm -f "$sup_disp"
    echo ""
  fi

  if [ "$LOCKFILE_COUNT" -gt 0 ]; then
    echo "## Lockfile Freshness"
    echo ""
    echo "Direct dependencies whose exact pyproject.toml pin is missing from or"
    echo "different in requirements-lock.txt. Regenerate the lock with"
    echo "pip-compile --all-extras --strip-extras -o requirements-lock.txt pyproject.toml."
    echo ""
    emit_md_table "Dependency|Expected|Locked" "$LOCKFILE_RESULTS" code
    echo ""
  fi

  # ----- Skipped checks (collapsed) -----
  if [ -n "${ADDON_SKIP_REASON}${EKS_K8S_SKIP_REASON}${AURORA_SKIP_REASON}${EMR_SKIP_REASON}${BEDROCK_MODEL_SKIP_REASON}${ACCELERATOR_SKIP_REASON}${AUTOPILOT_SKIP_REASON}${CDK_ENUM_SKIP_REASON}${PYTHON_RELEASE_SKIP_REASON}" ] \
     || [ -s "$INCOMPLETE_REASONS_FILE" ]; then
    echo "<details>"
    echo "<summary>Skipped checks</summary>"
    echo ""
    [ -n "$ADDON_SKIP_REASON" ]         && echo "- **EKS Add-ons:** $ADDON_SKIP_REASON"
    [ -n "$EKS_K8S_SKIP_REASON" ]       && echo "- **EKS Kubernetes Version:** $EKS_K8S_SKIP_REASON"
    [ -n "$AURORA_SKIP_REASON" ]        && echo "- **Aurora PostgreSQL Engine:** $AURORA_SKIP_REASON"
    [ -n "$EMR_SKIP_REASON" ]           && echo "- **EMR Serverless:** $EMR_SKIP_REASON"
    [ -n "$BEDROCK_MODEL_SKIP_REASON" ] && echo "- **Bedrock Default Model:** $BEDROCK_MODEL_SKIP_REASON"
    [ -n "$ACCELERATOR_SKIP_REASON" ]   && echo "- **Online Accelerator Catalog:** $ACCELERATOR_SKIP_REASON"
    [ -n "$AUTOPILOT_SKIP_REASON" ]     && echo "- **GCO Autopilot Pins:** $AUTOPILOT_SKIP_REASON"
    [ -n "$CDK_ENUM_SKIP_REASON" ]      && echo "- **CDK Enum Constants:** $CDK_ENUM_SKIP_REASON"
    [ -n "$PYTHON_RELEASE_SKIP_REASON" ] && echo "- **Python Release:** $PYTHON_RELEASE_SKIP_REASON"
    if [ -s "$INCOMPLETE_REASONS_FILE" ]; then
      while IFS= read -r incomplete_reason; do
        echo "- **Incomplete lookup or parse:** ${incomplete_reason}"
      done < <(sort -u "$INCOMPLETE_REASONS_FILE")
    fi
    echo ""
    echo "</details>"
    echo ""
  fi

  echo "## Action Required"
  echo ""
  echo "1. Review changelogs for breaking changes (see the per-row **Ref** links)"
  echo "2. Follow accelerator findings exactly; review lifecycle/generation metadata"
  echo "   before running \`python scripts/accelerator_catalog.py refresh\`"
  echo "3. Update versions in \`pyproject.toml\`, manifests, \`charts.yaml\`, or the"
  echo "   pinned \`*_VERSION\` env / ARG values"
  echo "4. Regenerate \`requirements-lock.txt\` if Python deps changed"
  echo "5. Reconcile any **Version Consistency** rows so every copy of a pin agrees"
  echo "6. Run tests locally to verify compatibility, then open a PR"
  echo ""
  echo "---"
  echo "_Automatically created by the \`deps-scan\` workflow._"
} > "$REPORT_FILE"

rm -f "$NPM_RESULTS" "$DOCKER_RESULTS" "$HELM_RESULTS" "$ADDON_RESULTS" "$EKS_K8S_RESULTS" "$AURORA_RESULTS" "$EMR_RESULTS" "$DOCKERFILE_RESULTS" "$AUTOPILOT_RESULTS" "$PRECOMMIT_RESULTS" "$CDK_ENUM_RESULTS" "$PYTHON_RELEASE_RESULTS" "$BEDROCK_MODEL_RESULTS" "$CI_TOOLING_RESULTS" "$CONSISTENCY_RESULTS" "$EPOCH_RESULTS" "$SUPPRESSION_RESULTS" "$LOCKFILE_RESULTS" "$ACCELERATOR_OFFLINE_REPORT" "$ACCELERATOR_ONLINE_REPORT" "$ACCELERATOR_ONLINE_SUMMARY" "$ACCELERATOR_OFFLINE_ERROR" "$ACCELERATOR_ONLINE_ERROR" "$INCOMPLETE_REASONS_FILE"

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "has_drift=true"
    echo "scan_complete=$SCAN_COMPLETE"
    echo "report_path=$REPORT_FILE"
  } >> "$GITHUB_OUTPUT"
fi

# Mirror the report into the workflow run's job summary so results are visible
# on the Actions run page even for workflow_dispatch runs and regardless of
# whether an issue is opened.
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  cat "$REPORT_FILE" >> "$GITHUB_STEP_SUMMARY"
fi

echo ""
echo "Wrote report to $REPORT_FILE"
