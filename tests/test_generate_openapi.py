"""Tests for ``scripts/generate_openapi.py``'s command-line behaviour.

``tests/test_api_docs_coverage.py`` already runs ``main(["--check"])`` against
the real applications and committed documents. This module pins the remaining
contract with stand-in applications and a temporary output directory: write
mode creates missing documents, rewrites stale ones, and leaves current ones
untouched while reporting each outcome; ``--check`` names every missing or
stale document, points at the regeneration command on stderr, exits non-zero,
and never writes; ``build_document`` strips FastAPI's own documentation routes
and returns plain JSON types; ``render`` is deterministic; and ``load_apps``
puts the repository root on ``sys.path`` when it is absent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import generate_openapi


class _FakeApp:
    """Minimal stand-in for a FastAPI application: only ``openapi()`` is used."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document

    def openapi(self) -> dict[str, Any]:
        return self.document


def _document(title: str, *paths: str) -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": "1.0.0"},
        "paths": {path: {"get": {"summary": f"GET {path}"}} for path in paths},
    }


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the generator at a temporary repository root and output directory."""
    monkeypatch.setattr(generate_openapi, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(generate_openapi, "OUTPUT_DIR", tmp_path / "docs" / "openapi")
    return tmp_path


@pytest.fixture
def apps(monkeypatch: pytest.MonkeyPatch) -> dict[str, _FakeApp]:
    """Two stand-in services, deliberately unsorted so ordering is observable."""
    fakes = {
        "svc-b": _FakeApp(_document("B", "/api/v1/b", "/docs")),
        "svc-a": _FakeApp(_document("A", "/api/v1/a", "/openapi.json")),
    }
    monkeypatch.setattr(generate_openapi, "load_apps", lambda: dict(fakes))
    return fakes


def _expected(app: _FakeApp) -> str:
    return generate_openapi.render(generate_openapi.build_document(app))


# ---------------------------------------------------------------------------
# build_document / render
# ---------------------------------------------------------------------------


def test_build_document_strips_fastapi_doc_routes_and_plain_types() -> None:
    document = _document("Svc", "/api/v1/jobs", "/docs", "/docs/oauth2-redirect", "/redoc")
    document["paths"]["/openapi.json"] = {"get": {}}
    document["servers"] = ({"url": "https://example.test"},)

    built = generate_openapi.build_document(_FakeApp(document))

    assert list(built["paths"]) == ["/api/v1/jobs"]
    assert built["servers"] == [{"url": "https://example.test"}]
    assert built is not document
    assert list(document["paths"]) != ["/api/v1/jobs"], "the app's own document is untouched"


def test_build_document_tolerates_a_document_without_paths() -> None:
    assert generate_openapi.build_document(_FakeApp({"openapi": "3.1.0"})) == {"openapi": "3.1.0"}


def test_render_is_sorted_indented_and_newline_terminated() -> None:
    rendered = generate_openapi.render({"b": 1, "a": {"d": [1, 2], "c": None}})

    assert (
        rendered
        == '{\n  "a": {\n    "c": null,\n    "d": [\n      1,\n      2\n    ]\n  },\n  "b": 1\n}\n'
    )
    assert rendered == generate_openapi.render(json.loads(rendered))


# ---------------------------------------------------------------------------
# main: write mode
# ---------------------------------------------------------------------------


def test_main_writes_missing_documents_and_reports_unchanged_ones(
    workspace: Path, apps: dict[str, _FakeApp], capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = workspace / "docs" / "openapi"
    output_dir.mkdir(parents=True)
    (output_dir / "svc-a.json").write_text(_expected(apps["svc-a"]), encoding="utf-8")

    assert generate_openapi.main([]) == 0

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "docs/openapi/svc-a.json: unchanged",
        "docs/openapi/svc-b.json: written",
    ]
    assert captured.err == ""
    written = json.loads((output_dir / "svc-b.json").read_text(encoding="utf-8"))
    assert list(written["paths"]) == ["/api/v1/b"], "FastAPI doc routes are not committed"
    assert (output_dir / "svc-b.json").read_text(encoding="utf-8") == _expected(apps["svc-b"])


def test_main_rewrites_stale_documents_and_creates_the_output_directory(
    workspace: Path, apps: dict[str, _FakeApp], capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = workspace / "docs" / "openapi"
    assert not output_dir.exists()

    assert generate_openapi.main([]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "docs/openapi/svc-a.json: written",
        "docs/openapi/svc-b.json: written",
    ]

    apps["svc-a"].document["paths"]["/api/v1/added"] = {"post": {"summary": "new"}}

    assert generate_openapi.main([]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "docs/openapi/svc-a.json: written",
        "docs/openapi/svc-b.json: unchanged",
    ]
    assert "/api/v1/added" in (output_dir / "svc-a.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# main: --check mode
# ---------------------------------------------------------------------------


def test_check_names_missing_and_stale_documents_without_writing(
    workspace: Path, apps: dict[str, _FakeApp], capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = workspace / "docs" / "openapi"
    output_dir.mkdir(parents=True)
    (output_dir / "svc-a.json").write_text("{}\n", encoding="utf-8")

    assert generate_openapi.main(["--check"]) == 1

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "docs/openapi/svc-a.json: stale",
        "docs/openapi/svc-b.json: missing",
    ]
    assert "Regenerate with: python scripts/generate_openapi.py" in captured.err
    assert (output_dir / "svc-a.json").read_text(encoding="utf-8") == "{}\n"
    assert not (output_dir / "svc-b.json").exists()


def test_check_is_silent_and_successful_when_documents_are_current(
    workspace: Path, apps: dict[str, _FakeApp], capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = workspace / "docs" / "openapi"
    output_dir.mkdir(parents=True)
    for service, app in apps.items():
        (output_dir / f"{service}.json").write_text(_expected(app), encoding="utf-8")

    assert generate_openapi.main(["--check"]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_check_reports_a_partially_stale_set(
    workspace: Path, apps: dict[str, _FakeApp], capsys: pytest.CaptureFixture[str]
) -> None:
    """One current document does not mask a stale sibling."""
    output_dir = workspace / "docs" / "openapi"
    output_dir.mkdir(parents=True)
    (output_dir / "svc-a.json").write_text(_expected(apps["svc-a"]), encoding="utf-8")
    (output_dir / "svc-b.json").write_text(_expected(apps["svc-a"]), encoding="utf-8")

    assert generate_openapi.main(["--check"]) == 1

    assert capsys.readouterr().out.splitlines() == ["docs/openapi/svc-b.json: stale"]


def test_main_rejects_unknown_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        generate_openapi.main(["--write"])
    assert excinfo.value.code == 2
    assert "unrecognized arguments: --write" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# load_apps: import-path bootstrap
# ---------------------------------------------------------------------------


def test_load_apps_puts_the_repository_root_first_on_sys_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running the script from another directory must still import ``gco``."""
    root = str(generate_openapi.REPO_ROOT)
    monkeypatch.setenv("GCO_DEV_MODE", "true")
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != root])

    apps = generate_openapi.load_apps()

    assert sys.path[0] == root
    assert sys.path.count(root) == 1
    assert tuple(apps) == generate_openapi.SERVICE_NAMES
    for app in apps.values():
        assert callable(app.openapi)


def test_load_apps_does_not_duplicate_an_existing_sys_path_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = str(generate_openapi.REPO_ROOT)
    monkeypatch.setenv("GCO_DEV_MODE", "true")
    monkeypatch.setattr(sys, "path", [root, *(entry for entry in sys.path if entry != root)])

    generate_openapi.load_apps()

    assert sys.path.count(root) == 1
