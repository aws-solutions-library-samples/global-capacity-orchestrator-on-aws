#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Shared library for GCO demo scripts
# ─────────────────────────────────────────────────────────────────────────────
# Sourced by live_demo.sh and record_demo.sh. Also sourced by BATS tests
# so the tests exercise the real functions, not duplicated copies.
#
# Usage:
#   source demo/lib_demo.sh
#
# shellcheck disable=SC2034  # Variables are used by sourcing scripts
# ─────────────────────────────────────────────────────────────────────────────

# ── Colors & Formatting ─────────────────────────────────────────────────────
# Uses tput for portability. Falls back to empty strings when there's no
# terminal or tput isn't available.

setup_colors() {
    if [ -t 1 ] && command -v tput &>/dev/null && [ "${TERM:-dumb}" != "dumb" ]; then
        BOLD=$(tput bold)
        DIM=$(tput dim)
        RESET=$(tput sgr0)
        CYAN=$(tput setaf 6)
        GREEN=$(tput setaf 2)
        YELLOW=$(tput setaf 3)
        MAGENTA=$(tput setaf 5)
        BLUE=$(tput setaf 4)
        WHITE=$(tput setaf 7)
        RED=$(tput setaf 1)
        BG_BLUE=$(tput setab 4)
    else
        BOLD="" DIM="" RESET="" CYAN="" GREEN="" YELLOW=""
        MAGENTA="" BLUE="" WHITE="" RED="" BG_BLUE=""
    fi
}

# ── Pause Durations ─────────────────────────────────────────────────────────
# GCO_DEMO_FAST=1 shortens pauses for rehearsal or recording.

setup_pauses() {
    PAUSE_SHORT="${GCO_DEMO_FAST:+1}"
    PAUSE_SHORT="${PAUSE_SHORT:-3}"
    PAUSE_LONG="${GCO_DEMO_FAST:+2}"
    PAUSE_LONG="${PAUSE_LONG:-5}"
}

# ── Display Helpers ──────────────────────────────────────────────────────────

banner() {
    # Use terminal width if available, otherwise default to 72.
    # This ensures the banner fills the recording frame nicely.
    local width
    width=$(tput cols 2>/dev/null || echo "72")
    # Cap at 120 to avoid absurdly wide banners on ultrawide terminals
    if [ "$width" -gt 120 ]; then width=120; fi
    local text="$1"
    local text_len=${#text}
    local pad_left=$(( (width - text_len) / 2 ))
    local pad_right=$(( width - text_len - pad_left ))
    echo ""
    printf "%s%s%s%*s%s\n" "$BG_BLUE" "$WHITE" "$BOLD" "$width" "" "$RESET"
    printf "%s%s%s%*s%s%*s%s\n" "$BG_BLUE" "$WHITE" "$BOLD" "$pad_left" "" "$text" "$pad_right" "" "$RESET"
    printf "%s%s%s%*s%s\n" "$BG_BLUE" "$WHITE" "$BOLD" "$width" "" "$RESET"
    echo ""
}

section_header() {
    local num="$1"
    local title="$2"
    local color="${3:-$CYAN}"
    # Build a divider line that fills the terminal width (capped at 120)
    local width
    width=$(tput cols 2>/dev/null || echo "72")
    if [ "$width" -gt 120 ]; then width=120; fi
    local divider
    divider=$(printf '%*s' "$width" '' | tr ' ' '━')
    echo ""
    echo "${color}${BOLD}${divider}${RESET}"
    echo "${color}${BOLD}  [$num]  $title${RESET}"
    echo "${color}${BOLD}${divider}${RESET}"
    echo ""
}

narrate()   { echo "  ${DIM}$1${RESET}"; }
highlight() { echo "  ${YELLOW}${BOLD}▸ $1${RESET}"; }
success()   { echo "  ${GREEN}${BOLD}✓ $1${RESET}"; }
warn()      { echo "  ${RED}${BOLD}⚠ $1${RESET}"; }
spacer()    { echo ""; }

feature_status() {
    local value="$1"
    if [ "$value" = "true" ]; then
        echo "${GREEN}enabled${RESET}"
    else
        echo "${DIM}disabled${RESET}"
    fi
}

run_cmd() {
    echo ""
    echo "  ${MAGENTA}\$ ${WHITE}${BOLD}$1${RESET}"
    echo "  ${DIM}────────────────────────────────────────────────────────────${RESET}"
    eval "$1" 2>&1 | sed 's/^/  /'
    local exit_code=${PIPESTATUS[0]}
    echo "  ${DIM}────────────────────────────────────────────────────────────${RESET}"
    if [ "$exit_code" -ne 0 ]; then
        warn "Command exited with code $exit_code"
    fi
    return "$exit_code"
}

pause_for_audience() {
    if [ "${GCO_DEMO_NONINTERACTIVE:-}" = "1" ]; then
        sleep 1
        return
    fi
    echo ""
    echo "  ${DIM}Press Enter to continue...${RESET}"
    read -r
}

countdown() {
    local msg="$1"
    local secs="$2"
    for i in $(seq "$secs" -1 1); do
        printf "\r  %s%s %d...%s" "$DIM" "$msg" "$i" "$RESET"
        sleep 1
    done
    printf "\r  %s%-60s%s\n" "$DIM" "$msg done." "$RESET"
}

# wait_for_job <job-name> <namespace> [timeout_seconds]
#
# Waits for a Kubernetes Job to reach the ``complete`` condition, showing a
# live-updating progress indicator until it succeeds, fails, or times out.
# Designed for the live demo where we want the audience to actually see the
# job's final logs — a fixed-duration ``sleep`` used to fall short when
# image pulls or node provisioning pushed completion past the window.
#
# Arguments:
#   job-name         Name of the ``batch/v1`` Job resource.
#   namespace        Namespace containing the job.
#   timeout_seconds  Optional wall-clock budget (default: 240). This is a
#                    deadline, not a target — if the job finishes sooner
#                    we return immediately. The budget deliberately does
#                    not shrink in ``GCO_DEMO_FAST=1`` mode: that flag is
#                    for narration pauses, not real work.
#
# The helper *always* returns 0, even on timeout or failure. Callers are
# running under ``set -euo pipefail`` and we don't want a slow job to kill
# the entire recording mid-demo — the next ``kubectl logs`` / ``kubectl
# get`` call will surface the state naturally. On timeout we print the pod
# status so the next narration makes sense instead of showing a blank log
# block.
wait_for_job() {
    local job="$1"
    local ns="$2"
    local budget="${3:-240}"
    # NOTE: GCO_DEMO_FAST is for narration pauses, not for real work. Jobs
    # still need as long as they need. If the caller explicitly passes a
    # smaller budget via $3, that wins.

    local start=$SECONDS
    local deadline=$((start + budget))

    # First tick: the Job resource itself may not have appeared in the API
    # yet (submit-direct returns before the apply is persisted across the
    # control plane on a cold cluster). Spin briefly until it does.
    while [ "$SECONDS" -lt "$deadline" ]; do
        if kubectl get "job/${job}" -n "$ns" >/dev/null 2>&1; then
            break
        fi
        printf "\r  %sWaiting for job/%s to register...%s" "$DIM" "$job" "$RESET"
        sleep 1
    done

    # Use kubectl's own wait primitive for the remainder of the budget. It
    # returns immediately once the condition is met, so this is both faster
    # than polling and more accurate than a fixed sleep.
    local remaining=$((deadline - SECONDS))
    if [ "$remaining" -lt 5 ]; then remaining=5; fi

    printf "\r  %sWaiting for job/%s to complete (up to %ds)...%s\n" \
        "$DIM" "$job" "$remaining" "$RESET"

    if kubectl wait --for=condition=complete "job/${job}" \
            -n "$ns" --timeout="${remaining}s" >/dev/null 2>&1; then
        local elapsed=$((SECONDS - start))
        printf "  %s${GREEN}${BOLD}✓${RESET} %sjob/%s completed in %ds%s\n" \
            "" "$DIM" "$job" "$elapsed" "$RESET"
        return 0
    fi

    # Timed out or job failed — show what the pod is doing so the audience
    # sees meaningful context before we hit ``kubectl logs`` on a non-ready
    # pod. We always return 0 so ``set -e`` callers don't die on a slow job.
    printf "  %s${YELLOW}${BOLD}!${RESET} %sjob/%s still running after %ds — showing latest pod status%s\n" \
        "" "$DIM" "$job" "$budget" "$RESET"
    kubectl get pods -n "$ns" \
        -l "job-name=${job}" --no-headers 2>/dev/null | sed 's/^/    /' || true
    return 0
}

# ── Feature Detection ────────────────────────────────────────────────────────
# Reads cdk.json and sets global variables for each feature flag.
# Requires jq and CDK_JSON to be set.

detect_features() {
    local cdk="${1:-cdk.json}"
    VOLCANO_ENABLED=$(jq -r '.context.helm.volcano.enabled // false' "$cdk")
    KUEUE_ENABLED=$(jq -r '.context.helm.kueue.enabled // false' "$cdk")
    YUNIKORN_ENABLED=$(jq -r '.context.helm.yunikorn.enabled // false' "$cdk")
    SLURM_ENABLED=$(jq -r '.context.helm.slurm.enabled // false' "$cdk")
    FSX_ENABLED=$(jq -r '.context.fsx_lustre.enabled // false' "$cdk")
    VALKEY_ENABLED=$(jq -r '.context.valkey.enabled // false' "$cdk")
    AURORA_PGVECTOR_ENABLED=$(jq -r '.context.aurora_pgvector.enabled // false' "$cdk")
}

detect_region() {
    local cdk="${1:-cdk.json}"
    REGION="${GCO_DEMO_REGION:-$(jq -r '.context.deployment_regions.regional[0] // "us-east-1"' "$cdk")}"
}

detect_endpoint_access() {
    local cdk="${1:-cdk.json}"
    ENDPOINT_ACCESS=$(jq -r '.context.eks_cluster.endpoint_access // "PRIVATE"' "$cdk")
}

# ── Section Counter ──────────────────────────────────────────────────────────

# Section counter — can't use $(next_section) because command substitution
# runs in a subshell and the counter increment is lost. Instead we increment
# inline and use the variable directly.
SECTION=0

# ── ARN Helpers (shared with setup-cluster-access.sh) ────────────────────────

# Checks if an ARN is an assumed-role ARN.
is_assumed_role() {
    [[ "$1" == *":assumed-role/"* ]]
}

# Extracts the role name from an assumed-role ARN.
# Input:  arn:aws:sts::123456789012:assumed-role/MyRole/session-name
# Output: MyRole
extract_role_name() {
    echo "$1" | sed 's/.*:assumed-role\/\([^\/]*\)\/.*/\1/'
}

# Reconstructs an IAM role ARN from an assumed-role ARN and account ID.
# Input:  role_name, account_id
# Output: arn:aws:iam::123456789012:role/MyRole
build_role_arn() {
    local role_name="$1"
    local account_id="$2"
    echo "arn:aws:iam::${account_id}:role/${role_name}"
}

# ── Recording Helpers ────────────────────────────────────────────────────────

# Default font family used when rendering .cast files to GIFs with agg.
#
# agg's text renderer (resvg/usvg) is first-family-wins — it does not do
# per-glyph fallback down the family list like a GUI text engine would. So
# Menlo is kept first because it covers the characters our scripts emit
# (box-drawing, arrows, geometric shapes, and the dingbats ✓ ✗ ⚠ ▸). Any
# codepoint Menlo doesn't have (typically color-emoji pictographs from CDK
# output like ✨ and ✅, or the information symbol ℹ) would otherwise fall
# through to ``.LastResort`` and render as a tofu box.
#
# We fix that upstream instead of with more font fallbacks: every cast file
# runs through ``strip_emoji_from_cast`` before ``render_gif``, which maps
# the known tofu-triggering codepoints to safe monochrome equivalents.
# After that pass, Menlo covers every character in the cast and agg never
# needs to fall back.
#
# Override via the DEMO_FONT_FAMILY environment variable if you need to
# skip this substitution and use a font that has real coverage of those
# codepoints (e.g. a full Unicode monospace font).
DEMO_FONT_FAMILY_DEFAULT="Menlo,Monaco,Courier New"

# verify_recording_git_state <repo_root> [allowed_dirty_path ...]
#
# When GCO_EXPECTED_GIT_SHA is set, verifies that the checkout is exactly that
# full commit and rejects every dirty/untracked path except the explicitly
# supplied recorder outputs. The output allowlist lets a destroy recording
# follow a deploy recording before the four generated artifacts are committed,
# while still proving that the source tree matches the CI-green commit.
verify_recording_git_state() {
    local repo_root="$1"
    shift
    local expected="${GCO_EXPECTED_GIT_SHA:-}"
    if [ -z "$expected" ]; then
        return 0
    fi
    if [ "${#expected}" -ne 40 ] || [[ "$expected" == *[!0-9a-fA-F]* ]]; then
        echo "GCO_EXPECTED_GIT_SHA must be a full 40-character hexadecimal SHA." >&2
        return 1
    fi

    local actual
    if ! actual=$(git -C "$repo_root" rev-parse HEAD 2>/dev/null); then
        echo "Unable to resolve git HEAD in $repo_root." >&2
        return 1
    fi
    local expected_normalized actual_normalized
    expected_normalized=$(printf '%s' "$expected" | tr '[:upper:]' '[:lower:]')
    actual_normalized=$(printf '%s' "$actual" | tr '[:upper:]' '[:lower:]')
    if [ "$actual_normalized" != "$expected_normalized" ]; then
        echo "Git HEAD does not match GCO_EXPECTED_GIT_SHA." >&2
        return 1
    fi

    local dirty
    if ! dirty=$(git -C "$repo_root" status --porcelain=v1 --untracked-files=all); then
        echo "Unable to inspect git worktree state in $repo_root." >&2
        return 1
    fi

    local line path candidate is_allowed rename_source rename_destination
    local unexpected=""
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        path="${line:3}"
        rename_source=""
        rename_destination="$path"
        case "$path" in
            *" -> "*)
                rename_source="${path%% -> *}"
                rename_destination="${path##* -> }"
                ;;
        esac
        # Porcelain-v1 renders renames as ``source -> destination``. Both
        # paths must be allowlisted: accepting only the destination would let
        # a recorder delete or move arbitrary tracked source files.
        for path in "$rename_source" "$rename_destination"; do
            [ -n "$path" ] || continue
            is_allowed=0
            for candidate in "$@"; do
                if [ "$path" = "$candidate" ]; then
                    is_allowed=1
                    break
                fi
            done
            if [ "$is_allowed" -ne 1 ]; then
                unexpected="${unexpected}${unexpected:+, }${path}"
            fi
        done
    done <<< "$dirty"

    if [ -n "$unexpected" ]; then
        echo "Unexpected dirty paths for guarded recording: $unexpected" >&2
        return 1
    fi
}

# verify_recording_aws_account
#
# When GCO_EXPECTED_ACCOUNT_ID is set, resolves the active caller through STS
# and requires an exact match before a recorder can mutate infrastructure.
verify_recording_aws_account() {
    local expected="${GCO_EXPECTED_ACCOUNT_ID:-}"
    if [ -z "$expected" ]; then
        return 0
    fi
    if ! [[ "$expected" =~ ^[0-9]{12}$ ]]; then
        echo "GCO_EXPECTED_ACCOUNT_ID must contain exactly 12 digits." >&2
        return 1
    fi

    local actual
    if ! actual=$(aws sts get-caller-identity --query Account --output text 2>/dev/null); then
        echo "Unable to resolve the active AWS account through STS." >&2
        return 1
    fi
    if [ "$actual" != "$expected" ]; then
        echo "Active AWS account does not match GCO_EXPECTED_ACCOUNT_ID." >&2
        return 1
    fi
}

# sanitize_cast <cast_file>
#
# Redacts AWS account IDs and temporary/long-lived AWS access-key IDs from an
# asciinema recording. Account-ID-shaped values become 000000000000 and
# AKIA/ASIA access-key IDs become REDACTED_AWS_ACCESS_KEY_ID. Operates in place.
#
# The account heuristic is intentionally broad: unrelated standalone 12-digit
# values are also redacted. Over-redaction is safer than allowing an account ID
# into a committed cast or the GIF rendered from it.
#
# Use SKIP_SANITIZE=1 only to debug a local recording. Bypassed artifacts must
# never be committed or distributed.
sanitize_cast() {
    local cast_file="$1"
    if [ "${SKIP_SANITIZE:-}" = "1" ]; then
        return
    fi
    if [ ! -f "$cast_file" ]; then
        return
    fi

    python3 - "$cast_file" <<'PYEOF'
import json
import re
import sys
from pathlib import Path

ACCOUNT_ID = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")
ACCESS_KEY_ID = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")
PATTERNS = (
    (ACCOUNT_ID, "000000000000"),
    (ACCESS_KEY_ID, "REDACTED_AWS_ACCESS_KEY_ID"),
)


def redactions(text):
    edits = [
        (match.start(), match.end(), replacement)
        for pattern, replacement in PATTERNS
        for match in pattern.finditer(text)
    ]
    edits.sort(key=lambda edit: (edit[0], edit[1]))
    return edits


def redact_text(text):
    edits = redactions(text)
    parts = []
    cursor = 0
    for start, end, replacement in edits:
        if start < cursor:
            continue
        parts.extend((text[cursor:start], replacement))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def redact_value(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value


def mapped_offset(position, edits):
    source_cursor = 0
    output_cursor = 0
    for start, end, replacement in edits:
        if position <= start:
            return output_cursor + position - source_cursor
        output_cursor += start - source_cursor
        if position < end:
            # Assign a replacement spanning event boundaries to the event in
            # which the sensitive value began; later fragments become empty.
            return output_cursor + len(replacement)
        output_cursor += len(replacement)
        source_cursor = end
    return output_cursor + position - source_cursor


path = Path(sys.argv[1])
documents = []
for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
    if not line.strip():
        raise ValueError(f"blank line {line_number} is not valid cast NDJSON")
    documents.append(json.loads(line))

output_events = []
for index, document in enumerate(documents):
    if isinstance(document, list) and len(document) >= 3 and document[1] == "o":
        if not isinstance(document[2], str):
            raise ValueError(f"output event {index + 1} has a non-string payload")
        output_events.append(document)
    else:
        documents[index] = redact_value(document)

# Redact each payload first so a complete identifier cannot be hidden by
# adjacent event content that turns it into one longer alphanumeric run.
for event in output_events:
    event[2] = redact_text(event[2])

# Then redact the concatenated rendered terminal so identifiers split by
# asciinema's event boundaries cannot evade either pattern.
source = "".join(event[2] for event in output_events)
edits = redactions(source)
rendered = redact_text(source)
offset = 0
for event in output_events:
    start = offset
    offset += len(event[2])
    event[2] = rendered[mapped_offset(start, edits):mapped_offset(offset, edits)]

serialized = "\n".join(
    json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    for document in documents
)
path.write_text(serialized + ("\n" if documents else ""), encoding="utf-8")
PYEOF
}

# verify_cast_sanitized <cast_file>
#
# Independently verifies the sanitizer's postcondition without printing the
# matched values. The all-zero account placeholder is allowed; every other
# standalone 12-digit value and every AKIA/ASIA access-key ID fails closed.
verify_cast_sanitized() {
    local cast_file="$1"
    if [ "${SKIP_SANITIZE:-}" = "1" ]; then
        return
    fi
    if [ ! -f "$cast_file" ]; then
        echo "Cannot verify missing cast file: $cast_file" >&2
        return 1
    fi

    python3 - "$cast_file" <<'PYEOF'
import json
import re
import sys
from pathlib import Path

ACCOUNT_ID = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")
ACCESS_KEY_ID = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")


def string_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from string_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from string_values(item)


documents = []
path = Path(sys.argv[1])
for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
    if not line.strip():
        raise ValueError(f"blank line {line_number} is not valid cast NDJSON")
    documents.append(json.loads(line))

output_payloads = []
non_output_strings = []
for index, document in enumerate(documents):
    if isinstance(document, list) and len(document) >= 3 and document[1] == "o":
        if not isinstance(document[2], str):
            raise ValueError(f"output event {index + 1} has a non-string payload")
        output_payloads.append(document[2])
        non_output_strings.extend(string_values(document[:2]))
        non_output_strings.extend(string_values(document[3:]))
    else:
        non_output_strings.extend(string_values(document))

# Verify decoded content, not raw JSON serialization. This independently
# reconstructs the rendered output stream and catches identifiers split across
# adjacent output events while still checking header/non-output string fields.
texts = [*output_payloads, "".join(output_payloads), *non_output_strings]
account_ids = [
    match.group(0)
    for text in texts
    for match in ACCOUNT_ID.finditer(text)
    if match.group(0) != "000000000000"
]
access_key_ids = [
    match.group(0)
    for text in texts
    for match in ACCESS_KEY_ID.finditer(text)
]
if account_ids or access_key_ids:
    print(
        "Cast sanitization verification failed: "
        f"{len(account_ids)} account-ID pattern(s), "
        f"{len(access_key_ids)} access-key-ID pattern(s) remain.",
        file=sys.stderr,
    )
    raise SystemExit(1)
PYEOF
}

# strip_emoji_from_cast <cast_file>
#
# Rewrites tofu-triggering Unicode codepoints in a .cast file to ASCII or
# to monochrome glyphs Menlo can render, so agg never falls back to
# ``.LastResort`` during GIF conversion.
#
# Background: agg uses resvg/usvg, a pure-vector text renderer. When the
# first font in the family list can't render a glyph, usvg falls back to
# ``.LastResort`` (the system tofu font) rather than iterating the family
# list. Color emoji fonts like Apple Color Emoji don't help because they're
# bitmap (sbix/COLR) fonts, which usvg cannot use.
#
# This helper runs in-place with Python 3 for portable Unicode handling.
# The substitutions:
#   ℹ (INFORMATION SOURCE, U+2139)   → i       Menlo has no glyph
#   ✅ (WHITE HEAVY CHECK MARK, U+2705) → ✓     Menlo has ✓, not ✅
#   ✨ (SPARKLES, U+2728)            → *       Menlo has no glyph
#   📦 (PACKAGE, U+1F4E6)            → [pkg]   Menlo has no glyph
#   🚀 (ROCKET, U+1F680)             → >>      Menlo has no glyph
#
# Use SKIP_EMOJI_STRIP=1 to bypass (useful when you're confident your font
# chain renders everything correctly and don't want the substitutions).
strip_emoji_from_cast() {
    local cast_file="$1"
    if [ "${SKIP_EMOJI_STRIP:-}" = "1" ]; then
        return
    fi
    if [ ! -f "$cast_file" ]; then
        return
    fi
    # Python handles Unicode character substitution cleanly across GNU and
    # BSD sed variants, and lets us express the character set as a readable
    # translation table rather than cramming UTF-8 byte sequences into a
    # fragile sed one-liner.
    python3 - "$cast_file" <<'PYEOF'
import sys
from pathlib import Path

# Single-character substitutions (str.translate with the ord key).
SINGLE = {
    0x2139: "i",        # ℹ INFORMATION SOURCE → i
    0x2705: "\u2713",   # ✅ WHITE HEAVY CHECK MARK → ✓ (monochrome check, in Menlo)
    0x2728: "*",        # ✨ SPARKLES → *
}

# Multi-character substitutions applied after the translate pass.
MULTI = {
    "\U0001F4E6": "[pkg]",   # 📦 PACKAGE
    "\U0001F680": ">>",      # 🚀 ROCKET
}

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = text.translate(SINGLE)
for src, dst in MULTI.items():
    text = text.replace(src, dst)
path.write_text(text, encoding="utf-8")
PYEOF
}

# render_gif <cast_file> <gif_file> <speed> <theme> <cols> <rows>
#
# Converts an asciinema .cast file to an animated GIF using agg with the
# shared font-family fallback chain. Centralised here so all three
# record scripts render consistent-looking output.
#
# The DEMO_FONT_FAMILY env var overrides the default fallback list.
render_gif() {
    local cast_file="$1"
    local gif_file="$2"
    local speed="$3"
    local theme="$4"
    local cols="$5"
    local rows="$6"
    local font_family="${DEMO_FONT_FAMILY:-$DEMO_FONT_FAMILY_DEFAULT}"

    agg \
        --speed "$speed" \
        --theme "$theme" \
        --font-family "$font_family" \
        --font-size 14 \
        --cols "$cols" \
        --rows "$rows" \
        "$cast_file" \
        "$gif_file"
}

# ── Recording Publication Transaction ───────────────────────────────────────
#
# A cast and its GIF cannot be switched with one POSIX rename. These globals
# describe the narrow interval in which one file may have been switched but the
# other has not. Recorder EXIT/signal cleanup calls rollback_recording_publication
# so every handled failure restores the complete previous pair (or removes both
# newly-created outputs when no previous pair existed).
RECORDING_PUBLICATION_IN_PROGRESS=0
RECORDING_PUBLICATION_COMPLETE=0
RECORDING_PUBLICATION_STAGE_DIR=""
RECORDING_PUBLICATION_CAST_FILE=""
RECORDING_PUBLICATION_GIF_FILE=""
RECORDING_PUBLICATION_CAST_BACKUP=""
RECORDING_PUBLICATION_GIF_BACKUP=""
RECORDING_PUBLICATION_HAD_CAST=0
RECORDING_PUBLICATION_HAD_GIF=0

# recording_publication_restore_file <backup> <final> <restore-staging-path>
#
# Copies the preserved artifact to another same-filesystem staging path before
# renaming it over the final. The original backup remains available if this
# restoration attempt is interrupted or fails and the EXIT trap retries it.
recording_publication_restore_file() {
    local backup_file="$1"
    local final_file="$2"
    local restore_file="$3"

    rm -f "$restore_file"
    if ! cp -p "$backup_file" "$restore_file"; then
        return 1
    fi
    if ! mv -f "$restore_file" "$final_file"; then
        return 1
    fi
}

# rollback_recording_publication
#
# Restores both artifacts represented by the active publication transaction.
# Backups are copied rather than consumed so an EXIT cleanup can retry after a
# partial restoration failure. The transaction remains active until both final
# paths match their pre-publication state.
rollback_recording_publication() {
    if [ "${RECORDING_PUBLICATION_IN_PROGRESS:-0}" != "1" ]; then
        return 0
    fi

    RECORDING_PUBLICATION_COMPLETE=0
    local rollback_status=0

    if [ "$RECORDING_PUBLICATION_HAD_CAST" = "1" ]; then
        if ! recording_publication_restore_file \
                "$RECORDING_PUBLICATION_CAST_BACKUP" \
                "$RECORDING_PUBLICATION_CAST_FILE" \
                "${RECORDING_PUBLICATION_STAGE_DIR}/.restore-cast"; then
            rollback_status=1
        fi
    elif ! rm -f "$RECORDING_PUBLICATION_CAST_FILE"; then
        rollback_status=1
    fi

    if [ "$RECORDING_PUBLICATION_HAD_GIF" = "1" ]; then
        if ! recording_publication_restore_file \
                "$RECORDING_PUBLICATION_GIF_BACKUP" \
                "$RECORDING_PUBLICATION_GIF_FILE" \
                "${RECORDING_PUBLICATION_STAGE_DIR}/.restore-gif"; then
            rollback_status=1
        fi
    elif ! rm -f "$RECORDING_PUBLICATION_GIF_FILE"; then
        rollback_status=1
    fi

    if [ "$rollback_status" -eq 0 ]; then
        RECORDING_PUBLICATION_IN_PROGRESS=0
    fi
    return "$rollback_status"
}

# publish_recording_artifacts <staged-cast> <staged-gif-or-empty> <final-cast> <final-gif>
#
# Publishes a prepared cast/GIF pair with rollback across the two individually
# atomic same-filesystem renames. An empty staged GIF means the final GIF must
# be absent (SKIP_GIF mode). Existing finals are preserved before either final
# is changed. On any ordinary command failure this helper rolls both paths back;
# recorder EXIT/HUP/INT/TERM handling covers interruption between commands.
publish_recording_artifacts() {
    local staged_cast="$1"
    local staged_gif="$2"
    local final_cast="$3"
    local final_gif="$4"

    if [ "${RECORDING_PUBLICATION_IN_PROGRESS:-0}" = "1" ]; then
        echo "A recording publication transaction is already active." >&2
        return 1
    fi
    if [ ! -f "$staged_cast" ]; then
        echo "Cannot publish missing staged cast: $staged_cast" >&2
        return 1
    fi
    if [ -n "$staged_gif" ] && [ ! -f "$staged_gif" ]; then
        echo "Cannot publish missing staged GIF: $staged_gif" >&2
        return 1
    fi
    if [ -d "$final_cast" ] || [ -d "$final_gif" ]; then
        echo "Recording publication destinations must be files." >&2
        return 1
    fi

    local stage_dir
    local cast_backup
    local gif_backup
    local had_cast=0
    local had_gif=0
    stage_dir=$(dirname "$staged_cast")
    cast_backup="${stage_dir}/.previous-cast"
    gif_backup="${stage_dir}/.previous-gif"
    rm -f "$cast_backup" "$gif_backup" \
        "${stage_dir}/.restore-cast" "${stage_dir}/.restore-gif"

    # Do not mark the transaction active until both required backups are fully
    # copied. An interruption during this phase leaves the final paths intact.
    if [ -e "$final_cast" ]; then
        if ! cp -p "$final_cast" "$cast_backup"; then
            return 1
        fi
        had_cast=1
    fi
    if [ -e "$final_gif" ]; then
        if ! cp -p "$final_gif" "$gif_backup"; then
            return 1
        fi
        had_gif=1
    fi

    RECORDING_PUBLICATION_STAGE_DIR="$stage_dir"
    RECORDING_PUBLICATION_CAST_FILE="$final_cast"
    RECORDING_PUBLICATION_GIF_FILE="$final_gif"
    RECORDING_PUBLICATION_CAST_BACKUP="$cast_backup"
    RECORDING_PUBLICATION_GIF_BACKUP="$gif_backup"
    RECORDING_PUBLICATION_HAD_CAST="$had_cast"
    RECORDING_PUBLICATION_HAD_GIF="$had_gif"
    RECORDING_PUBLICATION_COMPLETE=0
    RECORDING_PUBLICATION_IN_PROGRESS=1

    local publication_status
    if mv -f "$staged_cast" "$final_cast"; then
        :
    else
        publication_status=$?
        if ! rollback_recording_publication; then
            echo "Recording publication failed and rollback could not complete." >&2
            return 1
        fi
        return "$publication_status"
    fi

    if [ -n "$staged_gif" ]; then
        if mv -f "$staged_gif" "$final_gif"; then
            :
        else
            publication_status=$?
            if ! rollback_recording_publication; then
                echo "Recording publication failed and rollback could not complete." >&2
                return 1
            fi
            return "$publication_status"
        fi
    elif rm -f "$final_gif"; then
        :
    else
        publication_status=$?
        if ! rollback_recording_publication; then
            echo "Recording publication failed and rollback could not complete." >&2
            return 1
        fi
        return "$publication_status"
    fi

    # Both final-path operations succeeded. If a handled signal arrives before
    # IN_PROGRESS is cleared, the active transaction safely restores both old
    # paths; after it is cleared, the new pair is already internally consistent.
    RECORDING_PUBLICATION_COMPLETE=1
    RECORDING_PUBLICATION_IN_PROGRESS=0
}
