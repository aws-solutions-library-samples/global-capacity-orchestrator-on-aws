#!/usr/bin/env bats
# -----------------------------------------------------------------------------
# BATS tests for demo/gif_to_mp4.sh
# -----------------------------------------------------------------------------
# Exercises argument handling and the assembled ffmpeg invocation. ffmpeg is
# faked with a PATH shim that records its argv and touches the output file,
# so no real encoder is required.
#
# Run:  bats tests/BATS/test_gif_to_mp4.bats
# -----------------------------------------------------------------------------

SCRIPT="demo/gif_to_mp4.sh"

setup() {
    SHIMDIR="$(mktemp -d)"
    WORKDIR="$(mktemp -d)"
    # A fake ffmpeg that logs its argv and creates the output file (the
    # final argument), mimicking a successful encode.
    cat > "$SHIMDIR/ffmpeg" <<'SHIM'
#!/usr/bin/env bash
if [ -n "${GCO_SHIM_LOG:-}" ]; then printf '%s\n' "$*" >> "$GCO_SHIM_LOG"; fi
for last in "$@"; do :; done
touch "$last"
SHIM
    chmod +x "$SHIMDIR/ffmpeg"
    printf 'GIF89a' > "$WORKDIR/clip.gif"
}

teardown() {
    rm -rf "$SHIMDIR" "$WORKDIR"
}

# -- Static checks ------------------------------------------------------------

@test "gif_to_mp4.sh passes bash -n" {
    bash -n "$SCRIPT"
}

@test "gif_to_mp4.sh passes shellcheck" {
    command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

# -- Usage and argument validation ---------------------------------------------

@test "no arguments prints usage and exits 2" {
    run bash "$SCRIPT"
    [ "$status" -eq 2 ]
    [[ "$output" == *"Usage:"* ]]
}

@test "--help prints usage and exits 2" {
    run bash "$SCRIPT" --help
    [ "$status" -eq 2 ]
    [[ "$output" == *"gif_to_mp4.sh"* ]]
}

@test "missing ffmpeg fails with install guidance" {
    # PATH with only the essentials, no ffmpeg.
    stripped="$(mktemp -d)"
    for tool in bash grep sed du cut command; do
        p="$(command -v "$tool" 2>/dev/null)" && ln -s "$p" "$stripped/$tool" || true
    done
    PATH="$stripped" run bash "$SCRIPT" "$WORKDIR/clip.gif"
    rm -rf "$stripped"
    [ "$status" -eq 1 ]
    [[ "$output" == *"ffmpeg is not installed"* ]]
}

@test "missing input file fails clearly" {
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" "$WORKDIR/nope.gif"
    [ "$status" -eq 1 ]
    [[ "$output" == *"input GIF not found"* ]]
}

@test "non-gif input is rejected" {
    printf 'x' > "$WORKDIR/clip.mov"
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" "$WORKDIR/clip.mov"
    [ "$status" -eq 1 ]
    [[ "$output" == *"must be a .gif"* ]]
}

@test "identical input and output paths are rejected" {
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" "$WORKDIR/clip.gif" "$WORKDIR/clip.gif"
    [ "$status" -eq 1 ]
    [[ "$output" == *"same path"* ]]
}

@test "non-numeric MP4_FPS is rejected" {
    MP4_FPS=fast PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" "$WORKDIR/clip.gif"
    [ "$status" -eq 1 ]
    [[ "$output" == *"MP4_FPS must be a positive integer"* ]]
}

# -- ffmpeg invocation ----------------------------------------------------------

@test "default output path replaces .gif with .mp4" {
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" "$WORKDIR/clip.gif"
    [ "$status" -eq 0 ]
    [ -f "$WORKDIR/clip.mp4" ]
    [[ "$output" == *"MP4 written"* ]]
}

@test "explicit output path is honored" {
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" "$WORKDIR/clip.gif" "$WORKDIR/elsewhere.mp4"
    [ "$status" -eq 0 ]
    [ -f "$WORKDIR/elsewhere.mp4" ]
}

@test "ffmpeg argv carries the compatibility flags" {
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" "$WORKDIR/clip.gif"
    [ "$status" -eq 0 ]
    grep -q -- '-pix_fmt yuv420p' "$GCO_SHIM_LOG"
    grep -q -- '-movflags +faststart' "$GCO_SHIM_LOG"
    grep -q -- 'trunc(iw/2)\*2:trunc(ih/2)\*2' "$GCO_SHIM_LOG"
    grep -q -- "-i $WORKDIR/clip.gif" "$GCO_SHIM_LOG"
}

@test "MP4_FPS overrides the output frame rate" {
    export GCO_SHIM_LOG="$SHIMDIR/calls.log"
    : > "$GCO_SHIM_LOG"
    MP4_FPS=24 PATH="$SHIMDIR:$PATH" run bash "$SCRIPT" "$WORKDIR/clip.gif"
    [ "$status" -eq 0 ]
    grep -q -- '-r 24' "$GCO_SHIM_LOG"
    [[ "$output" == *"24 fps"* ]]
}
