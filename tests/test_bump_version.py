"""
Tests for scripts/bump_version.py.

Exercises the SemVer version bumper that reads the source of truth from
the top-level ``VERSION`` file and keeps ``gco/_version.py``,
``cli/__init__.py``, and exact MCP launch refs in sync: reading the current version, patch/minor/major
bumps with correct field resets, dry-run mode that prints but doesn't
write, invalid-input error paths, and the main() CLI dispatcher including
case-insensitive bump arguments. Uses a tmp_path-backed fixture that
patches the module's ``PROJECT_ROOT``, ``VERSION_FILE``, ``VERSION_PY``,
and ``CLI_INIT_FILE`` constants so the real repo files are never touched.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import bump_version


@pytest.fixture
def version_files(tmp_path):
    """Create temporary VERSION, gco/_version.py, and cli/__init__.py."""
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.2.3\n")

    version_py = tmp_path / "gco" / "_version.py"
    version_py.parent.mkdir(parents=True)
    version_py.write_text('"""Version."""\n\n__version__ = "1.2.3"\n')

    cli_init = tmp_path / "cli" / "__init__.py"
    cli_init.parent.mkdir(parents=True)
    cli_init.write_text(
        '"""CLI."""\n\ntry:\n    from gco._version import __version__\n'
        'except ImportError:\n    __version__ = "1.2.3"\n'
    )

    mcp_readme = tmp_path / "gco_mcp" / "README.md"
    mcp_readme.parent.mkdir(parents=True)
    mcp_readme.write_text(
        "GCO_REF=v1.2.3\n"
        "git+https://example.invalid/global-capacity-orchestrator-on-aws.git@v1.2.3\n"
        "historical first release: v3.2.0\n",
        encoding="utf-8",
    )

    with (
        patch.object(bump_version, "PROJECT_ROOT", tmp_path),
        patch.object(bump_version, "VERSION_FILE", version_file),
        patch.object(bump_version, "VERSION_PY", version_py),
        patch.object(bump_version, "CLI_INIT_FILE", cli_init),
        patch.object(bump_version, "MCP_README_FILE", mcp_readme),
    ):
        yield tmp_path, version_file, version_py, cli_init


class TestGetVersion:
    def test_reads_version(self, version_files):
        _, _, _, _ = version_files
        assert bump_version.get_version() == "1.2.3"

    def test_strips_trailing_whitespace(self, version_files):
        _, version_file, _, _ = version_files
        version_file.write_text("  2.3.4  \n\n")
        assert bump_version.get_version() == "2.3.4"

    def test_missing_version_file_raises(self, version_files):
        _, version_file, _, _ = version_files
        version_file.unlink()
        with pytest.raises(FileNotFoundError, match="VERSION file not found"):
            bump_version.get_version()

    def test_invalid_version_raises(self, version_files):
        _, version_file, _, _ = version_files
        version_file.write_text("not-a-version\n")
        with pytest.raises(ValueError, match="Invalid version"):
            bump_version.get_version()

    def test_two_part_version_rejected(self, version_files):
        _, version_file, _, _ = version_files
        version_file.write_text("1.2\n")
        with pytest.raises(ValueError, match="Invalid version"):
            bump_version.get_version()


class TestBumpVersion:
    def test_patch(self):
        with patch.object(bump_version, "get_version", return_value="1.2.3"):
            assert bump_version.bump_version("patch") == "1.2.4"

    def test_minor(self):
        with patch.object(bump_version, "get_version", return_value="1.2.3"):
            assert bump_version.bump_version("minor") == "1.3.0"

    def test_major(self):
        with patch.object(bump_version, "get_version", return_value="1.2.3"):
            assert bump_version.bump_version("major") == "2.0.0"

    def test_patch_resets_nothing(self):
        with patch.object(bump_version, "get_version", return_value="0.0.0"):
            assert bump_version.bump_version("patch") == "0.0.1"

    def test_minor_resets_patch(self):
        with patch.object(bump_version, "get_version", return_value="1.5.9"):
            assert bump_version.bump_version("minor") == "1.6.0"

    def test_major_resets_minor_and_patch(self):
        with patch.object(bump_version, "get_version", return_value="3.7.11"):
            assert bump_version.bump_version("major") == "4.0.0"

    def test_invalid_bump_type(self):
        with (
            patch.object(bump_version, "get_version", return_value="1.0.0"),
            pytest.raises(ValueError, match="Invalid bump type"),
        ):
            bump_version.bump_version("hotfix")


class TestUpdateVersionFile:
    def test_updates_version(self, version_files):
        _, version_file, _, _ = version_files
        bump_version.update_version_file("2.0.0")
        assert version_file.read_text() == "2.0.0\n"

    def test_dry_run_no_change(self, version_files):
        _, version_file, _, _ = version_files
        original = version_file.read_text()
        bump_version.update_version_file("2.0.0", dry_run=True)
        assert version_file.read_text() == original


class TestUpdateVersionPy:
    def test_updates_double_quoted(self, version_files):
        _, _, version_py, _ = version_files
        bump_version.update_version_py("2.0.0")
        assert '__version__ = "2.0.0"' in version_py.read_text()

    def test_updates_single_quoted(self, version_files):
        _, _, version_py, _ = version_files
        version_py.write_text("__version__ = '9.8.7'\n")
        bump_version.update_version_py("2.0.0")
        assert '__version__ = "2.0.0"' in version_py.read_text()

    def test_dry_run_no_change(self, version_files):
        _, _, version_py, _ = version_files
        original = version_py.read_text()
        bump_version.update_version_py("2.0.0", dry_run=True)
        assert version_py.read_text() == original


class TestUpdateCliInit:
    def test_updates_fallback_version(self, version_files):
        _, _, _, cli_init = version_files
        bump_version.update_cli_init("2.0.0")
        assert '__version__ = "2.0.0"' in cli_init.read_text()

    def test_dry_run_no_change(self, version_files):
        _, _, _, cli_init = version_files
        original = cli_init.read_text()
        bump_version.update_cli_init("2.0.0", dry_run=True)
        assert cli_init.read_text() == original


class TestUpdateMcpReadme:
    def test_updates_exact_refs_but_preserves_historical_version(self, version_files):
        root, _, _, _ = version_files
        readme = root / "gco_mcp" / "README.md"

        bump_version.update_mcp_readme_release_refs("2.0.0")

        content = readme.read_text(encoding="utf-8")
        assert "GCO_REF=v2.0.0" in content
        assert ".git@v2.0.0" in content
        assert "historical first release: v3.2.0" in content

    def test_dry_run_no_change(self, version_files):
        root, _, _, _ = version_files
        readme = root / "gco_mcp" / "README.md"
        original = readme.read_text(encoding="utf-8")

        bump_version.update_mcp_readme_release_refs("2.0.0", dry_run=True)

        assert readme.read_text(encoding="utf-8") == original


class TestSetVersion:
    def test_updates_all_version_surfaces(self, version_files):
        root, version_file, version_py, cli_init = version_files
        bump_version.set_version("5.0.0")
        assert version_file.read_text() == "5.0.0\n"
        assert '__version__ = "5.0.0"' in version_py.read_text()
        assert '__version__ = "5.0.0"' in cli_init.read_text()
        mcp_readme = (root / "gco_mcp" / "README.md").read_text(encoding="utf-8")
        assert "GCO_REF=v5.0.0" in mcp_readme
        assert ".git@v5.0.0" in mcp_readme

    def test_dry_run_updates_nothing(self, version_files):
        root, version_file, version_py, cli_init = version_files
        mcp_readme = root / "gco_mcp" / "README.md"
        orig_version = version_file.read_text()
        orig_py = version_py.read_text()
        orig_cli = cli_init.read_text()
        orig_mcp = mcp_readme.read_text(encoding="utf-8")
        bump_version.set_version("5.0.0", dry_run=True)
        assert version_file.read_text() == orig_version
        assert version_py.read_text() == orig_py
        assert cli_init.read_text() == orig_cli
        assert mcp_readme.read_text(encoding="utf-8") == orig_mcp


class TestMain:
    def test_no_args_shows_current_version(self, version_files, capsys):
        with patch.object(sys, "argv", ["bump_version.py"]):
            bump_version.main()
        out = capsys.readouterr().out
        assert "Current version: 1.2.3" in out
        assert "VERSION" in out
        assert "gco/_version.py" in out
        assert "cli/__init__.py" in out

    def test_patch_bump(self, version_files, capsys):
        _, version_file, version_py, cli_init = version_files
        with patch.object(sys, "argv", ["bump_version.py", "patch"]):
            bump_version.main()
        out = capsys.readouterr().out
        assert "1.2.3 -> 1.2.4" in out
        assert version_file.read_text() == "1.2.4\n"
        assert '__version__ = "1.2.4"' in version_py.read_text()
        assert '__version__ = "1.2.4"' in cli_init.read_text()
        assert "git push -u origin HEAD" in out
        assert "gh pr create --base main --title 'Release v1.2.4'" in out
        assert "release-publish.yml creates the tag" in out
        assert "git push origin main" not in out
        assert "git tag" not in out

    def test_minor_bump(self, version_files, capsys):
        _, version_file, version_py, _ = version_files
        with patch.object(sys, "argv", ["bump_version.py", "minor"]):
            bump_version.main()
        assert version_file.read_text() == "1.3.0\n"
        assert '__version__ = "1.3.0"' in version_py.read_text()

    def test_major_bump(self, version_files, capsys):
        _, version_file, version_py, _ = version_files
        with patch.object(sys, "argv", ["bump_version.py", "major"]):
            bump_version.main()
        assert version_file.read_text() == "2.0.0\n"
        assert '__version__ = "2.0.0"' in version_py.read_text()

    def test_dry_run_flag(self, version_files, capsys):
        _, version_file, version_py, _ = version_files
        orig_version = version_file.read_text()
        orig_py = version_py.read_text()
        with patch.object(sys, "argv", ["bump_version.py", "patch", "--dry-run"]):
            bump_version.main()
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        assert version_file.read_text() == orig_version
        assert version_py.read_text() == orig_py

    def test_dry_run_short_flag(self, version_files, capsys):
        _, version_file, _, _ = version_files
        original = version_file.read_text()
        with patch.object(sys, "argv", ["bump_version.py", "minor", "-n"]):
            bump_version.main()
        assert version_file.read_text() == original

    def test_invalid_bump_type_exits(self, version_files):
        with (
            patch.object(sys, "argv", ["bump_version.py", "hotfix"]),
            pytest.raises(SystemExit),
        ):
            bump_version.main()

    def test_case_insensitive(self, version_files, capsys):
        _, version_file, version_py, _ = version_files
        with patch.object(sys, "argv", ["bump_version.py", "PATCH"]):
            bump_version.main()
        assert version_file.read_text() == "1.2.4\n"
        assert '__version__ = "1.2.4"' in version_py.read_text()
