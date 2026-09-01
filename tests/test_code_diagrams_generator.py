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

import ast
import hashlib
import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

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
_annotate_generated_html = renderer_mod._annotate_generated_html
_render_one = renderer_mod._render_one
_screenshot_scale = renderer_mod._screenshot_scale
RenderedTarget = renderer_mod.RenderedTarget
SENTINEL = source_marker_mod.SENTINEL
upsert_markers = source_marker_mod.upsert_markers
strip_markers_from = source_marker_mod.strip_markers_from
strip_all_markers = source_marker_mod.strip_all_markers
_ruff_format = source_marker_mod._ruff_format
_update_marker_file = source_marker_mod._update_file
render_readme = readme_mod.render_readme
generation_timestamp_utc = timestamp_mod.generation_timestamp_utc
generation_source_commit = timestamp_mod.generation_source_commit


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

    def test_every_selector_resolves_to_a_function_or_method(self) -> None:
        missing: list[str] = []
        for target in TARGETS:
            tree = ast.parse((ROOT / target.source).read_text(encoding="utf-8"))
            parts = target.function.split(".")
            nodes: list[ast.stmt] = tree.body
            for index, part in enumerate(parts):
                match = next(
                    (
                        node
                        for node in nodes
                        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                        and node.name == part
                    ),
                    None,
                )
                if match is None or (
                    index < len(parts) - 1 and not isinstance(match, ast.ClassDef)
                ):
                    missing.append(f"{target.source}:{target.function}")
                    break
                nodes = match.body if isinstance(match, ast.ClassDef) else []
        assert not missing, f"diagram selectors do not resolve: {missing}"

    def test_targets_and_output_stems_are_unique(self) -> None:
        identities = [(target.source, target.function) for target in TARGETS]
        stems = [
            _output_stem_for(target, output_dir=ROOT / "diagrams" / "code_diagrams")
            for target in TARGETS
        ]
        assert len(identities) == len(set(identities)), "duplicate diagram target"
        assert len(stems) == len(set(stems)), "diagram targets collide on an output stem"


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


class TestScreenshotScale:
    def test_small_diagram_is_not_resized(self) -> None:
        assert _screenshot_scale(2_000, 1_000) == 1.0

    def test_area_cap_applies_even_below_legacy_dimension_threshold(self) -> None:
        scale = _screenshot_scale(14_000, 14_000)
        assert 0 < scale < 1
        assert (14_000 * scale) * (14_000 * scale) <= 20_000_000

    def test_dimension_cap_applies_to_extremely_wide_diagrams(self) -> None:
        scale = _screenshot_scale(20_000, 1_000)
        assert 20_000 * scale <= 8_000


class TestGenerationTimestamp:
    """One validated timestamp must propagate without mixed-age PNG output."""

    def test_source_date_epoch_is_reproducible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "1784203200")
        assert generation_timestamp_utc() == "2026-07-16T12:00:00Z"

    def test_invalid_source_date_epoch_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-an-integer")
        with pytest.raises(ValueError, match="integer Unix timestamp"):
            generation_timestamp_utc()

    def test_source_commit_is_exact_and_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GCO_DIAGRAM_SOURCE_COMMIT", "A" * 40)
        assert generation_source_commit() == "a" * 40

    @pytest.mark.parametrize("value", ("", "abc", "g" * 40, "a" * 39))
    def test_invalid_source_commit_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("GCO_DIAGRAM_SOURCE_COMMIT", value)
        with pytest.raises(ValueError, match="exact 40-character Git commit SHA"):
            generation_source_commit()

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
        source_commit = "a" * 40

        result = _render_one(
            target=target,
            project_root=tmp_path,
            output_dir=output_dir,
            renderer=None,
            generated_at=generated_at,
            source_commit=source_commit,
        )

        assert result.png_path is None
        assert not stale_png.exists()
        html = result.html_path.read_text(encoding="utf-8")
        assert f'<meta name="gco-generated-at" content="{generated_at}">' in html
        assert f"<!-- Generated at (UTC): {generated_at} -->" in html
        assert f'<time datetime="{generated_at}">{generated_at}</time>' in html
        assert f'<meta name="gco-source-commit" content="{source_commit}">' in html
        assert f"Generated from Git commit: {source_commit}" in html
        assert f"Source commit: <code>{source_commit}</code>" in html
        assert result.source_commit == source_commit
        assert '<meta name="gco-flow-digest" content="' in html
        assert "Flow content SHA-256: <code>" in html

    def test_visible_flow_digest_changes_with_pre_annotation_content(self) -> None:
        template = (
            '<html>\n<head>\n        <meta charset="utf-8">\n</head>\n'
            '<body>FLOW\n        <div id="canvas"></div>\n</body>\n</html>\n'
        )
        generated_at = "2026-07-16T12:00:00Z"
        first_input = template.replace("FLOW", "first flow")
        second_input = template.replace("FLOW", "second flow")
        source_commit = "b" * 40
        first = _annotate_generated_html(
            first_input,
            generated_at=generated_at,
            source_commit=source_commit,
        )
        second = _annotate_generated_html(
            second_input,
            generated_at=generated_at,
            source_commit=source_commit,
        )
        first_digest = hashlib.sha256(first_input.encode()).hexdigest()[:16]
        second_digest = hashlib.sha256(second_input.encode()).hexdigest()[:16]
        assert first_digest != second_digest
        assert f'<meta name="gco-flow-digest" content="{first_digest}">' in first
        assert f"Flow content SHA-256: <code>{first_digest}</code>" in first
        assert f'<meta name="gco-flow-digest" content="{second_digest}">' in second


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
        source_commit="a" * 40,
    )


class TestUpsertMarkers:
    """Insert once, then run again — the second pass must replace (not duplicate)."""

    def test_ruff_format_failure_is_not_reported_as_generation_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "example.py"
        source.write_text("def example():\n    return True\n", encoding="utf-8")
        monkeypatch.setattr(
            source_marker_mod.subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                subprocess.CalledProcessError(2, args[0])
            ),
        )
        with pytest.raises(subprocess.CalledProcessError):
            _ruff_format([source], project_root=tmp_path)

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
        assert f"# Generated from Git commit: {'a' * 40}" in updated
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
        assert f"<!-- Generated from Git commit: {'a' * 40} -->" in rendered
        assert f"*Generated from Git commit: `{'a' * 40}`.*" in rendered

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
_verify_targets_match_source_commit = generate_mod._verify_targets_match_source_commit
_without_generated_marker = generate_mod._without_generated_marker


class TestSourceCommitVerification:
    @staticmethod
    def _fake_git_run(committed: bytes, *, object_type: bytes = b"commit") -> object:
        def run(args, **_kwargs):
            if args[1:3] == ["cat-file", "-t"]:
                return SimpleNamespace(returncode=0, stdout=object_type + b"\n", stderr=b"")
            assert args[1] == "show"
            return SimpleNamespace(returncode=0, stdout=committed, stderr=b"")

        return run

    def test_markerless_source_round_trips_byte_for_byte(self, tmp_path: Path) -> None:
        original = b"import os\n\n\ndef f():\n    return True\n"
        source = tmp_path / "example.py"
        source.write_bytes(original)
        stem = tmp_path / "diagrams" / "code_diagrams" / "example.f"
        rendered = RenderedTarget(
            target=Target(source="example.py", function="f"),
            html_path=stem.with_suffix(".html"),
            png_path=stem.with_suffix(".png"),
            generated_at="2026-08-30T12:00:00Z",
            source_commit="a" * 40,
        )

        assert _update_marker_file(
            source_path=source,
            results=[rendered],
            project_root=tmp_path,
        )
        assert _without_generated_marker(source.read_bytes()) == original

    def test_generated_markers_do_not_change_source_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        committed = "# café\ndef f():\n    return True\n".encode()
        source = tmp_path / "example.py"
        source.write_bytes(
            b"# <pyflowchart-code-diagram> BEGIN - generated\n"
            b"# metadata\n"
            b"# <pyflowchart-code-diagram> END\n\n" + committed
        )
        monkeypatch.setattr(
            generate_mod.subprocess,
            "run",
            self._fake_git_run(committed),
        )

        _verify_targets_match_source_commit(
            project_root=tmp_path,
            targets=[Target(source="example.py", function="f")],
            source_commit="a" * 40,
        )

    @pytest.mark.parametrize(
        ("committed", "working"),
        (
            (
                b"def f():\n    return True\n\ndef g():\n    return True\n",
                b"def f():\n    return True\n\n\ndef g():\n    return True\n",
            ),
            (
                b"def f():\n    return True\n",
                b"def f():\r\n    return True\r\n",
            ),
            (
                b"def f():\n    return True\n",
                b"def f():\n    return False\n",
            ),
        ),
    )
    def test_any_uncommitted_source_byte_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        committed: bytes,
        working: bytes,
    ) -> None:
        source = tmp_path / "example.py"
        source.write_bytes(working)
        monkeypatch.setattr(
            generate_mod.subprocess,
            "run",
            self._fake_git_run(committed),
        )

        with pytest.raises(RuntimeError, match="Commit substantive source changes first"):
            _verify_targets_match_source_commit(
                project_root=tmp_path,
                targets=[Target(source="example.py", function="f")],
                source_commit="a" * 40,
            )

    def test_non_commit_git_object_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            generate_mod.subprocess,
            "run",
            self._fake_git_run(b"", object_type=b"tree"),
        )
        with pytest.raises(RuntimeError, match="is a tree, not a commit"):
            _verify_targets_match_source_commit(
                project_root=tmp_path,
                targets=[Target(source="example.py", function="f")],
                source_commit="a" * 40,
            )


class TestProvenanceManifestVerification:
    """The repository-side freshness contract is self-contained and per source.

    ``verify_targets_match_provenance_manifest`` compares working-tree bytes
    against the digests recorded at generation time and must never resolve the
    recorded commit from Git history — a squash-merged PR deletes its branch
    commits, which is exactly how the recorded SHA became unreachable on
    ``main`` and broke every fresh clone's contract check. Provenance is
    recorded per source so one changed file restamps only its own artifacts.
    """

    @staticmethod
    def _write_source_and_manifest(
        tmp_path: Path,
        body: bytes,
        *,
        commit: str = "a" * 40,
        generated_at: str = "2026-09-01T12:00:00Z",
        name: str = "example.py",
        function: str = "f",
    ) -> Target:
        source = tmp_path / name
        source.write_bytes(body)
        target = Target(source=name, function=function)
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        output_dir.mkdir(parents=True, exist_ok=True)
        generate_mod.write_provenance_manifest(
            project_root=tmp_path,
            output_dir=output_dir,
            regenerated_targets=[target],
            generated_at=generated_at,
            source_commit=commit,
            catalog=[target],
        )
        return target

    def test_write_then_verify_round_trips(self, tmp_path: Path) -> None:
        target = self._write_source_and_manifest(tmp_path, b"def f():\n    return True\n")
        manifest = generate_mod.verify_targets_match_provenance_manifest(
            project_root=tmp_path, targets=[target]
        )
        assert manifest["example.py"]["source_commit"] == "a" * 40
        assert manifest["example.py"]["generated_at"] == "2026-09-01T12:00:00Z"

    def test_verifier_never_consults_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreachable recorded commit must not matter to the contract.

        Squash merges legitimately orphan the recorded SHA; the check only
        stays green in every clone because it never asks Git about it.
        """

        def _no_git(*_args, **_kwargs):
            raise AssertionError("the provenance contract must not invoke subprocesses")

        monkeypatch.setattr(generate_mod.subprocess, "run", _no_git)
        target = self._write_source_and_manifest(tmp_path, b"def f():\n    return True\n")
        generate_mod.verify_targets_match_provenance_manifest(
            project_root=tmp_path, targets=[target]
        )

    def test_missing_manifest_is_actionable(self, tmp_path: Path) -> None:
        (tmp_path / "example.py").write_bytes(b"def f():\n    return True\n")
        with pytest.raises(RuntimeError, match="missing provenance.json"):
            generate_mod.verify_targets_match_provenance_manifest(
                project_root=tmp_path,
                targets=[Target(source="example.py", function="f")],
            )

    def test_out_of_sync_target_set_is_rejected(self, tmp_path: Path) -> None:
        self._write_source_and_manifest(tmp_path, b"def f():\n    return True\n")
        (tmp_path / "other.py").write_bytes(b"def g():\n    return True\n")
        with pytest.raises(RuntimeError, match="out of sync with the target catalogue"):
            generate_mod.verify_targets_match_provenance_manifest(
                project_root=tmp_path,
                targets=[Target(source="other.py", function="g")],
            )

    def test_any_substantive_source_change_is_rejected(self, tmp_path: Path) -> None:
        target = self._write_source_and_manifest(tmp_path, b"def f():\n    return True\n")
        (tmp_path / "example.py").write_bytes(b"def f():\n    return False\n")
        with pytest.raises(RuntimeError, match="Commit substantive source changes first"):
            generate_mod.verify_targets_match_provenance_manifest(
                project_root=tmp_path, targets=[target]
            )

    def test_marker_restamp_does_not_change_source_identity(self, tmp_path: Path) -> None:
        body = b"def f():\n    return True\n"
        target = self._write_source_and_manifest(tmp_path, body)
        (tmp_path / "example.py").write_bytes(
            b"# <pyflowchart-code-diagram> BEGIN - generated\n"
            b"# restamped metadata\n"
            b"# <pyflowchart-code-diagram> END\n\n" + body
        )
        generate_mod.verify_targets_match_provenance_manifest(
            project_root=tmp_path, targets=[target]
        )

    def test_regenerating_one_source_preserves_other_entries(self, tmp_path: Path) -> None:
        """A partial rewrite must not restamp sources it did not re-render.

        This is the property that keeps a PR's diagram diff proportional to
        the code it touched instead of restamping the whole catalogue.
        """
        first = self._write_source_and_manifest(
            tmp_path,
            b"def f():\n    return True\n",
            commit="a" * 40,
            generated_at="2026-09-01T12:00:00Z",
        )
        second = Target(source="other.py", function="g")
        (tmp_path / "other.py").write_bytes(b"def g():\n    return True\n")
        output_dir = tmp_path / "diagrams" / "code_diagrams"

        # Add the second source, then re-render only that one.
        generate_mod.write_provenance_manifest(
            project_root=tmp_path,
            output_dir=output_dir,
            regenerated_targets=[second],
            generated_at="2026-09-02T12:00:00Z",
            source_commit="b" * 40,
            catalog=[first, second],
        )

        manifest = generate_mod.load_provenance_manifest(tmp_path)
        assert manifest["example.py"]["generated_at"] == "2026-09-01T12:00:00Z"
        assert manifest["example.py"]["source_commit"] == "a" * 40
        assert manifest["other.py"]["generated_at"] == "2026-09-02T12:00:00Z"
        assert manifest["other.py"]["source_commit"] == "b" * 40
        # A mixed-vintage catalogue is valid, and the newest stamp is what the
        # README header records.
        assert generate_mod.newest_provenance_stamp(manifest) == (
            "2026-09-02T12:00:00Z",
            "b" * 40,
        )
        generate_mod.verify_targets_match_provenance_manifest(
            project_root=tmp_path, targets=[first, second]
        )

    def test_retired_sources_drop_out_of_the_manifest(self, tmp_path: Path) -> None:
        first = self._write_source_and_manifest(tmp_path, b"def f():\n    return True\n")
        second = Target(source="other.py", function="g")
        (tmp_path / "other.py").write_bytes(b"def g():\n    return True\n")
        generate_mod.write_provenance_manifest(
            project_root=tmp_path,
            output_dir=tmp_path / "diagrams" / "code_diagrams",
            regenerated_targets=[second],
            generated_at="2026-09-02T12:00:00Z",
            source_commit="b" * 40,
            catalog=[second],
        )
        manifest = generate_mod.load_provenance_manifest(tmp_path)
        assert set(manifest) == {"other.py"}
        assert first.source not in manifest


class TestIncrementalTargetSelection:
    """Only sources whose bytes changed (or whose artifacts vanished) re-render."""

    @staticmethod
    def _seed(tmp_path: Path) -> tuple[Target, Target, Path]:
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        output_dir.mkdir(parents=True)
        targets = []
        for name, function in (("example.py", "f"), ("other.py", "g")):
            (tmp_path / name).write_bytes(f"def {function}():\n    return True\n".encode())
            target = Target(source=name, function=function)
            targets.append(target)
            stem = renderer_mod._output_stem_for(target, output_dir=output_dir)
            stem.parent.mkdir(parents=True, exist_ok=True)
            stem.with_name(f"{stem.name}.html").write_text("<html></html>", encoding="utf-8")
            stem.with_name(f"{stem.name}.png").write_bytes(b"\x89PNG")
        generate_mod.write_provenance_manifest(
            project_root=tmp_path,
            output_dir=output_dir,
            regenerated_targets=targets,
            generated_at="2026-09-01T12:00:00Z",
            source_commit="a" * 40,
            catalog=targets,
        )
        return targets[0], targets[1], output_dir

    def test_unchanged_catalogue_selects_nothing(self, tmp_path: Path) -> None:
        first, second, output_dir = self._seed(tmp_path)
        assert (
            generate_mod.select_stale_targets(
                project_root=tmp_path, targets=[first, second], output_dir=output_dir
            )
            == []
        )

    def test_only_the_changed_source_is_selected(self, tmp_path: Path) -> None:
        first, second, output_dir = self._seed(tmp_path)
        (tmp_path / second.source).write_bytes(b"def g():\n    return False\n")
        assert generate_mod.select_stale_targets(
            project_root=tmp_path, targets=[first, second], output_dir=output_dir
        ) == [second]

    def test_marker_only_edit_selects_nothing(self, tmp_path: Path) -> None:
        """Restamping a marker is not a substantive change."""
        first, second, output_dir = self._seed(tmp_path)
        body = (tmp_path / second.source).read_bytes()
        (tmp_path / second.source).write_bytes(
            b"# <pyflowchart-code-diagram> BEGIN - generated\n"
            b"# new stamp\n"
            b"# <pyflowchart-code-diagram> END\n\n" + body
        )
        assert (
            generate_mod.select_stale_targets(
                project_root=tmp_path, targets=[first, second], output_dir=output_dir
            )
            == []
        )

    def test_missing_artifact_selects_its_target(self, tmp_path: Path) -> None:
        first, second, output_dir = self._seed(tmp_path)
        stem = renderer_mod._output_stem_for(first, output_dir=output_dir)
        stem.with_name(f"{stem.name}.png").unlink()
        assert generate_mod.select_stale_targets(
            project_root=tmp_path, targets=[first, second], output_dir=output_dir
        ) == [first]

    def test_newly_charted_source_is_selected(self, tmp_path: Path) -> None:
        first, second, output_dir = self._seed(tmp_path)
        fresh = Target(source="fresh.py", function="h")
        (tmp_path / "fresh.py").write_bytes(b"def h():\n    return True\n")
        assert generate_mod.select_stale_targets(
            project_root=tmp_path, targets=[first, second, fresh], output_dir=output_dir
        ) == [fresh]

    def test_absent_manifest_selects_everything(self, tmp_path: Path) -> None:
        first, second, output_dir = self._seed(tmp_path)
        generate_mod.provenance_manifest_path(tmp_path).unlink()
        assert generate_mod.select_stale_targets(
            project_root=tmp_path, targets=[first, second], output_dir=output_dir
        ) == [first, second]


class TestSyncSharedLambdaCopies:
    """The generator propagates canonical shared sources to their copies.

    ``upsert_markers`` rewrites the pyflowchart header inside canonical
    shared Lambda sources; without this sync a full regeneration left the
    checked-in copies one header behind — the exact drift
    ``tests/test_lambda_shared_sources.py`` rejects. These tests build a fake
    project tree so they exercise the sync against the real
    ``gco.lambda_shared_sources.LAMBDA_SHARED_SOURCE_TARGETS`` map without touching the
    checkout.
    """

    @staticmethod
    def _tree(tmp_path: Path) -> Path:
        from gco.lambda_shared_sources import LAMBDA_SHARED_SOURCE_TARGETS

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
        from gco.lambda_shared_sources import LAMBDA_SHARED_SOURCE_TARGETS

        root = self._tree(tmp_path)
        _sync_shared_lambda_copies(root)
        for source_rel, target_rels in LAMBDA_SHARED_SOURCE_TARGETS.items():
            expected = (root / source_rel).read_bytes()
            for target_rel in target_rels:
                assert (root / target_rel).read_bytes() == expected, target_rel

    def test_identical_copies_are_left_untouched(self, tmp_path: Path) -> None:
        from gco.lambda_shared_sources import LAMBDA_SHARED_SOURCE_TARGETS

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
