"""Tests for ``scripts/mkdocs_hooks.py``, the wiki image-injection hook.

The hook exists so wiki pages can reference ``assets/images/<name>`` while the
only copy of each screenshot stays in the tracked ``images/`` directory. These
tests register the module with a real ``MkDocsConfig`` exactly the way MkDocs
registers ``hooks:`` entries, then dispatch the ``files`` event through
``PluginCollection`` and pin what ``on_files`` produces: every regular file in
the images directory becomes a generated ``File`` at ``assets/images/<name>``
backed by the on-disk path (no bytes copied), ``README.md`` and subdirectories
are skipped, ordering is deterministic, and the incoming ``Files`` collection
is extended in place and returned. A final test runs the hook against the
repository's real ``images/`` directory so the mapping ``tests/test_wiki.py``
relies on is verified end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import File, Files

from scripts import mkdocs_hooks

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def config(tmp_path: Path) -> MkDocsConfig:
    """A validated MkDocs configuration with the hook registered as a plugin.

    ``Hooks.post_validation`` in MkDocs does ``plugins[name] = module`` for every
    ``hooks:`` entry; doing the same here means ``on_files`` runs through
    ``PluginCollection.run_event`` with the plugin bookkeeping MkDocs performs
    during a real build (``File.generated`` records the originating hook).
    """
    docs_dir = tmp_path / "wiki"
    docs_dir.mkdir()
    (docs_dir / "index.md").write_text("# Wiki\n", encoding="utf-8")
    cfg = MkDocsConfig(config_file_path=str(tmp_path / "mkdocs.yml"))
    cfg.load_dict(
        {
            "site_name": "Hook test",
            "docs_dir": str(docs_dir),
            "site_dir": str(tmp_path / "site"),
            "plugins": [],
        }
    )
    errors, warnings = cfg.validate()
    assert (errors, warnings) == ([], [])
    cfg.plugins["gco-images"] = mkdocs_hooks
    return cfg


def _index_file(config: MkDocsConfig) -> File:
    return File(
        "index.md",
        src_dir=config.docs_dir,
        dest_dir=config.site_dir,
        use_directory_urls=config.use_directory_urls,
    )


def _dispatch(config: MkDocsConfig, files: Files) -> Files:
    """Run the ``files`` event the way ``mkdocs build`` does."""
    return config.plugins.on_files(files, config=config)


def _empty_dir(tmp_path: Path) -> Path:
    """A stand-in source directory with nothing to inject."""
    path = tmp_path / "empty"
    path.mkdir(exist_ok=True)
    return path


def test_on_files_injects_tracked_images_as_site_assets(
    tmp_path: Path, config: MkDocsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    images = tmp_path / "images"
    images.mkdir()
    (images / "zeta.png").write_bytes(b"\x89PNG")
    (images / "alpha.svg").write_text("<svg/>", encoding="utf-8")
    (images / "README.md").write_text("# Contributor notes\n", encoding="utf-8")
    (images / "nested").mkdir()
    (images / "nested" / "ignored.png").write_bytes(b"\x89PNG")
    monkeypatch.setattr(mkdocs_hooks, "_IMAGES_DIR", images)
    monkeypatch.setattr(mkdocs_hooks, "_API_SPECS_DIR", _empty_dir(tmp_path))
    incoming = Files([_index_file(config)])

    result = _dispatch(config, incoming)

    assert result is incoming, "the hook extends and returns the collection it was given"
    assert [file.src_uri for file in result] == [
        "index.md",
        "assets/images/alpha.svg",
        "assets/images/zeta.png",
    ]
    injected = {file.src_uri: file for file in result if file.src_uri != "index.md"}
    for name in ("alpha.svg", "zeta.png"):
        file = injected[f"assets/images/{name}"]
        assert file.abs_src_path == str(images / name), "served from disk, not a copy"
        assert file.dest_uri == f"assets/images/{name}"
        assert file.url == f"assets/images/{name}"
        assert file.generated_by == "gco-images"
        assert file.is_media_file()
    assert result.get_file_from_path("assets/images/README.md") is None
    assert result.get_file_from_path("assets/images/ignored.png") is None


def test_on_files_with_no_eligible_images_leaves_the_collection_unchanged(
    tmp_path: Path, config: MkDocsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    images = tmp_path / "images"
    images.mkdir()
    (images / "README.md").write_text("only notes\n", encoding="utf-8")
    monkeypatch.setattr(mkdocs_hooks, "_IMAGES_DIR", images)
    monkeypatch.setattr(mkdocs_hooks, "_API_SPECS_DIR", _empty_dir(tmp_path))

    result = _dispatch(config, Files([_index_file(config)]))

    assert [file.src_uri for file in result] == ["index.md"]


def test_on_files_serves_bytes_from_the_tracked_file(
    tmp_path: Path, config: MkDocsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generated File reads the tracked bytes lazily, so nothing is duplicated."""
    images = tmp_path / "images"
    images.mkdir()
    payload = b"\x89PNG\r\n\x1a\nfake"
    (images / "shot.png").write_bytes(payload)
    monkeypatch.setattr(mkdocs_hooks, "_IMAGES_DIR", images)
    monkeypatch.setattr(mkdocs_hooks, "_API_SPECS_DIR", _empty_dir(tmp_path))

    result = _dispatch(config, Files([]))
    file = result.get_file_from_path("assets/images/shot.png")

    assert file is not None
    assert file.content_bytes == payload


def test_hook_maps_every_tracked_repository_image_and_spec_sheet(config: MkDocsConfig) -> None:
    """Against the real repository: one asset per tracked image (no README), one page per sheet."""
    expected_images = sorted(
        path.name
        for path in (REPO_ROOT / "images").iterdir()
        if path.is_file() and path.name != "README.md"
    )
    catalogue = REPO_ROOT / "diagrams" / "api_specs"
    expected_sheets = sorted(
        path.name for path in catalogue.iterdir() if path.suffix in {".md", ".svg"}
    )
    assert expected_images, "the repository ships tracked images"
    assert (REPO_ROOT / "images" / "README.md").is_file()
    assert "README.md" in expected_sheets and len(expected_sheets) > 1, "the catalogue ships sheets"
    assert "api-topology.svg" in expected_sheets, "the catalogue ships its interaction diagram"

    result = _dispatch(config, Files([]))

    assert [file.src_uri for file in result] == [
        *(f"assets/images/{name}" for name in expected_images),
        *(f"api/{name}" for name in expected_sheets),
    ]
    diagram = result.get_file_from_path("api/api-topology.svg")
    assert diagram is not None and diagram.is_media_file()
    assert diagram.url == "api/api-topology.svg", "the index embeds it as a sibling image"
    for file in result:
        assert file.abs_src_path is not None
        assert Path(file.abs_src_path).parent in {
            REPO_ROOT / "images",
            REPO_ROOT / "diagrams" / "api_specs",
        }
        assert Path(file.abs_src_path).is_file()
    assert mkdocs_hooks._IMAGES_DIR == REPO_ROOT / "images"
    assert mkdocs_hooks._SITE_PREFIX == "assets/images"
    assert mkdocs_hooks._API_SPECS_DIR == REPO_ROOT / "diagrams" / "api_specs"
    assert mkdocs_hooks._API_SITE_PREFIX == "api"


def test_on_files_injects_only_the_markdown_sheets_as_api_pages(
    tmp_path: Path, config: MkDocsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generator, its package marker and caches are not site content; the diagram is."""
    specs = tmp_path / "api_specs"
    specs.mkdir()
    (specs / "README.md").write_text("# Index\n", encoding="utf-8")
    (specs / "manifest-processor.md").write_text("# Sheet\n", encoding="utf-8")
    (specs / "api-topology.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>\n")
    (specs / "generate.py").write_text("print()\n", encoding="utf-8")
    (specs / "__init__.py").write_text("", encoding="utf-8")
    (specs / "__pycache__").mkdir()
    monkeypatch.setattr(mkdocs_hooks, "_IMAGES_DIR", _empty_dir(tmp_path))
    monkeypatch.setattr(mkdocs_hooks, "_API_SPECS_DIR", specs)

    result = _dispatch(config, Files([_index_file(config)]))

    assert [file.src_uri for file in result] == [
        "index.md",
        "api/README.md",
        "api/api-topology.svg",
        "api/manifest-processor.md",
    ]
    readme = result.get_file_from_path("api/README.md")
    sheet = result.get_file_from_path("api/manifest-processor.md")
    assert readme is not None and sheet is not None
    assert readme.is_documentation_page() and sheet.is_documentation_page()
    # README.md is the directory index: /api/ and /api/manifest-processor/.
    assert readme.url == "api/"
    assert sheet.url == "api/manifest-processor/"
    assert sheet.abs_src_path == str(specs / "manifest-processor.md"), (
        "served from disk, not a copy"
    )
    assert sheet.generated_by == "gco-images"
