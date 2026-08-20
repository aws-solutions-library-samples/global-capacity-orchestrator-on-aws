"""Unit tests for :mod:`diagrams.code_diagrams.generate` and helpers.

Scope:

* **Targets module** — :func:`Target.slug` and :data:`TARGETS`
  well-formedness (every source file exists, every function name is a
  valid Python identifier or dotted path).
* **Renderer path/timestamp behavior** — :func:`_output_stem_for` mirrors the
  source tree correctly, reproducible timestamps propagate into generated
  HTML/catalogue metadata, and HTML-only runs delete stale PNGs.
* **Source marker** — :func:`upsert_markers` strips any existing
  marker block first (so placement-rule changes take effect on the
  next run without a separate cleanup pass), inserts the fresh block
  in the right place, is idempotent across repeated runs, and never
  duplicates existing blocks. :func:`strip_all_markers` provides the
  same teardown helper as a standalone CLI action
  (``--strip-markers``).
* **README renderer** — :func:`render_readme` groups by top-level
  directory, lists entries in insertion order, and degrades gracefully
  when a target has no PNG.

PNG rendering itself (Playwright) is not exercised here — those tests
live behind the ``diagrams`` extra and a working Chromium, which the
standard CI matrix doesn't carry. The generator falls back to HTML-only
output when Playwright is absent, so the interactive HTML path is
covered by the pyflowchart import in the renderer's unit tests below.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Skip the whole module if pyflowchart isn't installed — the renderer's
# control-flow module imports it eagerly at module scope via
# :mod:`diagrams.code_diagrams.generate`.
pytest.importorskip("pyflowchart")


# ---------------------------------------------------------------------------
# Import the code_diagrams sub-modules. They live under ``diagrams/`` which
# is not a Python package in the project's ``setuptools.packages.find``
# (see ``pyproject.toml``), so we import by file path.
# ---------------------------------------------------------------------------


def _load(module_name: str, path: Path) -> object:
    import sys

    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``@dataclass`` walks ``sys.modules`` to resolve forward references,
    # so the module must be registered before ``exec_module`` runs.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parent.parent
_CD_DIR = ROOT / "diagrams" / "code_diagrams"

targets_mod = _load("_cd_targets", _CD_DIR / "_targets.py")
renderer_mod = _load("_cd_renderer", _CD_DIR / "_renderer.py")
source_marker_mod = _load("_cd_source_marker", _CD_DIR / "_source_marker.py")
readme_mod = _load("_cd_readme", _CD_DIR / "_readme.py")

timestamp_mod = _load("_cd_timestamp", _CD_DIR / "_timestamp.py")

Target = targets_mod.Target
TARGETS = targets_mod.TARGETS
_output_stem_for = renderer_mod._output_stem_for
_render_one = renderer_mod._render_one
RenderedTarget = renderer_mod.RenderedTarget
SENTINEL = source_marker_mod.SENTINEL
upsert_markers = source_marker_mod.upsert_markers
strip_markers_from = source_marker_mod.strip_markers_from
strip_all_markers = source_marker_mod.strip_all_markers
render_readme = readme_mod.render_readme
generation_timestamp_utc = timestamp_mod.generation_timestamp_utc


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


class TestTargetSlug:
    """``Target.slug`` strips dots so dotted method names (``Cls.method``)
    produce filesystem-safe output stems."""

    def test_plain_function_name_is_unchanged(self) -> None:
        t = Target(source="x.py", function="lambda_handler")
        assert t.slug() == "lambda_handler"

    def test_dotted_method_becomes_underscore(self) -> None:
        t = Target(source="x.py", function="Foo.bar")
        assert t.slug() == "Foo_bar"


class TestTargetsCatalogue:
    """Every entry in :data:`TARGETS` must resolve to a real source file
    with a matching top-level function — otherwise running the generator
    blows up mid-batch."""

    def test_all_source_files_exist(self) -> None:
        missing = [t.source for t in TARGETS if not (ROOT / t.source).is_file()]
        assert not missing, (
            f"TARGETS references non-existent source files: {missing!r}. "
            "Either fix the Target.source path or remove the entry."
        )

    def test_all_functions_are_valid_identifiers(self) -> None:
        """Function names are passed verbatim to ``pyflowchart`` which
        accepts ``Class.method`` syntax — we assert each dotted part is
        a valid Python identifier."""
        for t in TARGETS:
            parts = t.function.split(".")
            bad = [p for p in parts if not p.isidentifier()]
            assert not bad, (
                f"Target {t.source}::{t.function!r} has non-identifier "
                f"segment(s) {bad!r}. pyflowchart --field only accepts "
                "plain identifiers and dotted ``Class.method`` paths."
            )


# ---------------------------------------------------------------------------
# Renderer path math
# ---------------------------------------------------------------------------


class TestOutputStemFor:
    """``_output_stem_for`` must mirror the source layout and survive the
    dotted-function edge case. ``Path.with_suffix`` treats ``.handler`` as
    a suffix and strips it, which is why the renderer hand-builds the stem.
    """

    def test_stem_mirrors_source_tree(self, tmp_path: Path) -> None:
        t = Target(
            source="lambda/example/handler.py",
            function="lambda_handler",
        )
        stem = _output_stem_for(t, output_dir=tmp_path)
        assert stem == tmp_path / "lambda/example/handler.lambda_handler"

    def test_dotted_function_does_not_get_stripped(self, tmp_path: Path) -> None:
        t = Target(source="cli/main.py", function="cli.run")
        stem = _output_stem_for(t, output_dir=tmp_path)
        # Slug collapses the dot; ``.run`` must NOT be interpreted as a suffix.
        assert stem == tmp_path / "cli/main.cli_run"
        assert stem.name.endswith("cli_run")

    def test_appending_html_suffix_gives_expected_path(self, tmp_path: Path) -> None:
        """Regression guard for the ``with_suffix`` bug that would have
        produced ``handler.html`` instead of
        ``handler.lambda_handler.html``."""
        t = Target(source="lambda/x/handler.py", function="lambda_handler")
        stem = _output_stem_for(t, output_dir=tmp_path)
        html_path = stem.parent / f"{stem.name}.html"
        assert html_path.name == "handler.lambda_handler.html"


class TestGenerationTimestamp:
    """One validated timestamp must propagate without mixed-age PNG output."""

    def test_source_date_epoch_is_reproducible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "1784203200")
        assert generation_timestamp_utc() == "2026-07-16T12:00:00Z"

    def test_invalid_source_date_epoch_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-an-integer")
        with pytest.raises(ValueError, match="integer Unix timestamp"):
            generation_timestamp_utc()

    def test_html_only_render_removes_stale_png_and_stamps_html(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pyflowchart

        source = tmp_path / "example.py"
        source.write_text("def f():\n    return True\n", encoding="utf-8")
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        target = Target(source="example.py", function="f")
        stem = _output_stem_for(target, output_dir=output_dir)
        stale_png = stem.parent / f"{stem.name}.png"
        stale_png.parent.mkdir(parents=True)
        stale_png.write_bytes(b"older-png")

        class FakeFlowchart:
            @classmethod
            def from_code(cls, code: str, *, field: str, inner: bool) -> FakeFlowchart:
                assert "def f" in code
                assert field == "f"
                assert inner is True
                return cls()

            def flowchart(self) -> str:
                return "st=>start: Start"

        def fake_output_html(path: str, _title: str, _dsl: str) -> None:
            Path(path).write_text(
                '<html>\n<head>\n        <meta charset="utf-8">\n</head>\n'
                '<body>\n        <div id="canvas"></div>\n</body>\n</html>\n',
                encoding="utf-8",
            )

        monkeypatch.setattr(pyflowchart, "Flowchart", FakeFlowchart)
        monkeypatch.setattr(pyflowchart, "output_html", fake_output_html)
        generated_at = "2026-07-16T12:00:00Z"

        result = _render_one(
            target=target,
            project_root=tmp_path,
            output_dir=output_dir,
            renderer=None,
            generated_at=generated_at,
        )

        assert result.png_path is None
        assert not stale_png.exists()
        html = result.html_path.read_text(encoding="utf-8")
        assert f'<meta name="gco-generated-at" content="{generated_at}">' in html
        assert f"<!-- Generated at (UTC): {generated_at} -->" in html
        assert f'<time datetime="{generated_at}">{generated_at}</time>' in html


# ---------------------------------------------------------------------------
# Source marker idempotence
# ---------------------------------------------------------------------------


def _make_rendered(
    project_root: Path,
    source: str,
    function: str,
    *,
    with_png: bool = True,
) -> RenderedTarget:
    """Build a :class:`RenderedTarget` fixture without running pyflowchart."""
    stem = (
        project_root
        / "diagrams/code_diagrams"
        / Path(source).parent
        / (f"{Path(source).stem}.{function.replace('.', '_')}")
    )
    html = stem.parent / f"{stem.name}.html"
    png = stem.parent / f"{stem.name}.png" if with_png else None
    return RenderedTarget(
        target=Target(source=source, function=function),
        html_path=html,
        png_path=png,
        generated_at="2026-07-16T12:00:00Z",
    )


class TestUpsertMarkers:
    """Insert once, then run again — the second pass must replace (not
    duplicate) the marker block."""

    def _write_source(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_inserts_block_after_module_docstring_and_imports(self, tmp_path: Path) -> None:
        """With a docstring + real imports, the marker sits below the
        imports so ruff's import sorter doesn't treat it as a section
        boundary that forces reordering of surrounding import statements."""
        src_rel = "mymod/handler.py"
        src_path = tmp_path / src_rel
        self._write_source(
            src_path,
            '"""Handler docstring."""\n\nimport os\n\n\ndef f():\n    return os.getcwd()\n',
        )

        rendered = _make_rendered(tmp_path, src_rel, "f")
        upsert_markers([rendered], project_root=tmp_path)

        updated = src_path.read_text(encoding="utf-8")
        assert SENTINEL in updated, "Expected marker sentinel to be inserted"
        assert "# Generated at (UTC): 2026-07-16T12:00:00Z" in updated
        # Marker must sit *below* the docstring + imports and *above*
        # the first real statement (``def f``).
        docstring_end = updated.index('"""Handler docstring."""') + len('"""Handler docstring."""')
        import_start = updated.index("import os")
        marker_start = updated.index(f"# <{SENTINEL}> BEGIN")
        def_start = updated.index("def f():")
        assert docstring_end < import_start < marker_start < def_start

    def test_rerun_is_idempotent(self, tmp_path: Path) -> None:
        src_rel = "mymod/handler.py"
        src_path = tmp_path / src_rel
        self._write_source(
            src_path,
            '"""Docstring."""\n\ndef f():\n    pass\n',
        )

        rendered = _make_rendered(tmp_path, src_rel, "f")

        # Two runs must converge on the same content — no stacked markers.
        upsert_markers([rendered], project_root=tmp_path)
        first = src_path.read_text(encoding="utf-8")
        upsert_markers([rendered], project_root=tmp_path)
        second = src_path.read_text(encoding="utf-8")

        assert first == second
        assert second.count(f"# <{SENTINEL}> BEGIN") == 1
        assert second.count(f"# <{SENTINEL}> END") == 1

    def test_handles_missing_docstring(self, tmp_path: Path) -> None:
        """Files without a module docstring still get a marker — the
        block just lands at the top. Allow for a leading blank line
        that separates the block from (non-existent) imports."""
        src_rel = "mymod/nodoc.py"
        src_path = tmp_path / src_rel
        self._write_source(src_path, "def f():\n    pass\n")

        rendered = _make_rendered(tmp_path, src_rel, "f")
        upsert_markers([rendered], project_root=tmp_path)

        updated = src_path.read_text(encoding="utf-8")
        marker_idx = updated.index(f"# <{SENTINEL}> BEGIN")
        def_idx = updated.index("def f():")
        assert marker_idx < def_idx
        # Nothing of substance between the top of the file and the
        # marker — at most whitespace.
        assert updated[:marker_idx].strip() == ""

    def test_collapses_multi_target_sources_into_one_block(self, tmp_path: Path) -> None:
        """One source with two charted functions → one marker block
        listing both."""
        src_rel = "multi/handler.py"
        src_path = tmp_path / src_rel
        self._write_source(
            src_path,
            '"""Multi-handler docstring."""\n\ndef alpha():\n    pass\n\ndef beta():\n    pass\n',
        )

        results = [
            _make_rendered(tmp_path, src_rel, "alpha"),
            _make_rendered(tmp_path, src_rel, "beta"),
        ]
        upsert_markers(results, project_root=tmp_path)

        updated = src_path.read_text(encoding="utf-8")
        assert updated.count(f"# <{SENTINEL}> BEGIN") == 1
        assert "``alpha``" in updated
        assert "``beta``" in updated

    def test_marker_survives_future_imports_and_regular_imports(self, tmp_path: Path) -> None:
        """``from __future__ import ...`` and subsequent regular imports
        all appear above the marker block — ruff's import sorter groups
        imports together and treats a comment in the middle as a section
        boundary, which would force reordering."""
        src_rel = "mymod/fut.py"
        src_path = tmp_path / src_rel
        self._write_source(
            src_path,
            '"""Docstring."""\n\nfrom __future__ import annotations\n\n'
            "import os\n\n\ndef f():\n    return os.getcwd()\n",
        )

        rendered = _make_rendered(tmp_path, src_rel, "f")
        upsert_markers([rendered], project_root=tmp_path)

        updated = src_path.read_text(encoding="utf-8")
        future_idx = updated.index("from __future__ import annotations")
        import_idx = updated.index("import os")
        marker_idx = updated.index(f"# <{SENTINEL}> BEGIN")
        def_idx = updated.index("def f():")
        assert future_idx < import_idx < marker_idx < def_idx


# ---------------------------------------------------------------------------
# Marker stripping
# ---------------------------------------------------------------------------


class TestStripMarkers:
    """``strip_markers_from`` + ``strip_all_markers`` implement the
    cleanup path. The strip is called automatically on every
    ``upsert_markers`` run so a placement-rule change (e.g. moving the
    block from "after ``__future__``" to "after all imports") lands in
    the right spot without a separate cleanup pass. It's also exposed
    on the CLI via ``--strip-markers`` for explicit teardown.
    """

    def _write_source(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_strip_markers_from_removes_block(self) -> None:
        src = (
            '"""doc."""\n\nimport os\n\n'
            f"# <{SENTINEL}> BEGIN - auto-inserted, do not edit\n"
            "# Flowchart(s) generated from this file:\n"
            f"# <{SENTINEL}> END\n\n"
            "def f():\n    return os.getcwd()\n"
        )
        stripped = strip_markers_from(src)
        assert SENTINEL not in stripped
        # The strip must collapse run-away ``\n{4,}`` sequences down to
        # ``\n\n\n`` (two blank lines) so the result is formatter-stable.
        # A lone ``\n\n\n`` (two blank lines between top-level defs) is
        # PEP 8 and is preserved.
        assert "\n\n\n\n" not in stripped

    def test_strip_markers_noop_when_absent(self) -> None:
        src = '"""doc."""\n\nimport os\n\ndef f():\n    return os.getcwd()\n'
        assert strip_markers_from(src) == src

    def test_strip_all_markers_walks_standard_roots(self, tmp_path: Path) -> None:
        """``strip_all_markers`` covers ``app.py`` + ``cli/`` + ``gco/``
        + ``lambda/``, skips the packaged bundle dirs, and returns
        the number of modified files."""
        # Set up a miniature project tree.
        self._write_source(
            tmp_path / "app.py",
            f'"""doc."""\n# <{SENTINEL}> BEGIN\n# <{SENTINEL}> END\n\ndef f():\n    pass\n',
        )
        self._write_source(
            tmp_path / "cli" / "jobs.py",
            f'"""doc."""\n# <{SENTINEL}> BEGIN\n# <{SENTINEL}> END\n\ndef f():\n    pass\n',
        )
        self._write_source(
            tmp_path / "gco" / "stacks" / "global_stack.py",
            f'"""doc."""\n# <{SENTINEL}> BEGIN\n# <{SENTINEL}> END\n\ndef f():\n    pass\n',
        )
        self._write_source(
            tmp_path / "lambda" / "helm-installer" / "handler.py",
            f'"""doc."""\n# <{SENTINEL}> BEGIN\n# <{SENTINEL}> END\n\ndef f():\n    pass\n',
        )
        # Bundle dirs must be skipped — the marker here is NOT ours and
        # must not be touched (in the real tree these hold vendored
        # dependency copies).
        self._write_source(
            tmp_path / "lambda" / "helm-installer-build" / "handler.py",
            f'"""doc."""\n# <{SENTINEL}> BEGIN\n# <{SENTINEL}> END\n\ndef f():\n    pass\n',
        )

        modified = strip_all_markers(tmp_path)

        assert modified == 4
        # Walked files have no marker.
        for rel in (
            "app.py",
            "cli/jobs.py",
            "gco/stacks/global_stack.py",
            "lambda/helm-installer/handler.py",
        ):
            assert SENTINEL not in (tmp_path / rel).read_text()
        # Bundle dir untouched.
        assert SENTINEL in (tmp_path / "lambda" / "helm-installer-build" / "handler.py").read_text()

    def test_upsert_strip_then_insert_repositions_stale_block(self, tmp_path: Path) -> None:
        """If a marker exists in a stale location (e.g. above the
        imports — where an older generator version put it), re-running
        ``upsert_markers`` must move it to the current target spot
        rather than leaving the stale block and duplicating a fresh
        one below.
        """
        src_rel = "mymod/handler.py"
        src_path = tmp_path / src_rel
        self._write_source(
            src_path,
            '"""Handler docstring."""\n'
            # Stale block placed directly under the docstring (old layout).
            f"# <{SENTINEL}> BEGIN - stale\n"
            "# Flowchart(s) generated from this file:\n"
            f"# <{SENTINEL}> END\n"
            "\nimport os\n\n\ndef f():\n    return os.getcwd()\n",
        )

        rendered = _make_rendered(tmp_path, src_rel, "f")
        upsert_markers([rendered], project_root=tmp_path)

        updated = src_path.read_text(encoding="utf-8")
        # Exactly one marker block — the stale one was stripped first.
        assert updated.count(f"# <{SENTINEL}> BEGIN") == 1
        # And the sole block is below the import, not above it.
        import_idx = updated.index("import os")
        marker_idx = updated.index(f"# <{SENTINEL}> BEGIN")
        assert import_idx < marker_idx


# ---------------------------------------------------------------------------
# README renderer
# ---------------------------------------------------------------------------


class TestRenderReadme:
    """The README is regenerated on every run — the renderer must group
    by top-level directory and degrade gracefully when PNG is missing."""

    def test_groups_by_top_level_directory(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        output_dir.mkdir(parents=True)
        results = [
            _make_rendered(tmp_path, "lambda/a/h.py", "f"),
            _make_rendered(tmp_path, "cli/commands/c.py", "g"),
            _make_rendered(tmp_path, "lambda/b/h.py", "f"),
        ]
        rendered = render_readme(results, output_dir=output_dir)
        assert "<!-- Generated at (UTC): 2026-07-16T12:00:00Z -->" in rendered
        assert "*Generated at (UTC): `2026-07-16T12:00:00Z`.*" in rendered

        # Top-level groups are alphabetized, which places ``cli/`` before
        # ``lambda/`` — deterministic ordering matters for stable diffs.
        cli_idx = rendered.index("### `cli/`")
        lambda_idx = rendered.index("### `lambda/`")
        assert cli_idx < lambda_idx

    def test_entries_include_html_and_png_links(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        output_dir.mkdir(parents=True)
        results = [_make_rendered(tmp_path, "lambda/x/h.py", "f")]
        rendered = render_readme(results, output_dir=output_dir)
        # The path uses POSIX separators regardless of platform so the
        # links work on every OS and in GitHub's web viewer.
        assert "[HTML](./lambda/x/h.f.html)" in rendered
        assert "[PNG](./lambda/x/h.f.png)" in rendered

    def test_entries_without_png_omit_png_link(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        output_dir.mkdir(parents=True)
        results = [
            _make_rendered(tmp_path, "lambda/x/h.py", "f", with_png=False),
        ]
        rendered = render_readme(results, output_dir=output_dir)
        assert "[HTML](" in rendered
        assert "[PNG]" not in rendered

    def test_includes_chromium_install_note(self, tmp_path: Path) -> None:
        """The README must tell users how to fetch Chromium for the PNG
        step — that's the single most common reason a regeneration fails
        in a fresh checkout."""
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        output_dir.mkdir(parents=True)
        rendered = render_readme([], output_dir=output_dir)
        assert "playwright install chromium" in rendered, (
            "README must document the one-time ``playwright install chromium`` "
            "step, otherwise users hit a confusing warning and skip PNG output."
        )


# ---------------------------------------------------------------------------
# Shared Lambda copy sync
# ---------------------------------------------------------------------------

generate_mod = _load("_cd_generate", _CD_DIR / "generate.py")
_sync_shared_lambda_copies = generate_mod._sync_shared_lambda_copies


class TestSyncSharedLambdaCopies:
    """The generator propagates canonical shared sources to their copies.

    ``upsert_markers`` rewrites the pyflowchart header inside canonical
    shared Lambda sources; without this sync a full regeneration left the
    checked-in copies one header behind — the exact drift
    ``tests/test_lambda_shared_sources.py`` rejects. These tests build a fake
    project tree so they exercise the sync against the real
    ``cli.stacks.LAMBDA_SHARED_SOURCE_TARGETS`` map without touching the
    checkout.
    """

    @staticmethod
    def _tree(tmp_path: Path) -> Path:
        from cli.stacks import LAMBDA_SHARED_SOURCE_TARGETS

        for source_rel, target_rels in LAMBDA_SHARED_SOURCE_TARGETS.items():
            source = tmp_path / source_rel
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"# canonical v2\n")
            for target_rel in target_rels:
                target = tmp_path / target_rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"# stale v1\n")
        return tmp_path

    def test_drifted_copies_are_rewritten_to_canonical_bytes(self, tmp_path: Path) -> None:
        from cli.stacks import LAMBDA_SHARED_SOURCE_TARGETS

        root = self._tree(tmp_path)
        _sync_shared_lambda_copies(root)
        for source_rel, target_rels in LAMBDA_SHARED_SOURCE_TARGETS.items():
            expected = (root / source_rel).read_bytes()
            for target_rel in target_rels:
                assert (root / target_rel).read_bytes() == expected, target_rel

    def test_identical_copies_are_left_untouched(self, tmp_path: Path) -> None:
        from cli.stacks import LAMBDA_SHARED_SOURCE_TARGETS

        root = self._tree(tmp_path)
        _sync_shared_lambda_copies(root)
        # Second run: nothing differs, so no mtimes may change (the sync
        # must not churn tracked files on every regeneration).
        stats = {
            target_rel: (root / target_rel).stat().st_mtime_ns
            for target_rels in LAMBDA_SHARED_SOURCE_TARGETS.values()
            for target_rel in target_rels
        }
        _sync_shared_lambda_copies(root)
        for target_rel, mtime in stats.items():
            assert (root / target_rel).stat().st_mtime_ns == mtime, target_rel

    def test_missing_canonical_or_target_dir_is_skipped(self, tmp_path: Path) -> None:
        # An empty tree exercises both guards: absent canonical sources and
        # absent consumer directories must be non-fatal no-ops.
        _sync_shared_lambda_copies(tmp_path)
