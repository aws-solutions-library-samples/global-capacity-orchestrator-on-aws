"""Tests for the demo GIF allowlist validator (``.github/scripts/validate_demo_gifs.py``).

The validator treats every tracked GIF as untrusted binary input. It parses the
block structure itself — before handing the file to Pillow — so that a disguised
or malformed file is rejected by a bounded, hand-written parser rather than by
whatever the decoder happens to do with it. That parser is the thing worth
testing exhaustively: each rejection is one specific byte-level malformation,
and a parser that accepted any of them would let a crafted file reach Pillow.

Fixtures are real GIFs written by Pillow and then mutated at known offsets, so
each test exercises exactly one rule. The allowlist and policy tables are
module-level data, and ``PROJECT_ROOT`` is a module-level path, so the
end-to-end path is driven against a throwaway git repository under ``tmp_path``
rather than the real ``demo/`` assets — nothing here reads the committed GIFs.
"""

from __future__ import annotations

import importlib.util
import io
import struct
import subprocess  # nosec B404 - fixed argv, no shell: builds a throwaway git repo
import sys
from pathlib import Path

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / ".github" / "scripts" / "validate_demo_gifs.py"

_spec = importlib.util.spec_from_file_location("validate_demo_gifs", _SCRIPT)
assert _spec is not None and _spec.loader is not None
validator = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("validate_demo_gifs", validator)
_spec.loader.exec_module(validator)

GIT = "/usr/bin/git"


# ---------------------------------------------------------------------------
# GIF builders
# ---------------------------------------------------------------------------


def _gif_bytes(
    *, width: int = 8, height: int = 6, frames: int = 2, fill: int | None = None
) -> bytes:
    """A real animated GIF from Pillow, with a visibly varied first frame.

    Each frame is a distinct solid colour with a contrasting block in one corner,
    so the "first frame is blank" check has non-background pixels to count. With
    ``fill`` set, frame zero is one flat colour instead.
    """
    images = []
    for index in range(frames):
        # RGB, not palette mode: Pillow collapses palette frames that look alike
        # into one, which silently produced single-frame fixtures.
        image = Image.new("RGB", (width, height), color=(10 * index + 20, 40, 200 - 10 * index))
        if not (index == 0 and fill is not None):
            for x in range(width // 2):
                for y in range(height // 2):
                    image.putpixel((x, y), (250, 250 - 10 * index, 30))
        images.append(image)
    buffer = io.BytesIO()
    images[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        loop=0,
        optimize=False,
        disposal=2,
    )
    return buffer.getvalue()


def _image_descriptor_offsets(data: bytes) -> list[int]:
    """Offsets of every ``0x2C`` image-descriptor marker, by walking the blocks."""
    offset = 13
    packed = data[10]
    if packed & 0x80:
        offset += 3 * (1 << ((packed & 0x07) + 1))
    found: list[int] = []
    while offset < len(data):
        marker = data[offset]
        offset += 1
        if marker == 0x3B:
            break
        if marker == 0x21:
            offset += 1
            offset = validator._consume_sub_blocks(data, offset, Path("x.gif"))
            continue
        assert marker == 0x2C, hex(marker)
        found.append(offset - 1)
        image_packed = data[offset + 8]
        offset += 9
        if image_packed & 0x80:
            offset += 3 * (1 << ((image_packed & 0x07) + 1))
        offset += 1  # LZW minimum code size
        offset = validator._consume_sub_blocks(data, offset, Path("x.gif"))
    return found


def _structure(data: bytes) -> tuple[int, int, int]:
    return validator._validate_gif_structure(data, Path("x.gif"))


def _rejects(data: bytes, match: str) -> None:
    with pytest.raises(validator.ValidationError, match=match):
        _structure(data)


# ---------------------------------------------------------------------------
# Structural parser
# ---------------------------------------------------------------------------


def test_a_real_gif_parses_to_its_canvas_and_frame_count() -> None:
    data = _gif_bytes(width=8, height=6, frames=3)
    assert _structure(data) == (8, 6, 3)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"GIF89a" + b"\x00" * 7, id="shorter-than-a-header"),
        pytest.param(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, id="png-with-gif-suffix"),
        pytest.param(b"GIF90a" + b"\x00" * 20, id="unknown-version"),
    ],
)
def test_a_missing_or_foreign_signature_is_rejected(data: bytes) -> None:
    """A disguised file must fail at the first six bytes, before any parsing."""
    _rejects(data, "missing GIF87a/GIF89a signature")


def test_a_zero_dimension_canvas_is_rejected() -> None:
    data = bytearray(_gif_bytes())
    struct.pack_into("<HH", data, 6, 0, 6)
    _rejects(bytes(data), "invalid 0x6 canvas")


def test_a_truncated_global_color_table_is_rejected() -> None:
    data = _gif_bytes()
    assert data[10] & 0x80, "fixture must carry a global colour table"
    _rejects(data[:14], "truncated global color table")


def test_a_gif_with_no_trailer_is_rejected() -> None:
    data = _gif_bytes()
    assert data[-1] == 0x3B
    _rejects(data[:-1], "missing GIF trailer")


def test_bytes_after_the_trailer_are_rejected() -> None:
    """Appended payloads hide behind a valid-looking image."""
    _rejects(_gif_bytes() + b"MZ\x90\x00", "4 trailing bytes after GIF trailer")


def test_a_trailer_before_any_frame_is_rejected() -> None:
    data = _gif_bytes()
    first_image = _image_descriptor_offsets(data)[0]
    # Cut everything from the first image descriptor and close the file.
    _rejects(data[:first_image] + b"\x3b", "GIF contains no image frames")


def test_an_unknown_block_marker_is_rejected() -> None:
    data = bytearray(_gif_bytes())
    data[_image_descriptor_offsets(data)[0]] = 0x7F
    _rejects(bytes(data), "invalid GIF block marker 0x7f")


def test_a_truncated_image_descriptor_is_rejected() -> None:
    data = _gif_bytes()
    first_image = _image_descriptor_offsets(data)[0]
    _rejects(data[: first_image + 5], "truncated image descriptor")


def test_an_empty_frame_rectangle_is_rejected() -> None:
    data = bytearray(_gif_bytes())
    first_image = _image_descriptor_offsets(data)[0]
    struct.pack_into("<HHHH", data, first_image + 1, 0, 0, 0, 6)
    _rejects(bytes(data), "frame has an empty image rectangle")


def test_a_frame_that_overflows_the_canvas_is_rejected() -> None:
    """A frame drawn outside the logical screen is a classic decoder crasher."""
    data = bytearray(_gif_bytes(width=8, height=6))
    first_image = _image_descriptor_offsets(data)[0]
    struct.pack_into("<HHHH", data, first_image + 1, 4, 0, 8, 6)
    _rejects(bytes(data), "frame rectangle exceeds logical canvas")


def test_a_truncated_local_color_table_is_rejected() -> None:
    data = bytearray(_gif_bytes())
    first_image = _image_descriptor_offsets(data)[0]
    # Claim a local colour table on the first frame, then end the file inside it.
    data[first_image + 9] |= 0x80
    _rejects(bytes(data[: first_image + 12]), "truncated local color table")


def test_a_missing_lzw_code_size_is_rejected() -> None:
    data = _gif_bytes()
    first_image = _image_descriptor_offsets(data)[0]
    _rejects(data[: first_image + 10], "missing LZW code size")


@pytest.mark.parametrize("code_size", [0, 1, 9, 12])
def test_an_out_of_range_lzw_code_size_is_rejected(code_size: int) -> None:
    data = bytearray(_gif_bytes())
    first_image = _image_descriptor_offsets(data)[0]
    data[first_image + 10] = code_size
    _rejects(bytes(data), f"invalid LZW minimum code size {code_size}")


def test_a_truncated_extension_label_is_rejected() -> None:
    # Extension marker as the very last byte, with nothing following it.
    data = _gif_bytes()
    first_image = _image_descriptor_offsets(data)[0]
    _rejects(data[:first_image] + b"\x21", "truncated extension label")


def test_a_data_sub_block_running_past_the_file_is_rejected() -> None:
    data = _gif_bytes()
    first_image = _image_descriptor_offsets(data)[0]
    # Descriptor + LZW size, then a sub-block claiming 200 bytes with 3 present.
    body = data[first_image : first_image + 11] + b"\xc8" + b"\x00\x00\x00"
    _rejects(data[:first_image] + body, "data sub-block exceeds file boundary")


def test_a_sub_block_sequence_that_never_terminates_is_rejected() -> None:
    data = _gif_bytes()
    first_image = _image_descriptor_offsets(data)[0]
    # A full sub-block with no terminating zero-length block and no more data.
    body = data[first_image : first_image + 11] + b"\x02\xaa\xbb"
    _rejects(data[:first_image] + body, "truncated data-sub-block sequence")


# ---------------------------------------------------------------------------
# Allowlist against a throwaway repository
# ---------------------------------------------------------------------------


def _fake_repo(tmp_path: Path, tracked: dict[str, bytes]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for relative, payload in tracked.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    for argv in (
        [GIT, "init", "-q", "."],
        [GIT, "config", "user.email", "t@example.com"],
        [GIT, "config", "user.name", "t"],
        [GIT, "add", "-A"],
    ):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)  # nosec B603
    return root


def _point_at(monkeypatch: pytest.MonkeyPatch, root: Path, policies: dict) -> None:
    monkeypatch.setattr(validator, "PROJECT_ROOT", root)
    monkeypatch.setattr(validator, "GIF_POLICIES", policies)


def _policy(**overrides: int) -> object:
    values = {"max_bytes": 1 * validator.MIB, "max_width": 64, "max_height": 64, "max_frames": 10}
    values.update(overrides)
    return validator.GifPolicy(**values)


def test_tracked_gifs_finds_only_git_tracked_gif_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case-insensitive on the suffix; untracked files are invisible."""
    root = _fake_repo(
        tmp_path, {"demo/a.gif": _gif_bytes(), "demo/B.GIF": _gif_bytes(), "docs/x.md": b"#"}
    )
    (root / "demo" / "untracked.gif").write_bytes(_gif_bytes())
    monkeypatch.setattr(validator, "PROJECT_ROOT", root)

    assert validator._tracked_gifs() == {Path("demo/a.gif"), Path("demo/B.GIF")}


def test_allowlist_passes_when_tracked_and_policy_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_repo(tmp_path, {"demo/a.gif": _gif_bytes()})
    _point_at(monkeypatch, root, {Path("demo/a.gif"): _policy()})

    validator._validate_allowlist()


def test_allowlist_reports_both_a_missing_and_an_unlisted_gif(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new GIF nobody reviewed and a deleted one both need a human."""
    root = _fake_repo(tmp_path, {"demo/new.gif": _gif_bytes()})
    _point_at(monkeypatch, root, {Path("demo/reviewed.gif"): _policy()})

    with pytest.raises(validator.ValidationError) as excinfo:
        validator._validate_allowlist()

    message = str(excinfo.value)
    assert "missing: demo/reviewed.gif" in message
    assert "not allowlisted: demo/new.gif" in message


# ---------------------------------------------------------------------------
# Per-file validation, with the real decoder
# ---------------------------------------------------------------------------


def _single(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    policy: object,
    *,
    name: str = "demo/a.gif",
) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    (root / "demo").mkdir(parents=True)
    (root / name).write_bytes(payload)
    monkeypatch.setattr(validator, "PROJECT_ROOT", root)
    return root, Path(name)


def test_a_conforming_gif_reports_its_measurements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _gif_bytes(width=8, height=6, frames=3)
    _, relative = _single(tmp_path, monkeypatch, payload, _policy())

    assert validator._validate_gif(relative, _policy()) == (len(payload), (8, 6), 3)


def test_a_symlink_is_refused_even_if_it_points_at_a_valid_gif(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tracked symlink could point outside the checkout."""
    root, _ = _single(tmp_path, monkeypatch, _gif_bytes(), _policy())
    (root / "demo" / "link.gif").symlink_to(root / "demo" / "a.gif")

    with pytest.raises(validator.ValidationError, match="expected a regular file"):
        validator._validate_gif(Path("demo/link.gif"), _policy())


def test_a_missing_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _single(tmp_path, monkeypatch, _gif_bytes(), _policy())

    with pytest.raises(validator.ValidationError, match="expected a regular file"):
        validator._validate_gif(Path("demo/absent.gif"), _policy())


def test_the_byte_ceiling_is_checked_before_the_file_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Size is the cheapest check, so it runs first — before any parsing."""
    payload = _gif_bytes()
    _, relative = _single(tmp_path, monkeypatch, payload, _policy())

    with pytest.raises(validator.ValidationError, match="bytes exceeds .*-byte limit"):
        validator._validate_gif(relative, _policy(max_bytes=len(payload) - 1))


def test_the_canvas_ceiling_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, relative = _single(tmp_path, monkeypatch, _gif_bytes(width=8, height=6), _policy())

    with pytest.raises(validator.ValidationError, match=r"8x6 exceeds 4x4 limit"):
        validator._validate_gif(relative, _policy(max_width=4, max_height=4))


def test_the_frame_ceiling_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, relative = _single(tmp_path, monkeypatch, _gif_bytes(frames=3), _policy())

    with pytest.raises(validator.ValidationError, match="3 frames exceeds 2-frame limit"):
        validator._validate_gif(relative, _policy(max_frames=2))


def test_a_blank_first_frame_is_rejected_for_the_autopilot_demos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Static clients show frame zero; an empty PTY frame is a broken preview."""
    payload = _gif_bytes(width=16, height=16, frames=2, fill=0)
    _, relative = _single(
        tmp_path, monkeypatch, payload, _policy(), name="demo/autopilot-codex.gif"
    )

    with pytest.raises(validator.ValidationError, match="first frame is effectively blank"):
        validator._validate_gif(relative, _policy())


def test_a_varied_first_frame_passes_the_autopilot_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _gif_bytes(width=16, height=16, frames=2)
    _, relative = _single(
        tmp_path, monkeypatch, payload, _policy(), name="demo/autopilot-claude-code.gif"
    )

    assert validator._validate_gif(relative, _policy())[2] == 2


def test_a_blank_first_frame_is_fine_for_a_non_autopilot_gif(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview rule is scoped to the two embedded product demos."""
    payload = _gif_bytes(width=16, height=16, frames=2, fill=0)
    _, relative = _single(tmp_path, monkeypatch, payload, _policy(), name="demo/deploy.gif")

    assert validator._validate_gif(relative, _policy())[2] == 2


def test_a_decoder_that_disagrees_with_the_parser_on_format_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces: if Pillow sees something other than a GIF, stop."""
    _, relative = _single(tmp_path, monkeypatch, _gif_bytes(), _policy())
    real_open = validator.Image.open

    class _NotAGif:
        format = "PNG"
        size = (8, 6)

        def __enter__(self) -> _NotAGif:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(validator.Image, "open", lambda path: _NotAGif())
    try:
        with pytest.raises(validator.ValidationError, match="decoder identified 'PNG', not GIF"):
            validator._validate_gif(relative, _policy())
    finally:
        monkeypatch.setattr(validator.Image, "open", real_open)


def test_a_decoder_canvas_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, relative = _single(tmp_path, monkeypatch, _gif_bytes(width=8, height=6), _policy())

    class _WrongSize:
        format = "GIF"
        size = (9, 6)

        def __enter__(self) -> _WrongSize:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(validator.Image, "open", lambda path: _WrongSize())

    with pytest.raises(validator.ValidationError, match="parser/decoder canvas mismatch"):
        validator._validate_gif(relative, _policy())


def test_a_decoder_frame_count_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structural parser and Pillow must agree, or one of them was fooled."""
    payload = _gif_bytes(width=8, height=6, frames=3)
    _, relative = _single(tmp_path, monkeypatch, payload, _policy())
    real_open = validator.Image.open
    opens = {"count": 0}

    class _FewerFrames:
        format = "GIF"
        size = (8, 6)
        n_frames = 2

        def verify(self) -> None:
            return None

        def seek(self, index: int) -> None:
            return None

        def load(self) -> None:
            return None

        def __enter__(self) -> _FewerFrames:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def fake_open(path):  # noqa: ANN001, ANN202
        opens["count"] += 1
        # First open: the real image, so verify() and the size check run on it.
        # Second open: a decoder that reports one frame fewer than the parser.
        return real_open(path) if opens["count"] == 1 else _FewerFrames()

    monkeypatch.setattr(validator.Image, "open", fake_open)

    with pytest.raises(
        validator.ValidationError, match="parser found 3 frames but decoder found 2"
    ):
        validator._validate_gif(relative, _policy())


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_passes_and_reports_each_gif(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fake_repo(
        tmp_path,
        {"demo/a.gif": _gif_bytes(width=8, height=6, frames=2), "demo/b.gif": _gif_bytes(frames=1)},
    )
    _point_at(monkeypatch, root, {Path("demo/a.gif"): _policy(), Path("demo/b.gif"): _policy()})

    assert validator.main() == 0

    out = capsys.readouterr().out
    assert "PASS demo/a.gif" in out and "8x6, 2 frames" in out
    assert "PASS demo/b.gif" in out


def test_main_turns_a_validation_error_into_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fake_repo(tmp_path, {"demo/a.gif": _gif_bytes() + b"payload"})
    _point_at(monkeypatch, root, {Path("demo/a.gif"): _policy()})

    assert validator.main() == 1
    err = capsys.readouterr().err
    assert "GIF validation failed" in err
    assert "trailing bytes after GIF trailer" in err


def test_main_turns_a_git_failure_into_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A checkout that is not a repository cannot produce a trustworthy allowlist."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    _point_at(monkeypatch, not_a_repo, {})

    assert validator.main() == 1
    assert "GIF validation failed" in capsys.readouterr().err


def test_main_turns_a_pillow_warning_into_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The decompression-bomb warning is promoted to an error inside the check."""
    root = _fake_repo(tmp_path, {"demo/a.gif": _gif_bytes()})
    _point_at(monkeypatch, root, {Path("demo/a.gif"): _policy()})

    def bomb(relative_path, policy):  # noqa: ANN001, ANN202
        raise validator.Image.DecompressionBombWarning("Image size exceeds limit")

    monkeypatch.setattr(validator, "_validate_gif", bomb)

    assert validator.main() == 1
    assert "exceeds limit" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The committed policy table
# ---------------------------------------------------------------------------


def test_the_committed_allowlist_matches_the_tracked_gifs() -> None:
    """The real inventory must agree with the real policy table.

    This is the one assertion that reads the repository, and it reads only
    ``git ls-files`` — not the GIF bytes — so it stays fast and offline.
    """
    validator._validate_allowlist()


def test_max_canvas_pixels_is_the_largest_policy_canvas() -> None:
    """Pillow's bomb threshold is derived from the policies, not hand-set."""
    assert (
        max(p.max_width * p.max_height for p in validator.GIF_POLICIES.values())
        == validator.MAX_CANVAS_PIXELS
    )


# ---------------------------------------------------------------------------
# Remaining branches
# ---------------------------------------------------------------------------


def test_a_gif_without_a_global_color_table_parses() -> None:
    """The global table is optional; frames may carry local tables instead."""
    data = bytearray(_gif_bytes())
    assert data[10] & 0x80
    # Drop the global table: clear the flag and splice the table bytes out.
    table_size = 3 * (1 << ((data[10] & 0x07) + 1))
    data[10] &= 0x7F
    del data[13 : 13 + table_size]

    assert _structure(bytes(data)) == (8, 6, 2)


def test_allowlist_reports_only_a_missing_gif(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_repo(tmp_path, {"docs/x.md": b"#"})
    _point_at(monkeypatch, root, {Path("demo/reviewed.gif"): _policy()})

    with pytest.raises(validator.ValidationError) as excinfo:
        validator._validate_allowlist()

    assert str(excinfo.value) == "tracked GIF allowlist mismatch (missing: demo/reviewed.gif)"


def test_allowlist_reports_only_an_unlisted_gif(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_repo(tmp_path, {"demo/new.gif": _gif_bytes()})
    _point_at(monkeypatch, root, {})

    with pytest.raises(validator.ValidationError) as excinfo:
        validator._validate_allowlist()

    assert str(excinfo.value) == "tracked GIF allowlist mismatch (not allowlisted: demo/new.gif)"
