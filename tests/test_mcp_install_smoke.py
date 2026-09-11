"""Tests for ``scripts/mcp_install_smoke.py``.

The smoke test is a module-level script meant to run under an *installed*
environment's interpreter (``uv tool install`` / ``uvx`` / a venv), so these
tests execute the file with ``runpy`` against stand-in ``mcp`` and ``gco_mcp``
modules registered in ``sys.modules`` and a fake ``sys.executable``; nothing is
installed and no real package is imported. They pin the four checks the script
performs (``gco_mcp`` imported from site-packages, ``run_mcp.main`` callable,
the PyPI ``mcp`` SDK not shadowed, and the resolved ``gco`` executable living
next to the interpreter), the exit code and report shape when any check fails,
the success report, and the namespace-package fallback used to locate
``gco_mcp`` when it has no ``__file__``.
"""

from __future__ import annotations

import runpy
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mcp_install_smoke.py"


@dataclass
class _Environment:
    """One simulated installation: interpreter, site-packages, and fake modules."""

    bindir: Path
    site_packages: Path
    gco_mcp_file: str | None
    mcp_file: str
    gco_executable: str


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    package_from_site: bool = True,
    main_callable: bool = True,
    mcp_from_site: bool = True,
    bundled_gco: bool = True,
    namespace_package: bool = False,
    namespace_path: bool = True,
) -> _Environment:
    venv = tmp_path / "venv"
    bindir = venv / "bin"
    site_packages = venv / "lib" / "python3.14" / "site-packages"
    checkout = tmp_path / "checkout"
    monkeypatch.setattr(sys, "executable", str(bindir / "python"))

    package_dir = (site_packages if package_from_site else checkout) / "gco_mcp"
    gco_mcp = types.ModuleType("gco_mcp")
    gco_mcp_file: str | None = str(package_dir / "__init__.py")
    if namespace_package:
        gco_mcp_file = None
    gco_mcp.__file__ = gco_mcp_file
    gco_mcp.__path__ = [str(package_dir)] if namespace_path else []

    gco_executable = str(bindir / "gco") if bundled_gco else "/usr/local/bin/gco"
    cli_runner = types.ModuleType("gco_mcp.cli_runner")
    cli_runner._gco_executable = lambda: gco_executable
    run_mcp = types.ModuleType("gco_mcp.run_mcp")
    run_mcp.main = (lambda: 0) if main_callable else "not-a-function"
    gco_mcp.cli_runner = cli_runner
    gco_mcp.run_mcp = run_mcp

    mcp = types.ModuleType("mcp")
    mcp_file = (
        str(site_packages / "mcp" / "__init__.py")
        if mcp_from_site
        else str(checkout / "gco_mcp" / "mcp" / "__init__.py")
    )
    mcp.__file__ = mcp_file

    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "gco_mcp", gco_mcp)
    monkeypatch.setitem(sys.modules, "gco_mcp.cli_runner", cli_runner)
    monkeypatch.setitem(sys.modules, "gco_mcp.run_mcp", run_mcp)
    return _Environment(
        bindir=bindir,
        site_packages=site_packages,
        gco_mcp_file=gco_mcp_file,
        mcp_file=mcp_file,
        gco_executable=gco_executable,
    )


def _run() -> dict[str, Any]:
    return runpy.run_path(str(SCRIPT))


def test_healthy_installation_passes_and_reports_what_it_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _install(monkeypatch, tmp_path)

    namespace = _run()

    assert namespace["problems"] == []
    assert capsys.readouterr().out.splitlines() == [
        "MCP install smoke test OK",
        f"gco_mcp: {env.gco_mcp_file}",
        f"mcp SDK: {env.mcp_file}",
        f"bundled gco: {env.bindir / 'gco'}",
    ]


def test_every_check_failing_exits_one_and_lists_each_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _install(
        monkeypatch,
        tmp_path,
        package_from_site=False,
        main_callable=False,
        mcp_from_site=False,
        bundled_gco=False,
    )

    with pytest.raises(SystemExit) as excinfo:
        _run()

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "MCP install smoke test FAILED:",
        f"- gco_mcp not imported from site-packages: {env.gco_mcp_file}",
        "- gco_mcp.run_mcp.main is not callable",
        f"- mcp SDK shadowed by gco_mcp: {env.mcp_file}",
        "- server resolved a non-bundled gco: /usr/local/bin/gco",
    ]
    assert "OK" not in out


@pytest.mark.parametrize(
    ("overrides", "expected_prefix"),
    [
        ({"package_from_site": False}, "- gco_mcp not imported from site-packages: "),
        ({"main_callable": False}, "- gco_mcp.run_mcp.main is not callable"),
        ({"mcp_from_site": False}, "- mcp SDK shadowed by gco_mcp: "),
        ({"bundled_gco": False}, "- server resolved a non-bundled gco: /usr/local/bin/gco"),
    ],
)
def test_a_single_failing_check_is_reported_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, bool],
    expected_prefix: str,
) -> None:
    """Each check is independent: one bad signal produces exactly one problem line."""
    _install(monkeypatch, tmp_path, **overrides)

    with pytest.raises(SystemExit) as excinfo:
        _run()

    assert excinfo.value.code == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "MCP install smoke test FAILED:"
    assert len(lines) == 2
    assert lines[1].startswith(expected_prefix)


def test_namespace_package_is_located_through_its_search_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``__file__``-less ``gco_mcp`` is judged by its ``__path__`` entry instead."""
    env = _install(monkeypatch, tmp_path, namespace_package=True)

    namespace = _run()

    assert namespace["pkg"] == str(env.site_packages / "gco_mcp")
    assert capsys.readouterr().out.splitlines()[1] == f"gco_mcp: {env.site_packages / 'gco_mcp'}"


def test_namespace_package_without_a_search_path_fails_the_location_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, tmp_path, namespace_package=True, namespace_path=False)

    with pytest.raises(SystemExit) as excinfo:
        _run()

    assert excinfo.value.code == 1
    assert capsys.readouterr().out.splitlines() == [
        "MCP install smoke test FAILED:",
        "- gco_mcp not imported from site-packages: ",
    ]


def test_real_gco_mcp_package_is_not_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stand-ins fully satisfy the script's imports; no checkout module is loaded."""
    env = _install(monkeypatch, tmp_path)

    namespace = _run()

    assert namespace["cli_runner"] is sys.modules["gco_mcp.cli_runner"]
    assert namespace["run_mcp"] is sys.modules["gco_mcp.run_mcp"]
    assert namespace["gco_exe"] == env.gco_executable
    assert namespace["bindir"] == str(env.bindir)
