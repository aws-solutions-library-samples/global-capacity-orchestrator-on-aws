#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Convert a demo GIF to an MP4 for embedding outside GitHub
# ─────────────────────────────────────────────────────────────────────────────
# GitHub renders the committed GIFs directly, but most other surfaces
# (LinkedIn, Slack, blog posts, internal wikis) treat native video far
# better than GIFs — autoplay, scrubbing, and a fraction of the bytes.
# This helper converts any of the demo recordings (or any GIF) into a
# widely-compatible H.264 MP4:
#
#   - yuv420p pixel format (the one every player/platform accepts)
#   - +faststart (moov atom up front, so streaming starts immediately)
#   - even-dimension scaling (H.264 requires width/height % 2 == 0;
#     agg output is often odd-height)
#
# MP4s are deliberately NOT committed to the repository — demo/*.mp4 is
# gitignored, and the tracked-media policy in
# .github/scripts/validate_demo_gifs.py governs GIFs only. Generate the
# MP4 locally whenever you need one.
#
# Usage:
#   bash demo/gif_to_mp4.sh <input.gif> [output.mp4]
#
#   bash demo/gif_to_mp4.sh demo/autopilot.gif
#   bash demo/gif_to_mp4.sh demo/live_demo.gif /tmp/live_demo.mp4
#
# Options (via environment variables):
#   MP4_FPS=12    Output frame rate (default: 12 — plenty for terminal
#                 recordings, keeps files small)
#
# Prerequisites:
#   - ffmpeg: brew install ffmpeg  (macOS) or  apt install ffmpeg  (Linux)
#
# An existing output file is replaced (conversions are cheap and
# deterministic; the GIF remains the source of truth).
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

usage() {
    grep '^#' "$0" | sed -n '2,40p' | sed 's/^# \{0,1\}//'
}

if [ "$#" -lt 1 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    usage
    exit 2
fi

INPUT="$1"
OUTPUT="${2:-${INPUT%.gif}.mp4}"
FPS="${MP4_FPS:-12}"

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "error: ffmpeg is not installed." >&2
    echo "  brew install ffmpeg   (macOS)" >&2
    echo "  apt install ffmpeg    (Linux)" >&2
    exit 1
fi

if [ ! -f "$INPUT" ]; then
    echo "error: input GIF not found: $INPUT" >&2
    exit 1
fi

case "$INPUT" in
    *.gif) : ;;
    *) echo "error: input must be a .gif file, got: $INPUT" >&2; exit 1 ;;
esac

if [ "$INPUT" = "$OUTPUT" ]; then
    echo "error: input and output are the same path: $INPUT" >&2
    exit 1
fi

case "$FPS" in
    ''|*[!0-9]*) echo "error: MP4_FPS must be a positive integer, got: $FPS" >&2; exit 1 ;;
esac

# trunc(n/2)*2 rounds each dimension down to even, which H.264 + yuv420p
# require; lanczos keeps terminal text crisp through the (at most 1px)
# rescale.
ffmpeg -hide_banner -loglevel error -y \
    -i "$INPUT" \
    -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=lanczos" \
    -pix_fmt yuv420p \
    -movflags +faststart \
    -r "$FPS" \
    "$OUTPUT"

SIZE=$(du -h "$OUTPUT" | cut -f1)
echo "✓ MP4 written: ${OUTPUT} (${SIZE}, ${FPS} fps)"
echo "  Note: MP4s are gitignored on purpose — the committed GIF stays the source of truth."
