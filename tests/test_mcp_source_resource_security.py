"""Security and error-path tests for the ``source://`` MCP resources."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gco_mcp"))

from resources import source  # noqa: E402


def test_source_file_rejects_sibling_prefix_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling whose name starts with the root name is not confined to it."""
    root = tmp_path / "project"
    root.mkdir()
    sibling = tmp_path / "project-secrets"
    sibling.mkdir()
    secret = sibling / "secret.py"
    secret.write_text("TOP_SECRET = True\n", encoding="utf-8")
    monkeypatch.setattr(source, "PROJECT_ROOT", root)

    result = source.source_file_resource(str(secret))

    assert result == "Access denied: path is outside the project."
    assert "TOP_SECRET" not in result


@pytest.mark.parametrize("escape_kind", ["traversal", "symlink"])
def test_source_file_rejects_other_resolved_escapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    escape_kind: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    monkeypatch.setattr(source, "PROJECT_ROOT", root)
    if escape_kind == "symlink":
        (root / "escape.py").symlink_to(outside)
        requested = "escape.py"
    else:
        requested = "../outside.py"

    assert source.source_file_resource(requested) == ("Access denied: path is outside the project.")


def test_source_file_serves_only_allowed_confined_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    allowed = root / "module.py"
    allowed.write_text("VALUE = 1\n", encoding="utf-8")
    skipped = root / ".git"
    skipped.mkdir()
    (skipped / "config.py").write_text("private\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"binary")
    monkeypatch.setattr(source, "PROJECT_ROOT", root)

    assert source.source_file_resource("module.py") == "VALUE = 1\n"
    assert source.source_file_resource("missing.py") == "File 'missing.py' not found."
    assert source.source_file_resource(".git/config.py").startswith("Access denied")
    assert source.source_file_resource("binary.bin").startswith("File type '.bin' not served")


def test_source_index_and_config_resources_cover_optional_layouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    missing_config = root / "00-missing.toml"
    config = root / "pyproject.toml"
    config.write_text("[project]\n", encoding="utf-8")
    populated = root / "pkg"
    populated.mkdir()
    (populated / "main.py").write_text("pass\n", encoding="utf-8")
    ignored = populated / "node_modules"
    ignored.mkdir()
    (ignored / "hidden.py").write_text("pass\n", encoding="utf-8")
    (populated / "blob.bin").write_bytes(b"x")
    empty = root / "empty"
    empty.mkdir()
    monkeypatch.setattr(source, "PROJECT_ROOT", root)
    monkeypatch.setattr(
        source,
        "_CONFIG_FILES",
        {"00-missing.toml": missing_config, "pyproject.toml": config},
    )
    monkeypatch.setattr(
        source,
        "_SOURCE_DIRS",
        {"missing": root / "missing", "empty": empty, "pkg": populated},
    )

    index = source.source_index()

    assert "source://gco/config/00-missing.toml" not in index
    assert "source://gco/config/pyproject.toml" in index
    assert "## pkg/ (1 files)" in index
    assert "source://gco/file/pkg/main.py" in index
    assert "missing/" not in index
    assert "empty/" not in index
    assert source.config_file_resource("pyproject.toml") == "[project]\n"
    assert source.config_file_resource("unknown") == (
        "Not available. Allowed: 00-missing.toml, pyproject.toml"
    )

    config.unlink()
    assert source.config_file_resource("pyproject.toml") == "File 'pyproject.toml' not found."
