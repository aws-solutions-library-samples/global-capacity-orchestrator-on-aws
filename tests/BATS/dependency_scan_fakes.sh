#!/usr/bin/env bash
# =============================================================================
# dependency_scan_fakes.sh — the faked upstream world dependency-scan.sh runs
# against in test_dependency_scan_driver.bats
# =============================================================================
#
# The monthly scan asks a dozen registries and AWS APIs one question each:
# "what is the newest release of X?". Its own logic is everything around
# that answer — which pins it reads, how it classifies a reply, what it
# reports and how it degrades when a lookup fails — and that is what the
# driver suite exercises. Every network-facing tool the scan calls is a fake
# on PATH that answers from a *catalog* of the repository's own pins, built
# by the suite from the tree the scan is pointed at with the same extractors
# the scan uses. So by default every answer is "you are current"; with
# FAKE_DRIFT=1 every answer is one release newer than the pin (the first
# number in the version is incremented, which keeps image variants and
# Bedrock model families intact); and each fake has a switch for the failure
# its section must survive.
#
# Catalog lines are ``kind|key|value``:
#   npm|<package>|<version>            pypi|<package>|<version>
#   pydep|<normalised name>|<version>  github-release|<owner/repo>|<tag>
#   github-tags|<owner/repo>|<tag>     node|dist|<vN.x.y>   node-lts|major|<N>
#   k8s-stable|<minor>|<version>       endoflife|<product>|<series>
#   image|<registry>/<repo>|<tag>      digest|<repo:tag>|<sha256 hex>
#   helm|<name>/<chart>|<version>      helm-oci|<repo_url>/<chart>|<version>
#   eks-addon|<name>|<version>         k8s|current|<version>
#   aurora|current|<version>           emr|current|<label>
#   bedrock-profile|<leaf>|<id>        bedrock-embedding|<block>|<id>
#   cdk-enum|<constant>|<member>
#
# Sourced by every fake through FAKE_LIB; the suite exports FAKE_CATALOG.
# =============================================================================

# catalog_get <kind> <key> — the first value recorded for kind|key.
catalog_get() {
    grep -F -- "${1}|${2}|" "$FAKE_CATALOG" 2>/dev/null | head -1 | cut -d'|' -f3-
}

# catalog_values <kind> [key] — every value recorded for a kind (and key).
catalog_values() {
    if [ -n "${2:-}" ]; then
        grep -F -- "${1}|${2}|" "$FAKE_CATALOG" 2>/dev/null | cut -d'|' -f3-
    else
        grep -- "^${1}|" "$FAKE_CATALOG" 2>/dev/null | cut -d'|' -f3-
    fi
}

# bump <version> — one release newer: the first integer in the string plus
# one (v1.36.4 -> v2.36.4, 26.08-py3 -> 27.08-py3, claude-opus-5 ->
# claude-opus-6, emr-7.14.0 -> emr-8.14.0).
bump() {
    if [[ "$1" =~ ^([^0-9]*)([0-9]+)(.*)$ ]]; then
        printf '%s%d%s' "${BASH_REMATCH[1]}" "$((BASH_REMATCH[2] + 1))" "${BASH_REMATCH[3]}"
    else
        printf '%s' "$1"
    fi
}

# bump_last <version> — a patch newer: the last integer plus one
# (emr-7.14.0 -> emr-7.14.1).
bump_last() {
    if [[ "$1" =~ ^(.*[^0-9])?([0-9]+)([^0-9]*)$ ]]; then
        printf '%s%d%s' "${BASH_REMATCH[1]}" "$((BASH_REMATCH[2] + 1))" "${BASH_REMATCH[3]}"
    else
        printf '%s' "$1"
    fi
}

# answer <kind> <key> [default] — the upstream's reply: the pin itself, or one
# release newer under FAKE_DRIFT=1. <default> stands in for a key the catalog
# does not know (a companion MCP package, say).
answer() {
    local value
    value="$(catalog_get "$1" "$2")"
    [ -n "$value" ] || value="${3:-}"
    if [ "${FAKE_DRIFT:-0}" = "1" ] && [ -n "$value" ]; then
        bump "$value"
    else
        printf '%s' "$value"
    fi
}

# json_list <item>... — a JSON array of strings.
json_list() {
    local first=1 item
    printf '['
    for item in "$@"; do
        [ "$first" -eq 1 ] || printf ','
        first=0
        printf '"%s"' "$item"
    done
    printf ']'
}
