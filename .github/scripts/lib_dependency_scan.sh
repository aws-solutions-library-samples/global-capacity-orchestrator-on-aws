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
#     NODE_MAJOR|24
#     NPM_VERSION|11.14.1
#     CDK_VERSION|2.1120.0
#     KUBECTL_VERSION|v1.36.1
#     AWSCLI_VERSION|2.34.42
#     DOCKER_VERSION|29.4.2
extract_dockerfile_pins() {
  local file="${1:-Dockerfile.dev}"
  [ -f "$file" ] || return 0
  python3 -c "
import re, sys
allowlist = {
    'NODE_MAJOR',
    'NPM_VERSION',
    'CDK_VERSION',
    'KUBECTL_VERSION',
    'AWSCLI_VERSION',
    'DOCKER_VERSION',
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

