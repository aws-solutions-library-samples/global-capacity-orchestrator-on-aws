"""MkDocs hook: serve tracked ``images/`` and the generated API spec sheets inside the wiki.

The wiki's content contract (enforced by ``tests/test_wiki.py``) forbids wiki
pages from using external image hosts (``raw.githubusercontent.com``) *and*
from committing duplicate copies of tracked files. MkDocs, however, only serves
files under ``docs_dir`` (``wiki/``). This hook closes that gap: ``on_files``
injects two trees that live elsewhere in the repository into the build:

* every file under ``images/`` as ``assets/images/<name>``, so wiki pages
  reference ``assets/images/x.png``, strict link validation sees a real file,
  and the screenshots stay single-source (regenerating a screenshot updates
  the wiki automatically);
* every Markdown file under ``diagrams/api_specs/`` as ``api/<name>``, so the
  API spec sheets that ``diagrams/api_specs/generate.py`` renders from the
  FastAPI-generated OpenAPI documents are wiki pages (``/api/<service>/``,
  with the catalogue's ``README.md`` serving ``/api/``) without a second copy
  that could drift from the committed one.

Wired via the ``hooks:`` key in ``mkdocs.yml``. Both mappings are mirrored by
``tests/test_wiki.py``, which asserts every image referenced by a wiki page
exists in ``images/`` and every ``api/`` nav entry has its generated sheet.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import File, Files

#: Repository root (this file lives in ``scripts/``).
_REPO_ROOT = Path(__file__).resolve().parent.parent
#: Source directory of tracked images and its path prefix inside the site.
_IMAGES_DIR = _REPO_ROOT / "images"
_SITE_PREFIX = "assets/images"
#: Source directory of the generated API spec sheets and their site prefix.
_API_SPECS_DIR = _REPO_ROOT / "diagrams" / "api_specs"
_API_SITE_PREFIX = "api"


def _inject(
    files: Files,
    config: MkDocsConfig,
    source_dir: Path,
    prefix: str,
    keep: Callable[[Path], bool],
) -> None:
    """Register ``source_dir/<name>`` as ``<prefix>/<name>`` for each kept file.

    ``File.generated`` (MkDocs >= 1.6) registers a file that lives outside
    ``docs_dir``; passing ``abs_src_path`` makes the build copy the real
    on-disk bytes, so nothing is duplicated in the repository. Only direct
    children are considered.
    """
    for path in sorted(source_dir.iterdir()):
        if path.is_file() and keep(path):
            files.append(
                File.generated(config, src_uri=f"{prefix}/{path.name}", abs_src_path=str(path))
            )


def on_files(files: Files, config: MkDocsConfig) -> Files:
    """Inject the tracked images and the generated API spec sheets."""
    # The README inside images/ is documentation for contributors, not a site asset.
    _inject(files, config, _IMAGES_DIR, _SITE_PREFIX, lambda path: path.name != "README.md")
    # Only the Markdown sheets: the generator and its package marker stay out of the site.
    _inject(files, config, _API_SPECS_DIR, _API_SITE_PREFIX, lambda path: path.suffix == ".md")
    return files
