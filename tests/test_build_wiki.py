"""Tests for ``scripts/build_wiki.py``, the wiki staging step in front of Zensical.

The script owns the one thing Zensical cannot do for this repository: assemble
the site's source tree (``build/wiki``) from ``wiki/`` (minus its README), the
tracked ``images/`` and the generated API spec sheets, then hand it to
``zensical build --clean --strict`` or ``zensical serve``. The staging is a sync,
so these tests pin what is copied, what is left out, what is removed when a
source disappears, that an unchanged tree is left alone, and that the preview
loop re-stages when a source changes and shuts the server down cleanly.

Everything runs against a synthetic repository under ``tmp_path``; Zensical
itself is replaced by fakes at the ``subprocess`` seam. The last test reads the
real repository: the stage mapping must cover exactly the pages ``tests/test_wiki.py``
treats as published, every tracked image but the README, and every sheet the
catalogue ships.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts import build_wiki

REPO_ROOT = Path(__file__).resolve().parent.parent


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "wiki").mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Home\n", encoding="utf-8")
    (root / "wiki" / "page.md").write_text("# Page\n", encoding="utf-8")
    (root / "wiki" / "README.md").write_text("# Not a page\n", encoding="utf-8")
    (root / "images").mkdir()
    (root / "images" / "shot.png").write_bytes(b"\x89PNG")
    (root / "images" / "README.md").write_text("# Not an asset\n", encoding="utf-8")
    (root / "images" / "nested").mkdir()  # directories under images/ are not assets
    specs = root / "diagrams" / "api_specs"
    specs.mkdir(parents=True)
    (specs / "README.md").write_text("# Catalogue\n", encoding="utf-8")
    (specs / "svc.md").write_text("# svc\n", encoding="utf-8")
    (specs / "api-topology.svg").write_text("<svg/>\n", encoding="utf-8")
    (specs / "generate.py").write_text("print()\n", encoding="utf-8")
    (specs / "__init__.py").write_text("", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# The mapping and the sync
# ---------------------------------------------------------------------------


def test_mapping_includes_pages_assets_and_sheets_and_nothing_else(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    mapping = build_wiki.staged_files(root)
    assert set(mapping) == {
        Path("index.md"),
        Path("page.md"),
        Path("assets/images/shot.png"),
        Path("api/README.md"),
        Path("api/svc.md"),
        Path("api/api-topology.svg"),
    }
    assert mapping[Path("api/svc.md")] == root / "diagrams" / "api_specs" / "svc.md"


def test_first_stage_copies_everything_preserving_content(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    copied, removed = build_wiki.stage(root)
    assert len(copied) == 6 and removed == []
    stage = root / build_wiki.STAGE_DIR
    assert (stage / "index.md").read_text(encoding="utf-8") == "# Home\n"
    assert (stage / "assets" / "images" / "shot.png").read_bytes() == b"\x89PNG"
    assert (stage / "api" / "api-topology.svg").is_file()
    assert not (stage / "README.md").exists()
    assert not (stage / "assets" / "images" / "README.md").exists()
    assert not (stage / "api" / "generate.py").exists()


def test_second_stage_is_a_no_op_when_nothing_changed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    build_wiki.stage(root)
    assert build_wiki.stage(root) == ([], [])


def test_changed_and_new_sources_are_copied_and_orphans_removed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    build_wiki.stage(root)
    stage = root / build_wiki.STAGE_DIR

    (root / "wiki" / "page.md").write_text("# Page, edited\n", encoding="utf-8")
    (root / "wiki" / "new.md").write_text("# New\n", encoding="utf-8")
    (root / "images" / "shot.png").unlink()
    (stage / "api" / "stale").mkdir()
    (stage / "api" / "stale" / "left-behind.md").write_text("x\n", encoding="utf-8")

    copied, removed = build_wiki.stage(root)
    assert copied == [Path("new.md"), Path("page.md")]
    assert removed == [Path("api/stale/left-behind.md"), Path("assets/images/shot.png")]
    assert (stage / "page.md").read_text(encoding="utf-8") == "# Page, edited\n"
    assert not (stage / "api" / "stale").exists(), "emptied directories are pruned"
    assert not (stage / "assets").exists(), "assets/images had only the one file"
    assert stage.is_dir(), "the tree itself is never removed: the preview server watches it"


def test_a_source_touched_without_content_change_is_recopied(tmp_path: Path) -> None:
    """mtime is part of the comparison, so a save with identical bytes still syncs."""
    root = _repo(tmp_path)
    build_wiki.stage(root)
    page = root / "wiki" / "index.md"
    os.utime(page, ns=(page.stat().st_atime_ns, page.stat().st_mtime_ns + 5_000_000_000))
    copied, _removed = build_wiki.stage(root)
    assert copied == [Path("index.md")]


def test_snapshot_tracks_every_source_size_and_mtime(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = build_wiki.snapshot(root)
    assert set(before) == set(build_wiki.staged_files(root))
    (root / "wiki" / "index.md").write_text("# Home, longer\n", encoding="utf-8")
    after = build_wiki.snapshot(root)
    assert after != before and after[Path("index.md")][0] > before[Path("index.md")][0]


# ---------------------------------------------------------------------------
# Finding zensical
# ---------------------------------------------------------------------------


def test_zensical_next_to_the_interpreter_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_python = tmp_path / "venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.touch()
    (fake_python.parent / "zensical").touch()
    monkeypatch.setattr(build_wiki.sys, "executable", str(fake_python))
    monkeypatch.setattr(build_wiki.shutil, "which", lambda name: "/elsewhere/zensical")
    assert build_wiki.zensical_executable() == str(fake_python.parent / "zensical")


def test_zensical_falls_back_to_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_wiki.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(build_wiki.shutil, "which", lambda name: "/usr/local/bin/zensical")
    assert build_wiki.zensical_executable() == "/usr/local/bin/zensical"


def test_missing_zensical_explains_how_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_wiki.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(build_wiki.shutil, "which", lambda name: None)
    with pytest.raises(build_wiki.ToolchainError, match=r'pip install -e "\.\[docs\]"'):
        build_wiki.zensical_executable()


# ---------------------------------------------------------------------------
# build and serve at the subprocess seam
# ---------------------------------------------------------------------------


class _Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _fake_run(calls: list[tuple[list[str], Path]], returncode: int = 0):  # type: ignore[no-untyped-def]
    def run(argv: list[str], cwd: Path, check: bool) -> _Completed:
        assert check is False, "a failed build is reported through the exit status, not raised"
        calls.append((argv, cwd))
        return _Completed(returncode)

    return run


def test_build_stages_then_runs_the_strict_clean_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(build_wiki, "zensical_executable", lambda: "/bin/zensical")
    monkeypatch.setattr(build_wiki.subprocess, "run", _fake_run(calls, returncode=3))

    assert build_wiki.build(root) == 3

    assert calls == [(["/bin/zensical", "build", "--clean", "--strict"], root)]
    assert (root / build_wiki.STAGE_DIR / "index.md").is_file()
    out = capsys.readouterr().out
    assert "==> Staging the wiki sources: 6 copied, 0 removed -> build/wiki/" in out
    assert "==> Strict build (the exact check CI runs)" in out


class _FakeServer:
    """Stands in for the ``zensical serve`` process: alive for ``polls`` checks, then done."""

    def __init__(self, argv: list[str], cwd: Path, *, polls: int, returncode: int = 0) -> None:
        self.argv, self.cwd = argv, cwd
        self._polls, self.returncode = polls, returncode
        self.terminated = self.waited = False

    def poll(self) -> int | None:
        if self._polls > 0:
            self._polls -= 1
            return None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def wait(self) -> int:
        self.waited = True
        return self.returncode


def test_serve_stops_at_a_failed_strict_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(build_wiki, "build", lambda repo_root: 1)
    monkeypatch.setattr(
        build_wiki.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("no server after a failed build"),
    )
    assert build_wiki.serve(root, 8000) == 1


def test_serve_restages_when_a_source_changes_and_returns_the_server_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    servers: list[_FakeServer] = []

    def popen(argv: list[str], cwd: Path) -> _FakeServer:
        server = _FakeServer(argv, cwd, polls=3, returncode=7)
        servers.append(server)
        return server

    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:  # between the second and third poll, a page is edited
            (root / "wiki" / "page.md").write_text("# Page, edited live\n", encoding="utf-8")

    monkeypatch.setattr(build_wiki, "build", lambda repo_root: 0)
    monkeypatch.setattr(build_wiki, "zensical_executable", lambda: "/bin/zensical")
    monkeypatch.setattr(build_wiki.subprocess, "Popen", popen)
    monkeypatch.setattr(build_wiki.time, "sleep", sleep)
    build_wiki.stage(root)  # what build() would have left behind

    assert build_wiki.serve(root, 9000) == 7

    assert servers[0].argv == ["/bin/zensical", "serve", "--dev-addr", "127.0.0.1:9000"]
    assert servers[0].cwd == root
    assert sleeps == [build_wiki.POLL_SECONDS] * 3
    assert (root / build_wiki.STAGE_DIR / "page.md").read_text(
        encoding="utf-8"
    ) == "# Page, edited live\n"
    out = capsys.readouterr().out
    assert "==> Serving with live reload at http://127.0.0.1:9000/ (Ctrl-C to stop)" in out
    assert out.count("==> Sources changed; re-staging: 1 copied, 0 removed") == 1


def test_serve_terminates_the_server_on_ctrl_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    servers: list[_FakeServer] = []

    def popen(argv: list[str], cwd: Path) -> _FakeServer:
        server = _FakeServer(argv, cwd, polls=10)
        servers.append(server)
        return server

    def sleep(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(build_wiki, "build", lambda repo_root: 0)
    monkeypatch.setattr(build_wiki, "zensical_executable", lambda: "/bin/zensical")
    monkeypatch.setattr(build_wiki.subprocess, "Popen", popen)
    monkeypatch.setattr(build_wiki.time, "sleep", sleep)

    assert build_wiki.serve(root, 8000) == 0
    assert servers[0].terminated and servers[0].waited


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def test_main_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(build_wiki, "REPO_ROOT", root)
    assert build_wiki.main(["stage"]) == 0
    assert (root / build_wiki.STAGE_DIR / "api" / "svc.md").is_file()
    assert "6 copied, 0 removed" in capsys.readouterr().out


def test_main_build_and_serve_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path)
    seen: list[tuple[str, object]] = []
    monkeypatch.setattr(build_wiki, "REPO_ROOT", root)
    monkeypatch.setattr(
        build_wiki, "build", lambda repo_root: seen.append(("build", repo_root)) or 4
    )
    monkeypatch.setattr(
        build_wiki, "serve", lambda repo_root, port: seen.append(("serve", (repo_root, port))) or 5
    )
    assert build_wiki.main(["build"]) == 4
    assert build_wiki.main(["serve"]) == 5
    assert build_wiki.main(["serve", "--port", "9100"]) == 5
    assert seen == [("build", root), ("serve", (root, 8000)), ("serve", (root, 9100))]


def test_main_reports_a_missing_toolchain_as_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(build_wiki, "REPO_ROOT", root)
    monkeypatch.setattr(build_wiki.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(build_wiki.shutil, "which", lambda name: None)
    assert build_wiki.main(["build"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: zensical is not installed for this interpreter.")


def test_main_requires_a_command() -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_wiki.main([])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# The real repository
# ---------------------------------------------------------------------------


def test_the_real_mapping_covers_the_published_pages_images_and_sheets() -> None:
    mapping = build_wiki.staged_files(REPO_ROOT)
    pages = {p.name for p in (REPO_ROOT / "wiki").glob("*.md")} - {"README.md"}
    assert {str(k) for k in mapping if k.parent == Path()} == pages
    images = {p.name for p in (REPO_ROOT / "images").iterdir() if p.is_file()} - {"README.md"}
    assert {k.name for k in mapping if k.parent == build_wiki.ASSETS_PREFIX} == images
    sheets = {p.name for p in (REPO_ROOT / "diagrams" / "api_specs").glob("*.md")} | {
        "api-topology.svg"
    }
    assert {k.name for k in mapping if k.parent == build_wiki.API_PREFIX} == sheets
    assert "generate.py" not in {k.name for k in mapping}
    assert (
        subprocess.run(  # the staging never touches tracked files
            ["git", "check-ignore", "-q", str(build_wiki.STAGE_DIR)], cwd=REPO_ROOT, check=False
        ).returncode
        == 0
    ), "build/wiki must be gitignored"
