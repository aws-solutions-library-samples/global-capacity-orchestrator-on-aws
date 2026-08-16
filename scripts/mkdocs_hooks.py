"""MkDocs hook: serve the repository's tracked ``images/`` inside the wiki.

Requirement 4.2 of the github-pages-wiki spec forbids wiki pages from using
external image hosts (``raw.githubusercontent.com``) *and* from committing
duplicate copies of tracked binaries. MkDocs, however, only serves files
under ``docs_dir`` (``wiki/``). This hook closes that gap: ``on_files``
injects every file under the repo's ``images/`` directory into the build as
``assets/images/<name>``, so wiki pages reference ``assets/images/x.png``,
strict link validation sees a real file, and the screenshots stay
single-source (regenerating a screenshot updates the wiki automatically).

Wired via the ``hooks:`` key in ``mkdocs.yml``. The ``assets/images/`` →
``images/`` mapping is mirrored by ``tests/test_wiki.py``, which asserts
every image referenced by a wiki page exists in ``images/``.
"""

from __future__ import annotations

from pathlib import Path

from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import File, Files

#: Repository root (this file lives in ``scripts/``).
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Source directory of tracked images and its path prefix inside the site.
_IMAGES_DIR = _REPO_ROOT / "images"
_SITE_PREFIX = "assets/images"


def on_files(files: Files, config: MkDocsConfig) -> Files:
    """Inject every tracked image as ``assets/images/<name>``.

    ``File.generated`` (MkDocs >= 1.6) registers a file that lives outside
    ``docs_dir``; passing ``abs_src_path`` makes the build copy the real
    on-disk bytes, so nothing is duplicated in the repository. The README
    inside ``images/`` is documentation for contributors, not a site asset.
    """
    for path in sorted(_IMAGES_DIR.iterdir()):
        if not path.is_file() or path.name == "README.md":
            continue
        files.append(
            File.generated(
                config,
                src_uri=f"{_SITE_PREFIX}/{path.name}",
                abs_src_path=str(path),
            )
        )
    return files
