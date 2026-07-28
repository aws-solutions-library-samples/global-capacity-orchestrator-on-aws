#!/usr/bin/env bash
# =============================================================================
# lib_dependency_scan.sh — sourceable functions for dependency-scan.sh
# =============================================================================
# Extracted from dependency-scan.sh so BATS tests can exercise the real logic
# without running the full scan (which needs pip, skopeo, helm, AWS creds).
#
# Usage:
#   source .github/scripts/lib_dependency_scan.sh
# =============================================================================

# extract_direct_python_deps [pyproject_path]
#
# Reads the ``project.dependencies`` list and every list under
# ``project.optional-dependencies`` from ``pyproject.toml`` and prints
# one normalized package name per line (lowercased, ``_`` → ``-`` per
# PEP 503). These are the packages we pin *directly* — everything else
# is a transitive dependency whose version is controlled by something
# we pin, and bumping it ourselves either does nothing (pip resolves
# back to the same version) or breaks the resolver.
#
# Used by the python-drift path in ``dependency-scan.sh`` to filter
# ``pip list --outdated`` down to names the operator can actually act
# on, so the monthly report doesn't flag (for example) ``cattrs`` as
# "outdated" when it's a jsii transitive we have no input on.
#
# Falls back silently to an empty list (prints nothing) if the file
# isn't present or can't be parsed — the caller treats an empty list
# as "no filter applied" rather than "no direct deps" so the scan
# never silently hides genuine drift if the TOML parse breaks.
#
# Requires Python 3.11+ for ``tomllib`` (stdlib). The deps-scan
# workflow already runs on 3.14.
extract_direct_python_deps() {
  local pyproject="${1:-pyproject.toml}"
  [ -f "$pyproject" ] || return 0
  python3 -c "
import re, sys, tomllib
try:
    with open(sys.argv[1], 'rb') as f:
        data = tomllib.load(f)
except Exception:
    sys.exit(0)

project = data.get('project', {}) or {}
deps = list(project.get('dependencies', []) or [])
for group in (project.get('optional-dependencies', {}) or {}).values():
    deps.extend(group or [])

# Drop the project self-reference (``gco-cli[dev]`` etc.) before
# normalising — pip doesn't report it in ``list --outdated`` anyway
# but we also don't want to match on it.
seen = set()
for spec in deps:
    if not isinstance(spec, str):
        continue
    name = re.split(r'[\\[=!<>;~ ]', spec, 1)[0].strip()
    if not name or name.lower() == 'gco-cli':
        continue
    # PEP 503 normalisation: lowercase, ``_`` + ``.`` → ``-``.
    name = re.sub(r'[-_.]+', '-', name).lower()
    if name not in seen:
        seen.add(name)
        print(name)
" "$pyproject" 2>/dev/null
}

# extract_build_system_pins [pyproject_path]
#
# Reads ``[build-system].requires`` and emits one ``name|version|raw``
# line per entry. ``version`` is filled only when the entry is a single
# exact ``==X[.Y[.Z…]]`` pin with no extras, ranges, or markers;
# otherwise it is left empty so the consistency check can flag the
# entry while the PyPI drift check skips it.
#
# The build backend is a Python dependency too, but it is invisible to
# ``pip list --outdated``: pip resolves it inside build isolation, not
# in the scan venv (a Python 3.14 venv does not even ship setuptools).
# Before this extractor existed the backend floated on a ``>=`` range
# with nothing watching it — the exact failure mode the rest of this
# library exists to catch.
#
# Prints nothing for a missing or unparseable file; the consistency
# check treats that as a finding (fail-visible) rather than a pass.
extract_build_system_pins() {
  local pyproject="${1:-pyproject.toml}"
  [ -f "$pyproject" ] || return 0
  python3 -c "
import re, sys, tomllib
try:
    with open(sys.argv[1], 'rb') as f:
        data = tomllib.load(f)
except Exception:
    sys.exit(0)
requires = (data.get('build-system') or {}).get('requires') or []
for raw in requires:
    if not isinstance(raw, str) or not raw.strip():
        continue
    entry = raw.strip()
    exact = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9]+(?:\.[0-9]+)*)', entry)
    name_match = re.match(r'[A-Za-z0-9][A-Za-z0-9._-]*', entry)
    # PEP 503 normalisation: lowercase, ``_`` + ``.`` → ``-``.
    name = re.sub(r'[-_.]+', '-', name_match.group(0)).lower() if name_match else ''
    version = exact.group(2) if exact else ''
    print(f'{name}|{version}|{entry}')
" "$pyproject" 2>/dev/null
}

# parse_image_registry <image>
#
# Given a Docker image name (without tag), prints "registry|repo" where
# registry is the domain and repo is the path within that registry.
#
# Examples:
#   parse_image_registry "nvcr.io/nvidia/cuda"        → "nvcr.io|nvidia/cuda"
#   parse_image_registry "pytorch/pytorch"            → "docker.io|pytorch/pytorch"
#   parse_image_registry "python"                     → "docker.io|library/python"
#   parse_image_registry "public.ecr.aws/eks/coredns" → "public.ecr.aws|eks/coredns"
parse_image_registry() {
  local image="$1"
  local registry="" repo=""
  case "$image" in
    nvcr.io/*|gcr.io/*|quay.io/*|ghcr.io/*|registry.k8s.io/*|public.ecr.aws/*)
      registry="$(echo "$image" | cut -d'/' -f1)"
      repo="$(echo "$image" | cut -d'/' -f2-)"
      ;;
    */*)
      registry="docker.io"
      repo="$image"
      ;;
    *)
      registry="docker.io"
      repo="library/$image"
      ;;
  esac
  echo "${registry}|${repo}"
}

# is_semver_tag <tag>
#
# Returns 0 (true) if the tag looks like a semver version (v1.2.3, 1.2, etc).
# Returns 1 (false) otherwise.
is_semver_tag() {
  echo "$1" | grep -qE "^v?[0-9]+\.[0-9]+(\.[0-9]+)?"
}

# is_project_image <image>
#
# Returns 0 (true) if the image is built by this project (gco/*).
is_project_image() {
  echo "$1" | grep -q "^gco/"
}

# compare_semver <current> <candidate>
#
# Prints "newer" if candidate is strictly newer than current (by sort -V),
# "same" if they're equal, "older" otherwise.
compare_semver() {
  local current="${1#v}"
  local candidate="${2#v}"
  if [ "$current" = "$candidate" ]; then
    echo "same"
    return
  fi
  local newest
  newest="$(printf '%s\n%s' "$current" "$candidate" | sort -V | tail -1)"
  if [ "$newest" = "$candidate" ]; then
    echo "newer"
  else
    echo "older"
  fi
}

# parse_accelerator_drift_count <json-summary-file>
#
# Validates the machine-readable output from
# ``scripts/accelerator_catalog.py check-online --json-summary`` and prints
# its exact non-negative ``drift_count``. The status/count relationship is
# checked as well: ``current`` means zero and ``drift`` means one or more.
#
# Returns non-zero with no output for a missing file, malformed JSON, a
# missing/invalid count, or an inconsistent status. The dependency-scan
# driver turns any such parser failure into one operational finding so a
# broken online scan can never be reported as clean.
parse_accelerator_drift_count() {
  local summary_file="$1"
  [ -f "$summary_file" ] || return 1
  python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as handle:
        summary = json.load(handle)
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
if not isinstance(summary, dict):
    raise SystemExit(1)
count = summary.get('drift_count')
status = summary.get('status')
if isinstance(count, bool) or not isinstance(count, int) or count < 0:
    raise SystemExit(1)
if status not in {'current', 'drift'}:
    raise SystemExit(1)
if (status == 'current') != (count == 0):
    raise SystemExit(1)
print(count)
" "$summary_file" 2>/dev/null
}

# extract_aurora_versions <file>
#
# Extracts Aurora PostgreSQL engine versions from the constants module.
# Prints one "major.minor" per line, sorted and deduplicated.
# Falls back to reading constants.py directly if the module can't be imported.
extract_aurora_versions() {
  local file="${1:-gco/stacks/regional_stack.py}"
  python3 -c "
import sys
try:
    from gco.stacks.constants import AURORA_POSTGRES_VERSION_DISPLAY
    print(AURORA_POSTGRES_VERSION_DISPLAY)
except ImportError:
    # Fallback: read constants.py directly
    import re, os
    constants_path = os.path.join(os.path.dirname(sys.argv[1]), 'constants.py')
    if os.path.exists(constants_path):
        with open(constants_path) as f:
            text = f.read()
        m = re.search(r'AURORA_POSTGRES_VERSION_DISPLAY\s*=\s*\"([^\"]+)\"', text)
        if m:
            print(m.group(1))
    else:
        # Last resort: scan the file for VER_XX_Y patterns
        with open(sys.argv[1]) as f:
            text = f.read()
        seen = set()
        for m in re.finditer(r'AuroraPostgresEngineVersion\.VER_(\d+)_(\d+)', text):
            v = f'{m.group(1)}.{m.group(2)}'
            if v not in seen:
                seen.add(v)
                print(v)
" "$file" 2>/dev/null | sort -V
}

# extract_eks_addons <file>
#
# Extracts EKS addon name|version pairs from the constants module.
# Falls back to reading constants.py directly if the module can't be imported.
# Prints one "addon_name|addon_version" per line.
extract_eks_addons() {
  local file="${1:-gco/stacks/regional_stack.py}"
  python3 -c "
import sys
try:
    from gco.stacks.constants import (
        EKS_ADDON_POD_IDENTITY_AGENT,
        EKS_ADDON_METRICS_SERVER,
        EKS_ADDON_EFS_CSI_DRIVER,
        EKS_ADDON_CLOUDWATCH_OBSERVABILITY,
        EKS_ADDON_FSX_CSI_DRIVER,
    )
    addons = [
        ('eks-pod-identity-agent', EKS_ADDON_POD_IDENTITY_AGENT),
        ('metrics-server', EKS_ADDON_METRICS_SERVER),
        ('aws-efs-csi-driver', EKS_ADDON_EFS_CSI_DRIVER),
        ('amazon-cloudwatch-observability', EKS_ADDON_CLOUDWATCH_OBSERVABILITY),
        ('aws-fsx-csi-driver', EKS_ADDON_FSX_CSI_DRIVER),
    ]
    for name, version in addons:
        print(f'{name}|{version}')
except ImportError:
    # Fallback: read constants.py directly
    import re, os
    constants_path = os.path.join(os.path.dirname(sys.argv[1]), 'constants.py')
    if os.path.exists(constants_path):
        with open(constants_path) as f:
            text = f.read()
        # Map constant names to addon names
        mapping = {
            'EKS_ADDON_POD_IDENTITY_AGENT': 'eks-pod-identity-agent',
            'EKS_ADDON_METRICS_SERVER': 'metrics-server',
            'EKS_ADDON_EFS_CSI_DRIVER': 'aws-efs-csi-driver',
            'EKS_ADDON_CLOUDWATCH_OBSERVABILITY': 'amazon-cloudwatch-observability',
            'EKS_ADDON_FSX_CSI_DRIVER': 'aws-fsx-csi-driver',
        }
        for const_name, addon_name in mapping.items():
            m = re.search(const_name + r'\s*=\s*\"([^\"]+)\"', text)
            if m:
                print(f'{addon_name}|{m.group(1)}')
    else:
        # Last resort: scan the file for inline addon_name/addon_version pairs
        with open(sys.argv[1]) as f:
            text = f.read()
        for m in re.finditer(r'addon_name=\"([^\"]+)\".*?addon_version=\"([^\"]+)\"', text, re.DOTALL):
            print(f'{m.group(1)}|{m.group(2)}')
" "$file" 2>/dev/null
}

# extract_helm_charts [charts_yaml_path]
#
# Reads ``lambda/helm-installer/charts.yaml`` and prints one JSON object per
# chart entry: ``{name, repo_url, chart, version, use_oci}``. Extracted from
# dependency-scan.sh so BATS can exercise the real charts.yaml parse — the
# driver sources this helper and pipes its output into the version-drift loop
# (``helm search repo`` / ``helm show chart`` per entry). Because it iterates
# every entry, a newly-added chart (e.g. kube-prometheus-stack) is picked up
# automatically with no change here.
#
# Prints nothing (exit 0) when the file is missing or unparseable, matching the
# other extractors in this file so the caller treats empty as "skip".
extract_helm_charts() {
  local file="${1:-lambda/helm-installer/charts.yaml}"
  [ -f "$file" ] || return 0
  python3 -c "
import json, sys, yaml
try:
    with open(sys.argv[1]) as f:
        data = yaml.safe_load(f)
except Exception:
    sys.exit(0)
for name, cfg in (data or {}).get('charts', {}).items():
    cfg = cfg or {}
    print(json.dumps({
        'name':     name,
        'repo_url': cfg.get('repo_url', ''),
        'chart':    cfg.get('chart', ''),
        'version':  cfg.get('version', ''),
        'use_oci':  cfg.get('use_oci', False),
    }))
" "$file" 2>/dev/null
}

# extract_k8s_version [cdk_json_path]
#
# Reads the kubernetes_version from cdk.json. Falls back to "1.36".
extract_k8s_version() {
  local cdk="${1:-cdk.json}"
  python3 -c "import json; print(json.load(open('$cdk'))['context']['kubernetes_version'])" 2>/dev/null || echo "1.36"
}

# extract_dockerfile_pins <dockerfile>
#
# Parses ``ARG <NAME>=<VALUE>`` lines from the given Dockerfile and emits
# ``NAME|VALUE`` for each pin we care about. The allowlist below is
# intentional — random build-time ARGs (e.g. ``BUILD_DATE``) would add
# noise to the drift report.
#
# The line-anchor (``^ARG``) and single-line Python regex avoid matching
# ``ARG`` appearing inside a comment or a RUN heredoc. Leading whitespace
# is permitted so a future ``RUN --mount=…`` or multi-stage FROM line
# doesn't break the scan silently.
#
# Example output for Dockerfile.dev:
#
#     NODE_VERSION|v24.18.0
#     NPM_VERSION|11.14.1
#     CDK_VERSION|2.1120.0
#     KUBECTL_VERSION|v1.36.1
#     AWSCLI_VERSION|2.34.42
#     DOCKER_VERSION|29.4.2
#     BUILDX_VERSION|v0.35.0
extract_dockerfile_pins() {
  local file="${1:-Dockerfile.dev}"
  [ -f "$file" ] || return 0
  python3 -c "
import re, sys
allowlist = {
    'NODE_VERSION',
    'NPM_VERSION',
    'CDK_VERSION',
    'KUBECTL_VERSION',
    'AWSCLI_VERSION',
    'DOCKER_VERSION',
    'BUILDX_VERSION',
}
with open(sys.argv[1]) as f:
    for line in f:
        # Strip trailing inline comments but keep the ARG value itself.
        stripped = line.split('#', 1)[0]
        m = re.match(r'^\s*ARG\s+([A-Z_][A-Z0-9_]*)=(\S+)\s*$', stripped)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        if name in allowlist:
            print(f'{name}|{value}')
" "$file" 2>/dev/null
}

# extract_precommit_hooks [config_path]
#
# Parses ``.pre-commit-config.yaml`` and emits one ``repo|rev`` pair per
# hook ``repo:`` block. The repo URL is left intact (it's needed to
# resolve the upstream releases endpoint), and ``rev`` is the literal
# string committed to the config — usually a tag like ``v0.15.7`` or
# ``v1.19.1`` but pre-commit also tolerates plain semver and full SHAs.
# Local hook stanzas (``repo: local``) and the pre-commit hook
# meta-stanza (``repo: meta``) are skipped: there's no upstream release
# to compare against.
#
# Falls back silently to an empty list if the file is missing or the
# YAML can't be parsed — the caller treats that as "skip" rather than
# "no drift", same pattern as the other extractors in this file.
extract_precommit_hooks() {
  local file="${1:-.pre-commit-config.yaml}"
  [ -f "$file" ] || return 0
  python3 -c "
import sys, yaml
try:
    with open(sys.argv[1]) as f:
        data = yaml.safe_load(f)
except Exception:
    sys.exit(0)
for entry in (data or {}).get('repos', []) or []:
    repo = (entry or {}).get('repo', '') or ''
    rev = (entry or {}).get('rev', '') or ''
    # ``local`` and ``meta`` are pre-commit conventions for hooks
    # that aren't backed by an upstream repo; skip them.
    if not repo or repo in ('local', 'meta'):
        continue
    if not rev:
        continue
    print(f'{repo}|{rev}')
" "$file" 2>/dev/null
}

# extract_emr_versions <file>
#
# Extracts the pinned EMR Serverless release label from the constants module.
# Prints the label (e.g. ``emr-7.13.0``) on a single line. Falls back to
# reading constants.py directly if the module can't be imported.
extract_emr_versions() {
  local file="${1:-gco/stacks/constants.py}"
  python3 -c "
import sys
try:
    from gco.stacks.constants import EMR_SERVERLESS_RELEASE_LABEL
    print(EMR_SERVERLESS_RELEASE_LABEL)
except ImportError:
    import re, os
    constants_path = os.path.join(os.path.dirname(sys.argv[1]), 'constants.py') if 'constants.py' not in sys.argv[1] else sys.argv[1]
    if os.path.exists(constants_path):
        with open(constants_path) as f:
            text = f.read()
        m = re.search(r'EMR_SERVERLESS_RELEASE_LABEL\s*=\s*\"([^\"]+)\"', text)
        if m:
            print(m.group(1))
" "$file" 2>/dev/null
}

# extract_constant_value <name> [constants_path]
#
# Reads a single string-valued top-level constant from the constants
# module by regex (does *not* import ``gco.stacks``, which would pull
# in the full CDK stack package). Used by the CDK-enum drift checks
# below to look up ``LAMBDA_PYTHON_RUNTIME`` and ``AURORA_POSTGRES_VERSION``
# without assuming the rest of the project is installable.
#
# Example:
#   extract_constant_value LAMBDA_PYTHON_RUNTIME
#   # → PYTHON_3_14
extract_constant_value() {
  local name="$1"
  local file="${2:-gco/stacks/constants.py}"
  [ -f "$file" ] || return 0
  python3 -c "
import re, sys
name = sys.argv[1]
with open(sys.argv[2]) as f:
    text = f.read()
m = re.search(r'^' + re.escape(name) + r'\s*=\s*\"([^\"]+)\"', text, re.MULTILINE)
if m:
    print(m.group(1))
" "$name" "$file" 2>/dev/null
}

# get_latest_lambda_python_runtime
#
# Imports ``aws_cdk.aws_lambda`` and prints the highest ``PYTHON_X_Y``
# enum member of ``Runtime`` (e.g. ``PYTHON_3_14``). Empty output when
# aws-cdk-lib isn't importable — callers treat this as "skip".
#
# Used by the CDK-enum drift check in ``dependency-scan.sh`` to compare
# the ``LAMBDA_PYTHON_RUNTIME`` constant against the newest Lambda
# Python runtime the installed CDK can construct. The dep-scan workflow
# installs the latest ``aws-cdk-lib`` for this; locally the helper just
# reflects whatever is on the active interpreter.
#
# Suffixed members (``PYTHON_3_14_PROVIDED`` if it ever exists) are
# ignored — the regex anchor on ``$`` keeps the result aligned with the
# canonical "X.Y" runtime CDK exposes today.
get_latest_lambda_python_runtime() {
  python3 -c "
import re
try:
    from aws_cdk import aws_lambda
except Exception:
    raise SystemExit(0)
versions = []
for name in dir(aws_lambda.Runtime):
    m = re.match(r'^PYTHON_(\d+)_(\d+)$', name)
    if m:
        versions.append((int(m.group(1)), int(m.group(2)), name))
if versions:
    print(max(versions)[2])
" 2>/dev/null
}

# get_latest_lambda_nodejs_runtime
#
# Prints the highest canonical ``NODEJS_<major>_X`` member exposed by the
# installed aws-cdk-lib. This is the Node equivalent of the Python helper
# above and feeds the managed Lambda runtime drift check.
get_latest_lambda_nodejs_runtime() {
  python3 -c "
import re
try:
    from aws_cdk import aws_lambda
except Exception:
    raise SystemExit(0)
versions = []
for name in dir(aws_lambda.Runtime):
    m = re.match(r'^NODEJS_(\d+)_X$', name)
    if m:
        versions.append((int(m.group(1)), name))
if versions:
    print(max(versions)[1])
" 2>/dev/null
}

# get_latest_aurora_postgres_version
#
# Imports ``aws_cdk.aws_rds`` and prints the highest ``VER_X_Y`` enum
# member of ``AuroraPostgresEngineVersion`` (e.g. ``VER_17_9``). Empty
# output when aws-cdk-lib isn't importable.
#
# Skips suffixed variants such as ``VER_17_9_LIMITLESS`` and
# ``VER_15_4_R2`` — those aren't the canonical "latest minor" engine
# version we pin, and including them would cause the comparison to
# flap whenever AWS publishes a sidecar release line.
get_latest_aurora_postgres_version() {
  python3 -c "
import re
try:
    from aws_cdk import aws_rds
except Exception:
    raise SystemExit(0)
versions = []
for name in dir(aws_rds.AuroraPostgresEngineVersion):
    m = re.match(r'^VER_(\d+)_(\d+)$', name)
    if m:
        versions.append((int(m.group(1)), int(m.group(2)), name))
if versions:
    print(max(versions)[2])
" 2>/dev/null
}

# get_latest_python_release
#
# Queries https://endoflife.date/api/python.json and prints the
# highest ``cycle`` (e.g. ``3.14``) that's already shipped and still
# under standard support. Empty output on network failure or schema
# change — callers treat this as "skip" rather than as drift.
#
# We pick endoflife.date because it's a clean, unauthenticated JSON
# endpoint that already filters out prerelease/EOL cycles via its
# ``releaseDate`` and ``eol`` fields. Going through python.org or the
# python/cpython GitHub API would either rate-limit (no token) or
# require us to hand-roll prerelease-tag filtering.
get_latest_python_release() {
  curl -fsSL --max-time 15 "https://endoflife.date/api/python.json" 2>/dev/null \
    | python3 -c "
import datetime, json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
today = datetime.date.today().isoformat()
candidates = []
for entry in data:
    cycle = entry.get('cycle', '')
    release = entry.get('releaseDate', '') or ''
    eol = entry.get('eol', '')
    if not cycle or '.' not in cycle:
        continue
    # Skip prereleases (release date in the future).
    if isinstance(release, str) and release > today:
        continue
    # Skip end-of-life cycles. ``eol`` may be a string date or False
    # when EOL hasn't been announced yet — treat False/empty as still
    # supported.
    if isinstance(eol, str) and eol and eol < today:
        continue
    try:
        parts = tuple(int(p) for p in cycle.split('.'))
    except ValueError:
        continue
    candidates.append((parts, cycle))
if candidates:
    print(max(candidates)[1])
" 2>/dev/null
}
# get_latest_precommit_hook_release <repo_url>
#
# Given the ``repo:`` URL committed to ``.pre-commit-config.yaml``,
# prints the latest semver-shaped tag from the upstream Git host so
# the dep-scan can compare it against the pinned ``rev:``. Empty
# output on network failure, an unsupported host, or when no tag
# matches — callers treat that as "skip" rather than as drift.
#
# Today only GitHub repos are supported. Every hook in the project's
# ``.pre-commit-config.yaml`` is hosted there, and the pre-commit
# ecosystem is overwhelmingly GitHub-based. If a future hook lives
# elsewhere (GitLab, Codeberg) the helper will return empty and the
# scan logs a one-line skip note for that hook — no false drift.
#
# We use ``GET /repos/{owner}/{repo}/tags`` rather than
# ``releases/latest`` because pre-commit pins ``rev:`` to a Git tag,
# not a GitHub Release — and several hooks (yamllint, mirrors-mypy,
# markdownlint-cli2) tag without ever cutting a Release. The tags
# endpoint returns newest-first, so we filter to ``vX.Y.Z`` /
# ``X.Y.Z`` / ``X.Y`` shapes, drop pre-release suffixes (``-rc1``,
# ``-beta``), and take the highest by semver.
#
# Unauthenticated. The monthly scan calls this once per hook (four
# times against today's config) — the unauthenticated GitHub API
# limit is 60 req/h per IP, so a per-PAT/GITHUB_TOKEN bump to the
# 5000 req/h authenticated bucket isn't worth the extra coupling.
get_latest_precommit_hook_release() {
  local repo_url="$1"
  [ -n "$repo_url" ] || return 0

  # Only GitHub is supported today. Strip any trailing ``.git`` or
  # ``/`` so the owner/repo extraction works for both forms commonly
  # seen in pre-commit configs.
  local cleaned="${repo_url%.git}"
  cleaned="${cleaned%/}"
  case "$cleaned" in
    https://github.com/*) ;;
    *) return 0 ;;
  esac

  local owner_repo="${cleaned#https://github.com/}"
  # Reject anything that isn't ``owner/repo`` (no extra path segments).
  case "$owner_repo" in
    */*/*) return 0 ;;
    */*) ;;
    *) return 0 ;;
  esac

  curl -fsSL --max-time 15 \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${owner_repo}/tags?per_page=100" 2>/dev/null \
    | python3 -c "
import json, re, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
# pre-commit's ``rev:`` accepts ``vX.Y[.Z]``, ``X.Y[.Z]``, or full
# SHAs. We compare on the semver-shaped ones; SHA-pinned hooks are
# left alone (the helper returns empty and the caller skips them).
pat = re.compile(r'^v?\d+\.\d+(?:\.\d+)?$')
candidates = []
for entry in data or []:
    name = (entry or {}).get('name', '')
    if not pat.match(name):
        continue
    stripped = name.lstrip('v')
    parts = stripped.split('.')
    try:
        nums = tuple(int(p) for p in parts)
    except ValueError:
        continue
    candidates.append((nums, name))
if candidates:
    print(max(candidates)[1])
" 2>/dev/null
}


# extract_mooncake_default_image [images_py_path]
#
# Prints the default upstream Mooncake vLLM image reference (``repo:tag``)
# pinned in ``cli/images.py`` as ``_DISAGGREGATED_DEFAULT_IMAGE`` — the image
# GCO's disaggregated/store/both inference deploys pull when the operator
# passes no ``--image``.
#
# This image lives in a Python constant, not a Dockerfile ``FROM`` or a K8s
# manifest, so neither Dependabot (docker ecosystem) nor the manifest/workflow
# image sweep in dependency-scan.sh sees it. This extractor feeds it into the
# Docker-image drift check so a newer vLLM release is surfaced in the monthly
# report — the cue to validate and bump the pin (the ``mooncake-image``
# workflow re-runs the image contract tests against the new tag).
#
# Prints nothing if the file or constant is absent — the caller treats an
# empty result as "skip", same as the other extractors here.
extract_mooncake_default_image() {
  local file="${1:-cli/images.py}"
  [ -f "$file" ] || return 0
  python3 -c "
import re, sys
with open(sys.argv[1]) as f:
    text = f.read()
m = re.search(r'^_DISAGGREGATED_DEFAULT_IMAGE\s*=\s*\"([^\"]+)\"', text, re.MULTILINE)
if m:
    print(m.group(1))
" "$file" 2>/dev/null
}

# extract_default_bedrock_model [cdk_json_path]
#
# Prints the shared default Bedrock model id from
# ``cdk.json`` ``context.bedrock.default_model_id``. Mission sampling and
# the capacity advisor both resolve this system-defined cross-Region inference
# profile through ``gco.bedrock`` when no explicit override is supplied.
#
# This value feeds the Bedrock-model drift check in dependency-scan.sh, which
# compares it against the newest profile in the same model family
# (get_latest_bedrock_model). A newer release is the cue to update cdk.json and
# re-capture the scaffold fixture under tests/fixtures/scaffold_responses/.
#
# Prints nothing if the file is absent, malformed, or does not contain a
# non-empty string at the expected path. The caller treats empty output as a
# skip, matching the other extractors in this library.
extract_default_bedrock_model() {
  local file="${1:-cdk.json}"
  [ -f "$file" ] || return 0
  python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as handle:
        data = json.load(handle)
    value = data.get('context', {}).get('bedrock', {}).get('default_model_id')
except Exception:
    value = None
if isinstance(value, str) and value.strip():
    print(value.strip())
" "$file" 2>/dev/null
}

# bedrock_model_family <inference_profile_id>
#
# Prints the "model family" key for a Bedrock system-defined inference
# profile id so two releases of the same model line compare equal on
# family and differ only on version. The family is the geography +
# provider + the *alphabetic* tokens of the model name, with every
# purely-numeric token (model version, generation, date) dropped:
#
#   us.amazon.nova-pro-v1:0                       -> us.amazon.nova-pro
#   global.amazon.nova-3-lite-v1:0                -> global.amazon.nova-lite
#   us.anthropic.claude-sonnet-4-5-20250929-v1:0  -> us.anthropic.claude-sonnet
#   global.anthropic.claude-opus-4-6-v1           -> global.anthropic.claude-opus
#   global.anthropic.claude-opus-9                -> global.anthropic.claude-opus
#
# The trailing revision appears in three shapes across live profiles:
# ``-vMAJOR:MINOR``, ``-vMAJOR`` alone (newer Anthropic profiles), and
# absent entirely. All three are stripped, so one model line stays one
# family; matching only the ``:MINOR`` form would file
# ``claude-opus-4-6-v1`` under a phantom ``claude-opus-v1`` family and
# silently stop reporting drift against ``claude-opus-5``.
#
# Folding the numeric generation token into the version key (rather
# than the family) is deliberate: it keeps "Nova 1 Pro" and a future
# "Nova 2 Pro" in the same family so a generation bump is reported as
# drift, while different tiers (nova-pro vs nova-lite) and providers
# stay in separate families and are never cross-suggested.
bedrock_model_family() {
  python3 -c "
import re, sys
mid = sys.argv[1]
core = re.sub(r'-v\d+(?::\d+)?\Z', '', mid)
parts = core.split('.')
if len(parts) >= 3:
    geo, provider, name = parts[0], parts[1], '.'.join(parts[2:])
elif len(parts) == 2:
    geo, provider, name = '', parts[0], parts[1]
else:
    geo, provider, name = '', '', core
tokens = [t for t in name.split('-') if t and not t.isdigit()]
prefix = '.'.join([p for p in (geo, provider) if p])
print(prefix + ('.' + '-'.join(tokens) if tokens else ''))
" "$1" 2>/dev/null
}

# compare_bedrock_model <current_id> <candidate_id>
#
# Prints "newer" when candidate is a newer release than current,
# "same" when equal, "older" otherwise — mirroring compare_semver's
# contract so the drift check reads the same way. The comparison key
# is the tuple of every integer in the id, left to right (model
# version, generation, date, and the trailing ``vMAJOR:MINOR``), so
# nova-pro-v1:0 (1,0) is older than a hypothetical nova-pro-v2:0 (2,0)
# and claude-sonnet-4-5-...-v1:0 (4,5,...) is older than a
# claude-sonnet-4-6-...-v1:0. Callers scope to one family first (see
# bedrock_model_family); this helper only looks at the integer key.
compare_bedrock_model() {
  python3 -c "
import re, sys
def key(mid):
    return [int(n) for n in re.findall(r'\d+', mid)]
a, b = key(sys.argv[1]), key(sys.argv[2])
print('same' if a == b else ('newer' if b > a else 'older'))
" "$1" "$2" 2>/dev/null
}

# get_latest_bedrock_model <current_id> [region]
#
# Prints the newest system-defined inference-profile id in the same
# model family as <current_id> (see bedrock_model_family), as reported
# by ``aws bedrock list-inference-profiles --type-equals SYSTEM_DEFINED``.
# Used by the Bedrock-model drift check to tell whether the pinned
# DEFAULT_BEDROCK_MODEL_ID has a newer release available.
#
# Family scoping keeps the comparison apples-to-apples: a newer Nova
# Pro is reported against a pinned Nova Pro, but a different tier (Nova
# Lite, a Claude model, ...) is never suggested as a "newer" default —
# switching tier/provider is a human decision, not drift.
#
# Region defaults to us-east-1 (matches the advisor + Mission sampling
# default region) regardless of the workflow's configured region, so both
# global.* and geography-scoped cross-Region profiles resolve consistently.
# Empty output on any failure (no creds, throttling, schema change) — the caller
# treats an empty result as "skip", same as the other AWS-creds
# helpers.
#
# IAM action: bedrock:ListInferenceProfiles.
get_latest_bedrock_model() {
  local current="$1"
  local region="${2:-us-east-1}"
  [ -n "$current" ] || return 0
  aws bedrock list-inference-profiles \
    --type-equals SYSTEM_DEFINED \
    --region "$region" \
    --output json 2>/dev/null \
  | python3 -c "
import json, re, sys
current = sys.argv[1]
def family(mid):
    # Keep in lockstep with bedrock_model_family above: the revision
    # suffix is optional and its ``:MINOR`` half is too.
    core = re.sub(r'-v\d+(?::\d+)?\Z', '', mid)
    parts = core.split('.')
    if len(parts) >= 3:
        geo, provider, name = parts[0], parts[1], '.'.join(parts[2:])
    elif len(parts) == 2:
        geo, provider, name = '', parts[0], parts[1]
    else:
        geo, provider, name = '', '', core
    tokens = [t for t in name.split('-') if t and not t.isdigit()]
    prefix = '.'.join([p for p in (geo, provider) if p])
    return prefix + ('.' + '-'.join(tokens) if tokens else '')
def key(mid):
    return [int(n) for n in re.findall(r'\d+', mid)]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
target = family(current)
cands = []
for prof in data.get('inferenceProfileSummaries', []) or []:
    pid = prof.get('inferenceProfileId', '') or ''
    if (prof.get('status') or 'ACTIVE') != 'ACTIVE':
        continue
    if pid and family(pid) == target:
        cands.append(pid)
if cands:
    cands.sort(key=key)
    print(cands[-1])
" "$current" 2>/dev/null
}

# =============================================================================
# Expanded coverage helpers
# =============================================================================
# The functions below extend the scan to surfaces that live outside the
# original twelve: CI tooling pins the workflows install by hand, version
# pins that must move in lockstep across several files, and recurring
# hygiene checks (suppression expiry, lockfile freshness, base-image
# security epochs).
#
# Every one keeps the same contract as the extractors above: print one
# record per line to stdout, and print nothing (exit 0) on missing/malformed
# input so the caller treats an empty result as "skip", never as drift.
# =============================================================================

# get_latest_github_release_tag <owner/repo>
#
# Prints the ``tag_name`` of the latest non-prerelease GitHub Release for
# ``<owner/repo>`` (e.g. ``v0.70.0``). Generalises the inline release lookups
# the Dockerfile.dev section already does for moby/moby and docker/buildx so
# the CI-tooling drift check (Trivy, Helm, kind) can share one code path.
#
# Empty output on network failure, a non ``owner/repo`` argument, or a repo
# with no published Release — callers treat empty as "skip", same as the
# other lookups here. Unauthenticated: the monthly scan makes a handful of
# these calls, well under the 60 req/h anonymous GitHub limit.
get_latest_github_release_tag() {
  local owner_repo="$1"
  [ -n "$owner_repo" ] || return 0
  # Reject anything that isn't exactly ``owner/repo`` (mirrors the guard in
  # get_latest_precommit_hook_release) so a stray URL or path can't 404.
  case "$owner_repo" in
    */*/*) return 0 ;;
    */*) ;;
    *) return 0 ;;
  esac
  curl -fsSL --max-time 15 \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${owner_repo}/releases/latest" 2>/dev/null \
    | jq -r '.tag_name // empty' 2>/dev/null
}

# extract_workflow_env_pin <VAR_NAME> [workflows_dir]
#
# Prints the unique value(s) of a ``<VAR_NAME>: "<value>"`` env assignment
# found across the workflow YAML under <workflows_dir> (default
# ``.github/workflows``). Used by the CI-tooling drift check to read the
# pinned ``TRIVY_VERSION`` / ``HELM_VERSION`` / ``KUBECTL_VERSION`` the
# workflows install their own tooling from — pins Dependabot doesn't watch
# (they're plain env strings, not ``uses:`` refs or Dockerfile ``FROM``
# lines).
#
# Prints one value per line, de-duplicated and sorted. More than one line
# means the same tool is pinned to *different* values across files — a
# lockstep-drift bug the Version Consistency section reports. Empty output
# when the dir is absent or the var is unset anywhere.
extract_workflow_env_pin() {
  local var="$1"
  local dir="${2:-.github/workflows}"
  [ -n "$var" ] || return 0
  [ -d "$dir" ] || return 0
  grep -rhoE "^[[:space:]]*${var}:[[:space:]]*\"?[A-Za-z0-9._+-]+\"?" "$dir" 2>/dev/null \
    | sed -E "s/^[[:space:]]*${var}:[[:space:]]*//" \
    | tr -d '"' \
    | sort -u
}

# extract_kind_pins [workflow_file]
#
# Prints the kind pins configured on the ``helm/kind-action`` step:
#   kind|<version>        e.g. kind|v0.32.0        (the kind binary)
#   kind-node|<image:tag> e.g. kind-node|kindest/node:v1.36.1
#
# These live in the action's ``with:`` block, not a top-level ``image:`` or
# a Dockerfile ``FROM``, so neither the workflow image sweep nor Dependabot's
# docker ecosystem sees them. The caller checks the kind binary against
# kubernetes-sigs/kind releases and the node image against its own registry
# tags within the pinned K8s minor.
#
# Empty output if the file or the kind-action step is absent.
extract_kind_pins() {
  local file="${1:-.github/workflows/integration-tests.yml}"
  [ -f "$file" ] || return 0
  python3 -c "
import sys, yaml
try:
    with open(sys.argv[1]) as f:
        data = yaml.safe_load(f)
except Exception:
    sys.exit(0)
for job in (data or {}).get('jobs', {}).values():
    for step in (job or {}).get('steps', []) or []:
        uses = (step or {}).get('uses', '') or ''
        if uses.startswith('helm/kind-action'):
            with_ = (step or {}).get('with', {}) or {}
            ver = with_.get('version', '')
            node = with_.get('node_image', '')
            if ver:
                print(f'kind|{ver}')
            if node:
                print(f'kind-node|{node}')
" "$file" 2>/dev/null
}

# extract_ruff_pins [pyproject] [precommit] [lint_workflow]
#
# Prints the ruff version pinned in each place the project keeps it, one
# ``source|version`` per line (version normalised without a leading ``v``):
#   pyproject|0.15.19     ruff==X in [project.optional-dependencies]
#   precommit|0.15.20     astral-sh/ruff-pre-commit rev in .pre-commit-config.yaml
#   lint-action|0.15.19   astral-sh/ruff-action version input in lint.yml
#
# Ruff is pinned in three spots that must move together (developer install,
# the pre-commit hook, and the prebuilt-binary CI lint job). The base Python
# deps check already flags ruff drift vs PyPI, but nothing catches the three
# local pins silently disagreeing — which is exactly what the Version
# Consistency section reports.
extract_ruff_pins() {
  local pyproject="${1:-pyproject.toml}"
  local precommit="${2:-.pre-commit-config.yaml}"
  local lintwf="${3:-.github/workflows/lint.yml}"
  python3 -c "
import re, sys
pyproject, precommit, lintwf = sys.argv[1], sys.argv[2], sys.argv[3]

def norm(v):
    return v.lstrip('v').strip()

# pyproject: first ruff==X.Y.Z anywhere (the lint + diagrams extras pin the
# same value; report it once).
try:
    with open(pyproject) as f:
        m = re.search(r'ruff==([0-9.]+)', f.read())
    if m:
        print(f'pyproject|{norm(m.group(1))}')
except OSError:
    pass

# pre-commit: rev of the astral-sh/ruff-pre-commit repo block.
try:
    import yaml
    with open(precommit) as f:
        data = yaml.safe_load(f)
    for entry in (data or {}).get('repos', []) or []:
        if 'astral-sh/ruff-pre-commit' in ((entry or {}).get('repo', '') or ''):
            rev = (entry or {}).get('rev', '')
            if rev:
                print(f'precommit|{norm(rev)}')
            break
except Exception:
    pass

# lint workflow: version input on the first astral-sh/ruff-action step (the
# two steps pin the same value). Parse the YAML rather than regex the raw
# text so a nearby ``python-version:`` can't be mistaken for the action's
# own ``version:`` input.
try:
    import yaml
    with open(lintwf) as f:
        wf = yaml.safe_load(f)
    found = ''
    for job in (wf or {}).get('jobs', {}).values():
        for step in (job or {}).get('steps', []) or []:
            uses = (step or {}).get('uses', '') or ''
            if uses.startswith('astral-sh/ruff-action'):
                found = str(((step or {}).get('with', {}) or {}).get('version', '') or '')
                if found:
                    break
        if found:
            break
    if found:
        print(f'lint-action|{norm(found)}')
except Exception:
    pass
" "$pyproject" "$precommit" "$lintwf" 2>/dev/null
}

# extract_python_version_pins [workflows_dir]
#
# Prints one line per ``python-version: "X.Y"`` occurrence across the
# workflow YAML (value only, e.g. ``3.14``). The caller collapses these to
# unique values: more than one distinct value, or a value that disagrees
# with the project's canonical Python (derived from LAMBDA_PYTHON_RUNTIME),
# means the CI matrix drifted from the runtime the Lambdas actually ship on.
extract_python_version_pins() {
  local dir="${1:-.github/workflows}"
  [ -d "$dir" ] || return 0
  grep -rhoE "python-version:[[:space:]]*\"?[0-9]+\.[0-9]+\"?" "$dir" 2>/dev/null \
    | sed -E "s/python-version:[[:space:]]*//" \
    | tr -d '"'
}

# list_npm_package_dirs [root]
#
# Prints every repository-owned npm package directory. Generated, vendored,
# virtual-environment, and CDK assembly trees are excluded so a copied
# ``package.json`` never becomes a false dependency surface.
list_npm_package_dirs() {
  local root="${1:-.}"
  python3 -c "
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
excluded = {
    '.git', '.kiro', '.mypy_cache', '.pytest_cache', '.ruff_cache',
    'build', 'cdk.out', 'dist', 'node_modules', '__pycache__',
}

def owned(path):
    parts = path.relative_to(root).parts[:-1]
    return not any(
        part in excluded or part.startswith('.venv') or part.endswith('-build')
        for part in parts
    )

for manifest in sorted(root.rglob('package.json')):
    if not owned(manifest):
        continue
    relative = manifest.parent.relative_to(root)
    print('.' if relative == Path('.') else relative.as_posix())
" "$root" 2>/dev/null
}

# check_npm_package_management [root] [dependabot_config]
#
# Emits ``package.json|problem`` for every repository-owned npm graph that is
# not fully reproducible and managed. A graph must have a package-lock.json,
# an exact npm packageManager pin, exact direct dependency pins, and a matching
# Dependabot npm directory entry. The all-package npm-audit CI job treats any
# output as a hard failure; the monthly scan also reports it as consistency
# drift.
check_npm_package_management() {
  local root="${1:-.}"
  local dependabot="${2:-.github/dependabot.yml}"
  python3 -c "
import json, re, sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
dependabot_path = Path(sys.argv[2])
if not dependabot_path.is_absolute():
    dependabot_path = root / dependabot_path
excluded = {
    '.git', '.kiro', '.mypy_cache', '.pytest_cache', '.ruff_cache',
    'build', 'cdk.out', 'dist', 'node_modules', '__pycache__',
}
exact_version = re.compile(r'^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$')

def owned(path):
    parts = path.relative_to(root).parts[:-1]
    return not any(
        part in excluded or part.startswith('.venv') or part.endswith('-build')
        for part in parts
    )

dependabot_dirs = set()
try:
    text = dependabot_path.read_text(encoding='utf-8')
except OSError:
    text = ''
for block in re.split(r'(?m)(?=^\s*-\s+package-ecosystem:)', text):
    if not re.search(r'(?m)^\s*-\s+package-ecosystem:\s*[\"\']?npm[\"\']?\s*$', block):
        continue
    match = re.search(r'(?m)^\s+directory:\s*[\"\']?([^\"\'\s]+)', block)
    if match:
        dependabot_dirs.add('/' + match.group(1).strip('/'))

for manifest in sorted(root.rglob('package.json')):
    if not owned(manifest):
        continue
    rel_manifest = manifest.relative_to(root).as_posix()
    rel_dir = manifest.parent.relative_to(root)
    dependabot_dir = '/' if rel_dir == Path('.') else '/' + rel_dir.as_posix()
    try:
        package = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        print(f'{rel_manifest}|invalid JSON: {exc}')
        continue
    if not manifest.with_name('package-lock.json').is_file():
        print(f'{rel_manifest}|missing package-lock.json')
    manager = package.get('packageManager', '')
    if not re.fullmatch(r'npm@\d+\.\d+\.\d+', manager):
        print(f'{rel_manifest}|packageManager must be an exact npm@X.Y.Z pin')
    for section in ('dependencies', 'devDependencies', 'optionalDependencies'):
        for name, version in (package.get(section) or {}).items():
            if not isinstance(version, str) or not exact_version.fullmatch(version):
                print(f'{rel_manifest}|{section}.{name} must use an exact version pin')
    if dependabot_dir not in dependabot_dirs:
        print(f'{rel_manifest}|missing Dependabot npm entry for {dependabot_dir}')
" "$root" "$dependabot" 2>/dev/null
}

# extract_node_major_pins [root] [constants] [nvmrc] [dockerfile]
#
# Emits ``source|major`` for every place that intentionally mirrors the
# repository Node major: the Lambda runtime constant, .nvmrc, every owned npm
# graph's engine, and Dockerfile.dev. The driver reports missing sources and
# disagreement; the CDK enum check separately detects a newer Lambda runtime.
extract_node_major_pins() {
  local root="${1:-.}"
  local constants="${2:-gco/stacks/constants.py}"
  local nvmrc="${3:-.nvmrc}"
  local dockerfile="${4:-Dockerfile.dev}"
  python3 -c "
import json, re, sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
excluded = {
    '.git', '.kiro', '.mypy_cache', '.pytest_cache', '.ruff_cache',
    'build', 'cdk.out', 'dist', 'node_modules', '__pycache__',
}

def resolve(path):
    candidate = Path(path)
    return candidate if candidate.is_absolute() else root / candidate

def emit(source, value):
    match = re.search(r'\d+', str(value))
    if match:
        print(f'{source}|{int(match.group())}')

def owned(path):
    parts = path.relative_to(root).parts[:-1]
    return not any(
        part in excluded or part.startswith('.venv') or part.endswith('-build')
        for part in parts
    )

try:
    text = resolve(sys.argv[2]).read_text(encoding='utf-8')
    match = re.search(r'^LAMBDA_NODEJS_RUNTIME\s*=\s*\"NODEJS_(\d+)_X\"', text, re.MULTILINE)
    if match:
        emit('gco/stacks/constants.py', match.group(1))
except OSError:
    pass
try:
    emit('.nvmrc', resolve(sys.argv[3]).read_text(encoding='utf-8').strip())
except OSError:
    pass
try:
    text = resolve(sys.argv[4]).read_text(encoding='utf-8')
    # NODE_VERSION pins the exact release (e.g. ``v24.18.0``); emit()
    # reduces it to the leading major for the cross-source comparison.
    match = re.search(r'^\s*ARG\s+NODE_VERSION=(v?\d+(?:\.\d+)*)\s*$', text, re.MULTILINE)
    if match:
        emit('Dockerfile.dev', match.group(1))
except OSError:
    pass
for manifest in sorted(root.rglob('package.json')):
    if not owned(manifest):
        continue
    try:
        package = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        continue
    emit(manifest.relative_to(root).as_posix(), (package.get('engines') or {}).get('node', ''))
" "$root" "$constants" "$nvmrc" "$dockerfile" 2>/dev/null
}

# extract_npm_version_pins [root] [dockerfile]
#
# Emits every package.json packageManager npm version plus Dockerfile.dev's
# NPM_VERSION so Dependabot/tooling updates cannot leave contributor and CI
# npm versions disagreeing.
extract_npm_version_pins() {
  local root="${1:-.}"
  local dockerfile="${2:-Dockerfile.dev}"
  python3 -c "
import json, re, sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
excluded = {
    '.git', '.kiro', '.mypy_cache', '.pytest_cache', '.ruff_cache',
    'build', 'cdk.out', 'dist', 'node_modules', '__pycache__',
}

def owned(path):
    parts = path.relative_to(root).parts[:-1]
    return not any(
        part in excluded or part.startswith('.venv') or part.endswith('-build')
        for part in parts
    )

for manifest in sorted(root.rglob('package.json')):
    if not owned(manifest):
        continue
    try:
        package = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        continue
    match = re.fullmatch(r'npm@(\d+\.\d+\.\d+)', package.get('packageManager', ''))
    if match:
        print(f'{manifest.relative_to(root).as_posix()}|{match.group(1)}')
docker = Path(sys.argv[2])
if not docker.is_absolute():
    docker = root / docker
try:
    text = docker.read_text(encoding='utf-8')
    match = re.search(r'^\s*ARG\s+NPM_VERSION=(\d+\.\d+\.\d+)\s*$', text, re.MULTILINE)
    if match:
        print(f'Dockerfile.dev|{match.group(1)}')
except OSError:
    pass
" "$root" "$dockerfile" 2>/dev/null
}

# extract_cdk_cli_pins [root] [dockerfile]
#
# Emits the root tooling graph's aws-cdk version and Dockerfile.dev's global
# CDK CLI pin. Both execution paths must synthesize with the same CLI release.
extract_cdk_cli_pins() {
  local root="${1:-.}"
  local dockerfile="${2:-Dockerfile.dev}"
  python3 -c "
import json, re, sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
try:
    package = json.loads((root / 'package.json').read_text(encoding='utf-8'))
    version = (package.get('devDependencies') or {}).get('aws-cdk', '')
    if re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?', version):
        print(f'package.json|{version}')
except (OSError, json.JSONDecodeError):
    pass
docker = Path(sys.argv[2])
if not docker.is_absolute():
    docker = root / docker
try:
    text = docker.read_text(encoding='utf-8')
    match = re.search(r'^\s*ARG\s+CDK_VERSION=(\S+)\s*$', text, re.MULTILINE)
    if match:
        print(f'Dockerfile.dev|{match.group(1)}')
except OSError:
    pass
" "$root" "$dockerfile" 2>/dev/null
}

# parse_suppression_expiries <file>
#
# Prints ``ID|YYYY-MM-DD`` for every dated suppression entry in a
# ``.trivyignore`` / ``.pip-audit-ignore`` / ``.npm-audit-ignore`` file (any
# non-comment line carrying an ``exp:YYYY-MM-DD`` marker). For the
# whitespace-delimited trivy/pip format the ID is the first token; for the
# npm-audit pipe format (``package-dir|package|advisory|node-path|exp:…``)
# the ID is the advisory field, so the report names the GHSA rather than the
# whole entry line. The caller computes days-to-expiry and surfaces entries
# expiring soon so they get renewed *before* the CI expiry validator hard-
# fails a PR — the report is the early warning, the validator is the gate.
#
# Empty output when the file is absent or has no dated entries.
parse_suppression_expiries() {
  local file="$1"
  [ -f "$file" ] || return 0
  python3 -c "
import re, sys
with open(sys.argv[1]) as f:
    for line in f:
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        m = re.search(r'exp:(\d{4}-\d{2}-\d{2})', s)
        if not m:
            continue
        fields = s.split('|')
        if len(fields) == 5:
            # .npm-audit-ignore pipe format — the advisory is field 3.
            ident = fields[2]
        else:
            ident = s.split()[0]
        print(f'{ident}|{m.group(1)}')
" "$file" 2>/dev/null
}

# extract_helm_installer_pins [dockerfile]
#
# Prints the tool versions hardcoded in the helm-installer Lambda's
# Dockerfile RUN lines:
#   HELM_VERSION|vX.Y.Z     from the get.helm.sh download URL
#   KUBECTL_VERSION|vX.Y.Z  from the dl.k8s.io download URL
#
# These pins are RUN-line URL literals — not ARGs, ``FROM`` lines, or
# workflow env — so Dependabot, the Dockerfile.dev ARG sweep, and the
# workflow-env consistency check were all blind to them. The consistency
# section compares them against the HELM_VERSION / KUBECTL_VERSION workflow
# env pins (and Dockerfile.dev's KUBECTL_VERSION ARG) so the Lambda image,
# the CI installs, and the dev container can't silently disagree about
# which helm/kubectl they ship.
#
# Empty output when the file is absent or the URLs aren't found — callers
# treat that as "skip", matching every other extractor here.
extract_helm_installer_pins() {
  local file="${1:-lambda/helm-installer/Dockerfile}"
  [ -f "$file" ] || return 0
  python3 -c "
import re, sys
with open(sys.argv[1]) as f:
    text = f.read()
m = re.search(r'get\.helm\.sh/helm-(v\d+\.\d+\.\d+)-', text)
if m:
    print(f'HELM_VERSION|{m.group(1)}')
m = re.search(r'dl\.k8s\.io/release/(v\d+\.\d+\.\d+)/', text)
if m:
    print(f'KUBECTL_VERSION|{m.group(1)}')
" "$file" 2>/dev/null
}

# check_lockfile_freshness [pyproject] [lockfile]
#
# Prints one PEP-503-normalised direct-dependency name per line for every
# dep pinned in ``pyproject.toml`` that is ABSENT from ``requirements-lock.txt``
# — the signature of "added/renamed a dep but forgot to re-run pip-compile".
# The lock is compiled with ``--all-extras``, so every direct dep (base *and*
# every optional-dependencies group) is expected to appear. Deterministic and
# offline: it checks presence only, never version equality, so it never
# false-positives on the legitimate transitive pins pip-compile adds.
#
# Empty output when either file is missing or every direct dep is present.
check_lockfile_freshness() {
  local pyproject="${1:-pyproject.toml}"
  local lockfile="${2:-requirements-lock.txt}"
  [ -f "$pyproject" ] || return 0
  [ -f "$lockfile" ] || return 0
  python3 -c "
import re, sys, tomllib
pyproject, lockfile = sys.argv[1], sys.argv[2]
try:
    with open(pyproject, 'rb') as f:
        data = tomllib.load(f)
except Exception:
    sys.exit(0)
project = data.get('project', {}) or {}
deps = list(project.get('dependencies', []) or [])
for group in (project.get('optional-dependencies', {}) or {}).values():
    deps.extend(group or [])

def norm(name):
    return re.sub(r'[-_.]+', '-', name).lower()

names = set()
for spec in deps:
    if not isinstance(spec, str):
        continue
    name = re.split(r'[\\[=!<>;~ ]', spec, maxsplit=1)[0].strip()
    if not name or name.lower() == 'gco-cli':
        continue
    names.add(norm(name))

locked = set()
with open(lockfile) as f:
    for line in f:
        s = line.strip()
        if not s or s.startswith('#') or s.startswith('-'):
            continue
        name = re.split(r'[\\[=!<>;~ ]', s, maxsplit=1)[0].strip()
        if name:
            locked.add(norm(name))

for missing in sorted(names - locked):
    print(missing)
" "$pyproject" "$lockfile" 2>/dev/null
}

# extract_security_epochs <dockerfile>
#
# Prints ``ARGNAME|YYYY-MM-DD`` for each build-time security-refresh epoch
# ARG in the given Dockerfile (``APT_SECURITY_EPOCH`` for the Debian service
# images / dev image, ``DNF_SECURITY_EPOCH`` for the AL2023 helm-installer
# Lambda). These dates are bumped by hand to bust the CI layer cache and pull
# freshly-published OS security patches; nothing else reminds anyone to move
# them, so the report flags an epoch older than the freshness window. Trivy's
# container scan is the backstop; this is the proactive nudge.
#
# Empty output when the file is absent or pins no epoch ARG.
extract_security_epochs() {
  local file="$1"
  [ -f "$file" ] || return 0
  python3 -c "
import re, sys
with open(sys.argv[1]) as f:
    for line in f:
        stripped = line.split('#', 1)[0]
        m = re.match(r'^\s*ARG\s+((?:APT|DNF)_SECURITY_EPOCH)=(\d{4}-\d{2}-\d{2})\s*$', stripped)
        if m:
            print(f'{m.group(1)}|{m.group(2)}')
" "$file" 2>/dev/null
}
