#!/usr/bin/env python3
"""Safely validate the tracked demo GIF allowlist.

GIFs are untrusted binary input even though they cannot contain scripts. This
validator bounds decoder work, rejects disguised or malformed files, and fully
decodes every frame with Pillow inside the read-only security CI job.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024


@dataclass(frozen=True)
class GifPolicy:
    """Maximum accepted resource use for one intentionally tracked GIF."""

    max_bytes: int
    max_width: int
    max_height: int
    max_frames: int


# These ceilings leave modest re-recording headroom while keeping decoder work
# bounded. Any new GIF or intentional increase requires a review of this list.
GIF_POLICIES = {
    Path("demo/autopilot.gif"): GifPolicy(4 * MIB, 1024, 700, 800),
    Path("demo/deploy.gif"): GifPolicy(75 * MIB, 1360, 803, 1000),
    Path("demo/destroy.gif"): GifPolicy(2 * MIB, 1024, 744, 150),
    Path("demo/live_demo.gif"): GifPolicy(8 * MIB, 1024, 744, 250),
}
MAX_CANVAS_PIXELS = max(policy.max_width * policy.max_height for policy in GIF_POLICIES.values())


class ValidationError(ValueError):
    """A tracked media asset violates the reviewed GIF policy."""


def _consume_sub_blocks(data: bytes, offset: int, relative_path: Path) -> int:
    """Return the byte after a GIF data-sub-block sequence."""
    while True:
        if offset >= len(data):
            raise ValidationError(f"{relative_path}: truncated data-sub-block sequence")
        block_size = data[offset]
        offset += 1
        if block_size == 0:
            return offset
        offset += block_size
        if offset > len(data):
            raise ValidationError(f"{relative_path}: data sub-block exceeds file boundary")


def _validate_gif_structure(data: bytes, relative_path: Path) -> tuple[int, int, int]:
    """Parse GIF block boundaries and reject missing trailers or appended data."""
    if len(data) < 14 or data[:6] not in {b"GIF87a", b"GIF89a"}:
        raise ValidationError(f"{relative_path}: missing GIF87a/GIF89a signature")

    width, height = struct.unpack_from("<HH", data, 6)
    if width <= 0 or height <= 0:
        raise ValidationError(f"{relative_path}: invalid {width}x{height} canvas")

    offset = 13  # signature + logical screen descriptor
    logical_packed = data[10]
    if logical_packed & 0x80:
        offset += 3 * (1 << ((logical_packed & 0x07) + 1))
        if offset > len(data):
            raise ValidationError(f"{relative_path}: truncated global color table")

    frame_count = 0
    while offset < len(data):
        marker = data[offset]
        offset += 1
        if marker == 0x3B:  # GIF trailer
            if offset != len(data):
                raise ValidationError(
                    f"{relative_path}: {len(data) - offset:,} trailing bytes after GIF trailer"
                )
            if frame_count == 0:
                raise ValidationError(f"{relative_path}: GIF contains no image frames")
            return width, height, frame_count

        if marker == 0x21:  # extension
            if offset >= len(data):
                raise ValidationError(f"{relative_path}: truncated extension label")
            offset += 1
            offset = _consume_sub_blocks(data, offset, relative_path)
            continue

        if marker != 0x2C:  # image descriptor
            raise ValidationError(f"{relative_path}: invalid GIF block marker 0x{marker:02x}")
        if offset + 9 > len(data):
            raise ValidationError(f"{relative_path}: truncated image descriptor")

        left, top, frame_width, frame_height = struct.unpack_from("<HHHH", data, offset)
        image_packed = data[offset + 8]
        offset += 9
        if frame_width <= 0 or frame_height <= 0:
            raise ValidationError(f"{relative_path}: frame has an empty image rectangle")
        if left + frame_width > width or top + frame_height > height:
            raise ValidationError(f"{relative_path}: frame rectangle exceeds logical canvas")
        if image_packed & 0x80:
            offset += 3 * (1 << ((image_packed & 0x07) + 1))
            if offset > len(data):
                raise ValidationError(f"{relative_path}: truncated local color table")
        if offset >= len(data):
            raise ValidationError(f"{relative_path}: missing LZW code size")
        lzw_code_size = data[offset]
        offset += 1
        if not 2 <= lzw_code_size <= 8:
            raise ValidationError(f"{relative_path}: invalid LZW minimum code size {lzw_code_size}")
        offset = _consume_sub_blocks(data, offset, relative_path)
        frame_count += 1

    raise ValidationError(f"{relative_path}: missing GIF trailer")


def _tracked_gifs() -> set[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    return {
        Path(os.fsdecode(raw_path))
        for raw_path in result.stdout.split(b"\0")
        if raw_path and Path(os.fsdecode(raw_path)).suffix.lower() == ".gif"
    }


def _validate_allowlist() -> None:
    tracked = _tracked_gifs()
    expected = set(GIF_POLICIES)
    missing = sorted(expected - tracked)
    unexpected = sorted(tracked - expected)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing: " + ", ".join(map(str, missing)))
        if unexpected:
            details.append("not allowlisted: " + ", ".join(map(str, unexpected)))
        raise ValidationError("tracked GIF allowlist mismatch (" + "; ".join(details) + ")")


def _validate_gif(relative_path: Path, policy: GifPolicy) -> tuple[int, tuple[int, int], int]:
    path = PROJECT_ROOT / relative_path
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"{relative_path}: expected a regular file")

    file_size = path.stat().st_size
    if file_size > policy.max_bytes:
        raise ValidationError(
            f"{relative_path}: {file_size:,} bytes exceeds {policy.max_bytes:,}-byte limit"
        )

    data = path.read_bytes()
    width, height, parsed_frame_count = _validate_gif_structure(data, relative_path)
    del data
    if width > policy.max_width or height > policy.max_height:
        raise ValidationError(
            f"{relative_path}: {width}x{height} exceeds "
            f"{policy.max_width}x{policy.max_height} limit"
        )
    if parsed_frame_count > policy.max_frames:
        raise ValidationError(
            f"{relative_path}: {parsed_frame_count} frames exceeds {policy.max_frames}-frame limit"
        )
    decoded_pixels = width * height * parsed_frame_count
    pixel_budget = policy.max_width * policy.max_height * policy.max_frames
    if decoded_pixels > pixel_budget:
        raise ValidationError(
            f"{relative_path}: decoded pixel budget {decoded_pixels:,} exceeds {pixel_budget:,}"
        )

    # Pillow normally tolerates truncated images for some callers. Keep strict
    # decoding here and turn its decompression-bomb warning into a hard failure.
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    Image.MAX_IMAGE_PIXELS = MAX_CANVAS_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)

        with Image.open(path) as image:
            if image.format != "GIF":
                raise ValidationError(
                    f"{relative_path}: decoder identified {image.format!r}, not GIF"
                )
            if image.size != (width, height):
                raise ValidationError(
                    f"{relative_path}: parser/decoder canvas mismatch "
                    f"({width}x{height} versus {image.size[0]}x{image.size[1]})"
                )
            image.verify()

        # verify() intentionally invalidates the decoder, so reopen and load
        # every frame. This catches malformed LZW streams hidden after frame 1.
        with Image.open(path) as image:
            frame_count = getattr(image, "n_frames", 1)
            if frame_count != parsed_frame_count:
                raise ValidationError(
                    f"{relative_path}: parser found {parsed_frame_count} frames but "
                    f"decoder found {frame_count}"
                )
            for frame_number in range(frame_count):
                image.seek(frame_number)
                image.load()

    return file_size, (width, height), frame_count


def main() -> int:
    try:
        _validate_allowlist()
        for relative_path, policy in GIF_POLICIES.items():
            file_size, dimensions, frame_count = _validate_gif(relative_path, policy)
            print(
                f"PASS {relative_path}: {file_size:,} bytes, "
                f"{dimensions[0]}x{dimensions[1]}, {frame_count} frames"
            )
    except (OSError, subprocess.SubprocessError, ValidationError, Warning) as exc:
        print(f"GIF validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
