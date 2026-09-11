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
* **Renderer lifecycle and Playwright wrapper** — :func:`render_all`
  owns exactly one renderer and always closes it; ``_PlaywrightRenderer``
  is driven through fake browser/page/locator doubles (rescale of
  oversized SVGs, timeout and capture failures degrading to HTML-only,
  driver shutdown even when the browser refuses to close); orphan
  pruning and the README writer.
* **CLI** — :func:`main` end to end against a throwaway repository under
  ``tmp_path`` (``--strip-markers``, ``--skip-png``, ``--require-png``,
  ``--skip-marker``, ``--target``, ``--all``, incremental no-op reruns).

No real Chromium is ever launched here — those tests live behind the
``diagrams`` extra and a working browser, which the standard CI matrix
doesn't carry. The Playwright wrapper is exercised only through fakes,
and every ``main()`` run is HTML-only or uses an injected fake PNG
renderer.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from gco.lambda_shared_sources import LAMBDA_SHARED_SOURCE_TARGETS

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

    def test_absent_source_date_epoch_stamps_the_current_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reproducibility is opt-in: without the variable, record "now".

        A local run has no SOURCE_DATE_EPOCH, so this is the path humans
        actually take, and it must not raise the way a malformed value does.
        """
        monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
        before = datetime.now(UTC).replace(microsecond=0)

        stamped = generation_timestamp_utc()

        after = datetime.now(UTC).replace(microsecond=0)
        assert stamped.endswith("Z")
        parsed = datetime.strptime(stamped, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        assert before <= parsed <= after, f"{stamped} is outside the window it was taken in"

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
        with pytest.raises(RuntimeError, match=r"missing diagrams/code_diagrams/provenance\.json"):
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
        with pytest.raises(RuntimeError, match="no longer describe them"):
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


class TestPruneRetiredMarkers:
    """Only genuinely retired sources lose their marker blocks.

    A full strip-then-reinsert pass was the original source of
    whole-catalogue churn, so incremental runs prune surgically instead. The
    shared-Lambda copy case is a regression test: the first cut of this helper
    stripped those copies, which the contract separately requires to stay
    byte-identical to their canonical source.
    """

    _MARKED = (
        '"""Example."""\n\n'
        f"# <{source_marker_mod.SENTINEL}> BEGIN - auto-inserted, do not edit\n"
        "# Generated at (UTC): 2026-09-01T12:00:00Z\n"
        "# Generated from Git commit: " + "a" * 40 + "\n"
        "# Flowchart(s) generated from this file:\n"
        "#   * ``f`` -> ``diagrams/code_diagrams/x.f.html``\n"
        "#     (PNG: ``diagrams/code_diagrams/x.f.png``)\n"
        "# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> "
        "GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> "
        "python diagrams/generate.py --code-only``.\n"
        f"# <{source_marker_mod.SENTINEL}> END\n"
        "\n"
        "def f():\n    return True\n"
    )

    @staticmethod
    def _write(root: Path, relative: str, body: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def test_retired_source_is_stripped(self, tmp_path: Path) -> None:
        retired = self._write(tmp_path, "cli/retired.py", self._MARKED)
        assert generate_mod.prune_retired_markers(tmp_path, charted=set()) == 1
        assert source_marker_mod.SENTINEL not in retired.read_text(encoding="utf-8")

    def test_charted_source_is_left_untouched(self, tmp_path: Path) -> None:
        charted = self._write(tmp_path, "cli/charted.py", self._MARKED)
        before = charted.read_bytes()
        assert generate_mod.prune_retired_markers(tmp_path, charted={"cli/charted.py"}) == 0
        assert charted.read_bytes() == before

    def test_shared_lambda_copies_survive(self, tmp_path: Path) -> None:
        """The copies are legitimate marker carriers, not retired sources.

        Stripping them desynchronises them from their canonical source, which
        the shared-copy contract then reports as drift — the exact bug this
        pins down.
        """
        canonical = "lambda/proxy-shared/proxy_utils.py"
        copies = LAMBDA_SHARED_SOURCE_TARGETS[canonical]
        self._write(tmp_path, canonical, self._MARKED)
        copy_paths = [self._write(tmp_path, copy, self._MARKED) for copy in copies]

        allowed = generate_mod.marker_allowed_sources([Target(source=canonical, function="f")])
        assert generate_mod.prune_retired_markers(tmp_path, charted=allowed) == 0
        for path in copy_paths:
            assert source_marker_mod.SENTINEL in path.read_text(encoding="utf-8")

    def test_packaged_build_trees_are_skipped(self, tmp_path: Path) -> None:
        vendored = self._write(
            tmp_path, "lambda/kubectl-applier-simple-build/vendored.py", self._MARKED
        )
        before = vendored.read_bytes()
        assert generate_mod.prune_retired_markers(tmp_path, charted=set()) == 0
        assert vendored.read_bytes() == before

    def test_files_outside_the_source_roots_are_ignored(self, tmp_path: Path) -> None:
        outside = self._write(tmp_path, "scripts/helper.py", self._MARKED)
        before = outside.read_bytes()
        assert generate_mod.prune_retired_markers(tmp_path, charted=set()) == 0
        assert outside.read_bytes() == before

    def test_unmarked_sources_are_never_rewritten(self, tmp_path: Path) -> None:
        plain = self._write(tmp_path, "cli/plain.py", "def f():\n    return True\n")
        before = plain.read_bytes()
        assert generate_mod.prune_retired_markers(tmp_path, charted=set()) == 0
        assert plain.read_bytes() == before


class TestMarkerAllowedSources:
    """The generator and the contract checker must agree on marker carriers.

    Computing this set without the shared-Lambda copies is what made an early
    cut of incremental pruning strip those copies, so the computation is
    asserted directly rather than only through its callers.
    """

    def test_charted_canonical_pulls_in_its_copies(self) -> None:
        canonical, copies = next(iter(LAMBDA_SHARED_SOURCE_TARGETS.items()))
        allowed = generate_mod.marker_allowed_sources([Target(source=canonical, function="f")])
        assert canonical in allowed
        assert set(copies) <= allowed

    def test_uncharted_canonical_contributes_nothing(self) -> None:
        canonical, copies = next(iter(LAMBDA_SHARED_SOURCE_TARGETS.items()))
        allowed = generate_mod.marker_allowed_sources(
            [Target(source="cli/unrelated.py", function="f")]
        )
        assert allowed == {"cli/unrelated.py"}
        assert not set(copies) & allowed

    def test_real_catalogue_allows_every_shared_copy_it_charts(self) -> None:
        allowed = generate_mod.marker_allowed_sources(list(TARGETS))
        for canonical, copies in LAMBDA_SHARED_SOURCE_TARGETS.items():
            if canonical in allowed:
                assert set(copies) <= allowed, canonical


class TestReadmeMixedVintage:
    """The index header reports the newest generation, not a single vintage.

    Rendering used to raise when results disagreed on a timestamp or commit,
    which is incompatible with incremental regeneration — this pins the
    replacement behaviour so the guard cannot be reinstated by accident.
    """

    @staticmethod
    def _result(name: str, generated_at: str, commit: str) -> RenderedTarget:
        stem = Path("/tmp/out") / f"{name}.f"
        return RenderedTarget(
            target=Target(source=f"cli/{name}.py", function="f"),
            html_path=stem.with_suffix(".html"),
            png_path=stem.with_suffix(".png"),
            generated_at=generated_at,
            source_commit=commit,
        )

    def test_newest_stamp_wins(self) -> None:
        older = self._result("alpha", "2026-09-01T12:00:00Z", "a" * 40)
        newer = self._result("beta", "2026-09-02T12:00:00Z", "b" * 40)
        rendered = render_readme([older, newer], output_dir=Path("/tmp/out"))
        assert "2026-09-02T12:00:00Z" in rendered
        assert "b" * 40 in rendered
        assert "2026-09-01T12:00:00Z" not in rendered

    def test_newest_stamp_is_independent_of_input_order(self) -> None:
        """Row order follows the target catalogue; the header follows recency.

        Only the header is asserted here — ``_group_by_toplevel`` deliberately
        preserves each directory's ``TARGETS`` order, so the body legitimately
        changes when the caller's list order changes.
        """
        older = self._result("alpha", "2026-09-01T12:00:00Z", "a" * 40)
        newer = self._result("beta", "2026-09-02T12:00:00Z", "b" * 40)
        for ordering in ([older, newer], [newer, older]):
            header = render_readme(ordering, output_dir=Path("/tmp/out")).split("###")[0]
            assert "2026-09-02T12:00:00Z" in header
            assert "b" * 40 in header
            assert "2026-09-01T12:00:00Z" not in header


class TestProvenanceSchemaVersions:
    """A pre-v2 manifest fails loudly, then self-heals via a full regeneration."""

    @staticmethod
    def _write_v1(root: Path) -> None:
        output_dir = root / "diagrams" / "code_diagrams"
        output_dir.mkdir(parents=True)
        (output_dir / "provenance.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-09-01T12:00:00Z",
                    "source_commit": "a" * 40,
                    "source_digests": {"example.py": "deadbeef"},
                }
            ),
            encoding="utf-8",
        )

    def test_v1_layout_is_rejected(self, tmp_path: Path) -> None:
        self._write_v1(tmp_path)
        with pytest.raises(RuntimeError, match="no sources mapping"):
            generate_mod.load_provenance_manifest(tmp_path)

    def test_v1_layout_makes_every_target_stale(self, tmp_path: Path) -> None:
        """An unreadable manifest must not be mistaken for 'nothing to do'."""
        self._write_v1(tmp_path)
        (tmp_path / "example.py").write_bytes(b"def f():\n    return True\n")
        target = Target(source="example.py", function="f")
        assert generate_mod.select_stale_targets(
            project_root=tmp_path,
            targets=[target],
            output_dir=tmp_path / "diagrams" / "code_diagrams",
        ) == [target]

    def test_entry_missing_a_required_field_is_rejected(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        output_dir.mkdir(parents=True)
        (output_dir / "provenance.json").write_text(
            json.dumps({"schema_version": 2, "sources": {"example.py": {"digest": "deadbeef"}}}),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="must record"):
            generate_mod.load_provenance_manifest(tmp_path)


class TestSourceMarkerEdgeCases:
    """The paths a normal regeneration never takes.

    Marker insertion rewrites files that are under version control, so the
    interesting cases are the ones where it must *not* write, must not reformat,
    or must refuse outright. A silent write here shows up as unexplained diff
    noise in someone else's pull request.
    """

    @staticmethod
    def _result(
        tmp_path: Path,
        *,
        function: str = "mod.func",
        png: bool = True,
        generated_at: str = "2026-07-16T12:00:00Z",
        source_commit: str = "a" * 40,
    ) -> object:
        html_path = tmp_path / "artifacts" / f"{function.replace('.', '_')}.html"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text("<html></html>", encoding="utf-8")
        png_path = None
        if png:
            png_path = html_path.with_suffix(".png")
            png_path.write_bytes(b"\x89PNG")
        return RenderedTarget(
            target=Target(source="pkg/mod.py", function=function),
            html_path=html_path,
            png_path=png_path,
            generated_at=generated_at,
            source_commit=source_commit,
        )

    def test_reinserting_an_identical_block_writes_nothing(self, tmp_path: Path) -> None:
        """Idempotence is what keeps regeneration out of unrelated diffs."""
        source_path = tmp_path / "pkg" / "mod.py"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("import os\n\n\ndef func():\n    return os\n", encoding="utf-8")
        result = self._result(tmp_path)

        assert (
            _update_marker_file(source_path=source_path, results=[result], project_root=tmp_path)
            is True
        )
        after_first = source_path.read_text(encoding="utf-8")

        assert (
            _update_marker_file(source_path=source_path, results=[result], project_root=tmp_path)
            is False
        )
        assert source_path.read_text(encoding="utf-8") == after_first

    def test_upsert_skips_formatting_when_no_file_changed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No write means no ``ruff format`` invocation at all."""
        source_path = tmp_path / "pkg" / "mod.py"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("import os\n\n\ndef func():\n    return os\n", encoding="utf-8")
        result = self._result(tmp_path)

        # Stubbed for both runs: the real ruff would reformat the file after the
        # first insertion, so the second run would legitimately have something to
        # rewrite and the point being tested here would be lost.
        calls: list[object] = []
        monkeypatch.setattr(source_marker_mod, "_ruff_format", lambda *a, **k: calls.append((a, k)))

        upsert_markers([result], project_root=tmp_path)
        assert len(calls) == 1, "the first insertion should have triggered formatting"

        upsert_markers([result], project_root=tmp_path)
        assert len(calls) == 1, "ruff format ran again even though nothing was rewritten"

    def test_ruff_format_warns_and_returns_when_ruff_is_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing ruff degrades to a warning; generation still has to succeed."""
        monkeypatch.setitem(sys.modules, "ruff", None)
        real_import = builtins.__import__

        def _no_ruff(name: str, *args: object, **kwargs: object) -> object:
            if name == "ruff":
                raise ImportError("no ruff")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", _no_ruff)
        ran: list[object] = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))

        with pytest.warns(UserWarning, match="ruff is not installed"):
            _ruff_format([tmp_path / "pkg" / "mod.py"], project_root=tmp_path)

        assert ran == [], "ruff was invoked despite being unimportable"

    def test_results_disagreeing_on_the_source_commit_are_refused(self, tmp_path: Path) -> None:
        """One block records one commit; two would make the marker a lie."""
        source_path = tmp_path / "pkg" / "mod.py"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("import os\n", encoding="utf-8")
        results = [
            self._result(tmp_path, function="mod.one", source_commit="a" * 40),
            self._result(tmp_path, function="mod.two", source_commit="b" * 40),
        ]

        with pytest.raises(ValueError, match="one Git source commit"):
            _update_marker_file(source_path=source_path, results=results, project_root=tmp_path)

    def test_a_target_without_a_png_is_listed_without_one(self, tmp_path: Path) -> None:
        """HTML-only runs are normal (no Playwright), and must still mark up."""
        source_path = tmp_path / "pkg" / "mod.py"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("import os\n", encoding="utf-8")

        _update_marker_file(
            source_path=source_path,
            results=[self._result(tmp_path, png=False)],
            project_root=tmp_path,
        )
        marked = source_path.read_text(encoding="utf-8")

        assert "mod.func" in marked
        assert "(PNG:" not in marked

    def test_strip_all_markers_ignores_unmarked_and_missing_files(self, tmp_path: Path) -> None:
        """Only files actually carrying a marker may be rewritten."""
        for package in ("gco", "cli", "gco_mcp", "lambda"):
            (tmp_path / package).mkdir()
        unmarked = tmp_path / "gco" / "plain.py"
        unmarked.write_text("import os\n", encoding="utf-8")
        before = unmarked.read_text(encoding="utf-8")

        marked = tmp_path / "cli" / "marked.py"
        marked.write_text(
            f"import os\n\n# <{SENTINEL}> BEGIN - auto-inserted, do not edit\n# <{SENTINEL}> END\n",
            encoding="utf-8",
        )
        # A build directory that must be skipped even though it carries a marker.
        build = tmp_path / "lambda" / "helm-installer-build"
        build.mkdir()
        skipped = build / "copy.py"
        skipped.write_text(marked.read_text(encoding="utf-8"), encoding="utf-8")

        modified = strip_all_markers(tmp_path)

        assert modified == 1, "expected exactly the one eligible marked file to change"
        assert unmarked.read_text(encoding="utf-8") == before
        assert SENTINEL not in marked.read_text(encoding="utf-8")
        assert SENTINEL in skipped.read_text(encoding="utf-8"), "a build copy was rewritten"

    def test_a_bare_sentinel_mention_is_not_treated_as_a_block(self, tmp_path: Path) -> None:
        """Only a complete BEGIN/END block is strippable.

        The sentinel string also appears in prose — this module's own docstrings
        mention it — so seeing the word is not sufficient reason to rewrite a
        file. Stripping must be a no-op when there is no delimited block.
        """
        for package in ("gco", "cli", "gco_mcp", "lambda"):
            (tmp_path / package).mkdir()
        mentions = tmp_path / "gco" / "prose.py"
        mentions.write_text(
            f'"""A module that merely talks about {SENTINEL} blocks."""\n\nimport os\n',
            encoding="utf-8",
        )
        before = mentions.read_text(encoding="utf-8")

        assert strip_all_markers(tmp_path) == 0
        assert mentions.read_text(encoding="utf-8") == before


class TestMarkerInsertionPoint:
    """Where the block lands, for files that are all prelude or have none."""

    def test_a_file_that_is_only_imports_appends_at_the_end(self, tmp_path: Path) -> None:
        """The import walk can finish without ever hitting a non-import node."""
        source_path = tmp_path / "pkg" / "mod.py"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("import os\nimport sys\n", encoding="utf-8")

        _update_marker_file(
            source_path=source_path,
            results=[TestSourceMarkerEdgeCases._result(tmp_path)],
            project_root=tmp_path,
        )
        marked = source_path.read_text(encoding="utf-8")

        assert marked.startswith("import os\nimport sys\n")
        assert SENTINEL in marked

    def test_a_prelude_with_no_trailing_newline_still_marks_up(self, tmp_path: Path) -> None:
        """A file whose last prelude line lacks a newline must not lose the block."""
        source_path = tmp_path / "pkg" / "mod.py"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("import os", encoding="utf-8")

        _update_marker_file(
            source_path=source_path,
            results=[TestSourceMarkerEdgeCases._result(tmp_path)],
            project_root=tmp_path,
        )

        assert SENTINEL in source_path.read_text(encoding="utf-8")

    def test_a_file_with_no_prelude_marks_at_the_top(self, tmp_path: Path) -> None:
        source_path = tmp_path / "pkg" / "mod.py"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("x = 1\n", encoding="utf-8")

        _update_marker_file(
            source_path=source_path,
            results=[TestSourceMarkerEdgeCases._result(tmp_path)],
            project_root=tmp_path,
        )

        assert SENTINEL in source_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Renderer lifecycle and the Playwright wrapper (no browser is ever launched)
# ---------------------------------------------------------------------------

# ``generate.py`` imports the renderer as a real package module, so ``main()``
# resolves ``_make_png_renderer`` from *this* module object, not from the
# path-loaded ``renderer_mod`` the unit tests above use.
real_renderer_mod = importlib.import_module("diagrams.code_diagrams._renderer")
render_all = renderer_mod.render_all
prune_orphaned_artifacts = renderer_mod.prune_orphaned_artifacts
write_readme = renderer_mod.write_readme
_require_pyflowchart = renderer_mod._require_pyflowchart
_make_png_renderer = renderer_mod._make_png_renderer
_PlaywrightRenderer = renderer_mod._PlaywrightRenderer

GENERATED_AT = "2026-08-30T12:00:00Z"
SOURCE_COMMIT = "c" * 40


class _FakePngRenderer:
    """Stand-in for ``_PlaywrightRenderer`` that writes a PNG without Chromium."""

    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.rendered: list[tuple[Path, Path]] = []
        self.closed = False

    def render(self, *, html_path: Path, png_path: Path) -> bool:
        self.rendered.append((html_path, png_path))
        if self.succeed:
            png_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return self.succeed

    def close(self) -> None:
        self.closed = True


class _FakeLocator:
    """Minimal ``playwright.sync_api.Locator`` double."""

    def __init__(
        self,
        box: dict[str, float] | None = None,
        *,
        screenshot_error: Exception | None = None,
    ) -> None:
        self.box = box
        self.screenshot_error = screenshot_error
        self.evaluate_calls: list[object] = []
        self.screenshots: list[str] = []

    def bounding_box(self) -> dict[str, float] | None:
        return self.box

    def evaluate(self, script: str, arg: object) -> None:
        self.evaluate_calls.append(arg)

    def screenshot(self, *, path: str) -> None:
        if self.screenshot_error is not None:
            raise self.screenshot_error
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.screenshots.append(path)


class _FakePage:
    """Minimal ``playwright.sync_api.Page`` double."""

    def __init__(
        self,
        *,
        svg: _FakeLocator,
        artifact: _FakeLocator,
        wait_error: Exception | None = None,
    ) -> None:
        self.locators = {"#canvas svg": svg, "#generated-artifact": artifact}
        self.wait_error = wait_error
        self.visited: list[str] = []
        self.waits: list[int] = []
        self.closed = False

    def goto(self, url: str) -> None:
        self.visited.append(url)

    def wait_for_function(self, expression: str, *, timeout: int) -> None:
        if self.wait_error is not None:
            raise self.wait_error

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)

    def locator(self, selector: str) -> _FakeLocator:
        return self.locators[selector]

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    """Minimal ``playwright.sync_api.Browser`` double."""

    def __init__(self, page: _FakePage, *, close_error: Exception | None = None) -> None:
        self.page = page
        self.close_error = close_error
        self.closed = False

    def new_page(self, **_kwargs: object) -> _FakePage:
        return self.page

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class _FakeDriver:
    """Minimal ``sync_playwright().start()`` handle double."""

    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _renderer_with(browser: _FakeBrowser, driver: _FakeDriver | None = None) -> object:
    """Build a ``_PlaywrightRenderer`` around fakes without running ``__init__``."""
    renderer = object.__new__(_PlaywrightRenderer)
    renderer._browser = browser
    renderer._pw = driver if driver is not None else _FakeDriver()
    return renderer


def _write_charted_source(tmp_path: Path) -> Target:
    (tmp_path / "example.py").write_text(
        "def f(flag):\n    if flag:\n        return 1\n    return 2\n",
        encoding="utf-8",
    )
    return Target(source="example.py", function="f", title="Example flow")


class TestRequirePyflowchart:
    def test_installed_pyflowchart_passes_silently(self) -> None:
        """The guard is a no-op when the optional dependency is importable."""
        _require_pyflowchart()

    def test_missing_pyflowchart_exits_with_an_install_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An absent pyflowchart is a clean SystemExit naming the extra to install."""
        monkeypatch.setitem(sys.modules, "pyflowchart", None)
        with pytest.raises(SystemExit) as excinfo:
            _require_pyflowchart()
        message = str(excinfo.value.code)
        assert "pyflowchart is not installed" in message
        assert "pip install -e '.[diagrams]'" in message


class TestAnnotateGeneratedHtml:
    def test_unrecognised_template_is_refused(self) -> None:
        """A pyflowchart template change must fail loudly, not silently drop the stamp."""
        with pytest.raises(RuntimeError, match="no longer matches the annotator"):
            _annotate_generated_html(
                "<html><body></body></html>",
                generated_at=GENERATED_AT,
                source_commit=SOURCE_COMMIT,
            )


class TestRenderOneWithRenderer:
    """``_render_one`` with a PNG renderer present (real pyflowchart, fake Playwright)."""

    def test_successful_png_render_is_recorded(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A successful screenshot yields a PNG path and a normalised, titled HTML page."""
        target = _write_charted_source(tmp_path)
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        renderer = _FakePngRenderer()

        result = _render_one(
            target=target,
            project_root=tmp_path,
            output_dir=output_dir,
            renderer=renderer,
            generated_at=GENERATED_AT,
            source_commit=SOURCE_COMMIT,
        )

        assert result.png_path == output_dir / "example.f.png"
        assert result.png_path.is_file()
        assert renderer.rendered == [(output_dir / "example.f.html", output_dir / "example.f.png")]
        html = result.html_path.read_text(encoding="utf-8")
        assert "<title>Example flow</title>" in html
        assert f'<meta name="gco-source-commit" content="{SOURCE_COMMIT}">' in html
        assert all(line == line.rstrip() for line in html.splitlines())
        assert html.endswith("\n")
        out = capsys.readouterr().out
        assert "✓ PNG   diagrams/code_diagrams/example.f.png" in out
        assert "removed stale" not in out

    def test_failed_png_render_degrades_to_html_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A renderer that returns ``False`` leaves HTML in place and records no PNG."""
        target = _write_charted_source(tmp_path)
        output_dir = tmp_path / "diagrams" / "code_diagrams"

        result = _render_one(
            target=target,
            project_root=tmp_path,
            output_dir=output_dir,
            renderer=_FakePngRenderer(succeed=False),
            generated_at=GENERATED_AT,
            source_commit=SOURCE_COMMIT,
        )

        assert result.png_path is None
        assert result.html_path.is_file()
        assert not (output_dir / "example.f.png").exists()
        assert "✓ PNG" not in capsys.readouterr().out


class TestRenderAll:
    """The batch owns exactly one renderer and always closes it."""

    def test_html_only_batch_never_creates_a_renderer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``render_png=False`` must not even attempt Playwright start-up."""
        target = _write_charted_source(tmp_path)
        monkeypatch.setattr(
            renderer_mod,
            "_make_png_renderer",
            lambda: pytest.fail("no renderer may be created for an HTML-only batch"),
        )

        results = render_all(
            targets=[target],
            project_root=tmp_path,
            output_dir=tmp_path / "diagrams" / "code_diagrams",
            render_png=False,
            generated_at=GENERATED_AT,
            source_commit=SOURCE_COMMIT,
        )

        assert [result.png_path for result in results] == [None]
        assert results[0].html_path.is_file()
        assert results[0].generated_at == GENERATED_AT
        assert results[0].source_commit == SOURCE_COMMIT

    def test_png_batch_shares_one_renderer_and_closes_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every target in the batch reuses the same renderer, closed once at the end."""
        (tmp_path / "example.py").write_text(
            "def f():\n    return 1\n\n\ndef g():\n    return 2\n", encoding="utf-8"
        )
        fake = _FakePngRenderer()
        monkeypatch.setattr(renderer_mod, "_make_png_renderer", lambda: fake)

        results = render_all(
            targets=[
                Target(source="example.py", function="f"),
                Target(source="example.py", function="g"),
            ],
            project_root=tmp_path,
            output_dir=tmp_path / "diagrams" / "code_diagrams",
            render_png=True,
            generated_at=GENERATED_AT,
            source_commit=SOURCE_COMMIT,
        )

        assert [result.png_path.name for result in results] == ["example.f.png", "example.g.png"]
        assert all(result.png_path.is_file() for result in results)
        assert len(fake.rendered) == 2
        assert fake.closed

    def test_renderer_is_closed_even_when_a_target_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mid-batch exception still shuts the browser down."""
        fake = _FakePngRenderer()
        monkeypatch.setattr(renderer_mod, "_make_png_renderer", lambda: fake)

        with pytest.raises(FileNotFoundError):
            render_all(
                targets=[Target(source="missing.py", function="f")],
                project_root=tmp_path,
                output_dir=tmp_path / "diagrams" / "code_diagrams",
                render_png=True,
                generated_at=GENERATED_AT,
                source_commit=SOURCE_COMMIT,
            )

        assert fake.closed
        assert fake.rendered == []


class TestPruneOrphanedArtifacts:
    def test_untracked_artifacts_and_their_empty_directories_are_removed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Only ``.html``/``.png`` files without a target go; everything else survives.

        ``__pycache__`` is never removed even when empty, and directories that
        still hold files are left alone.
        """
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        keep = Target(source="cli/alpha.py", function="f")
        stem = _output_stem_for(keep, output_dir=output_dir)
        stem.parent.mkdir(parents=True)
        kept_html = stem.parent / f"{stem.name}.html"
        kept_png = stem.parent / f"{stem.name}.png"
        kept_html.write_text("<html></html>", encoding="utf-8")
        kept_png.write_bytes(b"\x89PNG")

        orphan_dir = output_dir / "gco" / "old"
        orphan_dir.mkdir(parents=True)
        orphan_html = orphan_dir / "old.f.html"
        orphan_png = orphan_dir / "old.f.png"
        orphan_html.write_text("<html></html>", encoding="utf-8")
        orphan_png.write_bytes(b"\x89PNG")

        readme = output_dir / "README.md"
        readme.write_text("# index\n", encoding="utf-8")
        manifest = output_dir / "provenance.json"
        manifest.write_text("{}\n", encoding="utf-8")
        pycache = output_dir / "__pycache__"
        pycache.mkdir()

        removed = prune_orphaned_artifacts(targets=[keep], output_dir=output_dir)

        assert sorted(removed) == [orphan_html, orphan_png]
        assert kept_html.is_file() and kept_png.is_file()
        assert readme.is_file() and manifest.is_file()
        assert not orphan_dir.exists()
        assert not (output_dir / "gco").exists()
        assert pycache.is_dir()
        out = capsys.readouterr().out
        assert "removed obsolete artifact gco/old/old.f.html" in out
        assert "removed obsolete artifact gco/old/old.f.png" in out

    def test_current_catalogue_is_left_untouched(self, tmp_path: Path) -> None:
        """A catalogue with no orphans removes nothing."""
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        keep = Target(source="cli/alpha.py", function="f")
        stem = _output_stem_for(keep, output_dir=output_dir)
        stem.parent.mkdir(parents=True)
        (stem.parent / f"{stem.name}.html").write_text("<html></html>", encoding="utf-8")

        assert prune_orphaned_artifacts(targets=[keep], output_dir=output_dir) == []
        assert (stem.parent / f"{stem.name}.html").is_file()


class TestWriteReadme:
    def test_index_is_written_into_the_output_directory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``write_readme`` renders the grouped index to ``<output_dir>/README.md``."""
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        output_dir.mkdir(parents=True)

        write_readme([_make_rendered(tmp_path, "cli/alpha.py", "f")], output_dir=output_dir)

        content = (output_dir / "README.md").read_text(encoding="utf-8")
        assert content.startswith("# GCO Code Flowcharts")
        assert "[HTML](./cli/alpha.f.html) · [PNG](./cli/alpha.f.png)" in content
        assert f"Wrote {output_dir / 'README.md'}" in capsys.readouterr().out


class TestPlaywrightRenderer:
    """``render``/``close`` driven through fakes; Chromium is never started."""

    @staticmethod
    def _paths(tmp_path: Path) -> tuple[Path, Path]:
        html_path = tmp_path / "example.f.html"
        html_path.write_text("<html></html>", encoding="utf-8")
        return html_path, tmp_path / "example.f.png"

    def test_small_diagram_is_screenshotted_at_native_size(self, tmp_path: Path) -> None:
        """A diagram within Chromium's limits is not resized before the screenshot."""
        svg = _FakeLocator({"x": 0, "y": 0, "width": 1_200, "height": 800})
        artifact = _FakeLocator()
        page = _FakePage(svg=svg, artifact=artifact)
        renderer = _renderer_with(_FakeBrowser(page))
        html_path, png_path = self._paths(tmp_path)

        assert renderer.render(html_path=html_path, png_path=png_path) is True

        assert page.visited == [html_path.absolute().as_uri()]
        assert svg.evaluate_calls == []
        assert page.waits == [500]
        assert artifact.screenshots == [str(png_path)]
        assert png_path.is_file()
        assert page.closed

    def test_missing_bounding_box_falls_back_to_native_size(self, tmp_path: Path) -> None:
        """No bounding box (detached SVG) means no rescale, but still a screenshot."""
        svg = _FakeLocator(None)
        artifact = _FakeLocator()
        page = _FakePage(svg=svg, artifact=artifact)
        renderer = _renderer_with(_FakeBrowser(page))
        html_path, png_path = self._paths(tmp_path)

        assert renderer.render(html_path=html_path, png_path=png_path) is True
        assert svg.evaluate_calls == []
        assert page.waits == [500]
        assert png_path.is_file()
        assert page.closed

    def test_oversized_diagram_is_shrunk_before_the_screenshot(self, tmp_path: Path) -> None:
        """Diagrams past the 8k CSS-pixel edge are resized in-page and given a settle beat."""
        svg = _FakeLocator({"x": 0, "y": 0, "width": 20_000, "height": 1_000})
        artifact = _FakeLocator()
        page = _FakePage(svg=svg, artifact=artifact)
        renderer = _renderer_with(_FakeBrowser(page))
        html_path, png_path = self._paths(tmp_path)

        assert renderer.render(html_path=html_path, png_path=png_path) is True

        assert svg.evaluate_calls == [
            {
                "sourceWidth": 20_000,
                "sourceHeight": 1_000,
                "width": pytest.approx(8_000),
                "height": pytest.approx(400),
            }
        ]
        assert page.waits == [500, 100]
        assert png_path.is_file()
        assert page.closed

    def test_timeout_degrades_to_html_only_with_a_warning(self, tmp_path: Path) -> None:
        """flowchart.js never rendering is a warning and ``False``, not an abort."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        artifact = _FakeLocator()
        page = _FakePage(
            svg=_FakeLocator(),
            artifact=artifact,
            wait_error=PlaywrightTimeout("no svg after 30s"),
        )
        renderer = _renderer_with(_FakeBrowser(page))
        html_path, png_path = self._paths(tmp_path)

        with pytest.warns(UserWarning, match="timed out rendering .*example.f.html"):
            assert renderer.render(html_path=html_path, png_path=png_path) is False

        assert artifact.screenshots == []
        assert not png_path.exists()
        assert page.closed

    def test_screenshot_failure_degrades_to_html_only_with_a_warning(self, tmp_path: Path) -> None:
        """Chromium refusing the capture (diagram too large) is a warning and ``False``."""
        from playwright.sync_api import Error as PlaywrightError

        artifact = _FakeLocator(screenshot_error=PlaywrightError("Unable to capture screenshot"))
        page = _FakePage(
            svg=_FakeLocator({"x": 0, "y": 0, "width": 10, "height": 10}), artifact=artifact
        )
        renderer = _renderer_with(_FakeBrowser(page))
        html_path, png_path = self._paths(tmp_path)

        with pytest.warns(UserWarning, match="could not screenshot .*max size.*Unable to capture"):
            assert renderer.render(html_path=html_path, png_path=png_path) is False

        assert not png_path.exists()
        assert page.closed

    def test_close_shuts_the_browser_then_the_driver(self) -> None:
        """A normal close releases both the browser and the Playwright driver."""
        driver = _FakeDriver()
        browser = _FakeBrowser(_FakePage(svg=_FakeLocator(), artifact=_FakeLocator()))
        renderer = _renderer_with(browser, driver)

        renderer.close()

        assert browser.closed
        assert driver.stopped

    def test_close_stops_the_driver_even_if_the_browser_refuses_to_close(self) -> None:
        """The driver process must not be leaked when ``browser.close()`` raises."""
        driver = _FakeDriver()
        browser = _FakeBrowser(
            _FakePage(svg=_FakeLocator(), artifact=_FakeLocator()),
            close_error=RuntimeError("browser gone"),
        )
        renderer = _renderer_with(browser, driver)

        with pytest.raises(RuntimeError, match="browser gone"):
            renderer.close()

        assert driver.stopped


class TestMakePngRenderer:
    def test_missing_playwright_warns_and_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An un-importable Playwright downgrades to HTML-only with an install hint."""

        def _no_playwright(self: object) -> None:
            raise ImportError("No module named 'playwright'")

        monkeypatch.setattr(_PlaywrightRenderer, "__init__", _no_playwright)

        with pytest.warns(UserWarning, match="Playwright not installed"):
            assert _make_png_renderer() is None

    def test_available_playwright_returns_the_renderer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the wrapper constructs cleanly it is handed back for the batch."""
        monkeypatch.setattr(_PlaywrightRenderer, "__init__", lambda self: None)

        renderer = _make_png_renderer()

        assert isinstance(renderer, _PlaywrightRenderer)


# ---------------------------------------------------------------------------
# generate.py helpers not reached by the earlier suites
# ---------------------------------------------------------------------------


class TestTargetHint:
    def test_no_sources_or_too_many_sources_yield_no_hint(self) -> None:
        """The narrow ``--target`` hint is only offered for a handful of files."""
        assert generate_mod._target_hint([]) == ""
        assert generate_mod._target_hint([f"cli/m{i}.py" for i in range(5)]) == ""

    def test_a_handful_of_sources_get_an_explicit_target_list(self) -> None:
        """Up to four files get one ``--target`` placeholder each."""
        hint = generate_mod._target_hint(["cli/a.py", "gco/b.py"])
        assert "--target cli/a.py:<function> --target gco/b.py:<function>" in hint


class TestLoadProvenanceManifestErrors:
    def test_malformed_json_is_reported_as_unreadable(self, tmp_path: Path) -> None:
        """A corrupt manifest is distinguished from a missing one and keeps its cause."""
        path = generate_mod.provenance_manifest_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(
            RuntimeError, match=r"unreadable diagrams/code_diagrams/provenance\.json"
        ) as excinfo:
            generate_mod.load_provenance_manifest(tmp_path)

        assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)
        assert "To fix:" in str(excinfo.value)


class TestSelectStaleTargetsSharedSource:
    def test_two_targets_in_one_source_share_its_digest(self, tmp_path: Path) -> None:
        """One source charted twice is hashed once and both targets move together."""
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        output_dir.mkdir(parents=True)
        source = tmp_path / "example.py"
        source.write_bytes(b"def f():\n    return 1\n\n\ndef g():\n    return 2\n")
        targets = [
            Target(source="example.py", function="f"),
            Target(source="example.py", function="g"),
        ]
        for target in targets:
            stem = _output_stem_for(target, output_dir=output_dir)
            stem.with_name(f"{stem.name}.html").write_text("<html></html>", encoding="utf-8")
            stem.with_name(f"{stem.name}.png").write_bytes(b"\x89PNG")
        generate_mod.write_provenance_manifest(
            project_root=tmp_path,
            output_dir=output_dir,
            regenerated_targets=targets,
            generated_at=GENERATED_AT,
            source_commit=SOURCE_COMMIT,
            catalog=targets,
        )

        assert (
            generate_mod.select_stale_targets(
                project_root=tmp_path, targets=targets, output_dir=output_dir
            )
            == []
        )

        source.write_bytes(b"def f():\n    return 0\n\n\ndef g():\n    return 2\n")
        assert (
            generate_mod.select_stale_targets(
                project_root=tmp_path, targets=targets, output_dir=output_dir
            )
            == targets
        )


class TestSourceCommitVerificationGitFailures:
    def test_unresolvable_commit_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A commit Git cannot find is reported with Git's own diagnostic."""

        def run(args: list[str], **_kwargs: object) -> SimpleNamespace:
            assert args[1:3] == ["cat-file", "-t"]
            return SimpleNamespace(
                returncode=128, stdout=b"", stderr=b"fatal: Not a valid object name\n"
            )

        monkeypatch.setattr(generate_mod.subprocess, "run", run)

        with pytest.raises(RuntimeError, match="does not resolve: fatal: Not a valid object name"):
            _verify_targets_match_source_commit(
                project_root=tmp_path,
                targets=[Target(source="example.py", function="f")],
                source_commit="a" * 40,
            )

    def test_source_missing_from_the_commit_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A charted file absent from the commit cannot have been generated from it."""
        (tmp_path / "example.py").write_bytes(b"def f():\n    return True\n")

        def run(args: list[str], **_kwargs: object) -> SimpleNamespace:
            if args[1:3] == ["cat-file", "-t"]:
                return SimpleNamespace(returncode=0, stdout=b"commit\n", stderr=b"")
            assert args[1] == "show"
            return SimpleNamespace(
                returncode=128,
                stdout=b"",
                stderr=b"fatal: path 'example.py' does not exist in 'aaaa'\n",
            )

        monkeypatch.setattr(generate_mod.subprocess, "run", run)

        with pytest.raises(
            RuntimeError,
            match="cannot read example.py from GCO_DIAGRAM_SOURCE_COMMIT .*does not exist",
        ):
            _verify_targets_match_source_commit(
                project_root=tmp_path,
                targets=[Target(source="example.py", function="f")],
                source_commit="a" * 40,
            )


class TestProvenanceManifestSyncDetail:
    """The out-of-sync message names only the direction that is actually wrong."""

    @staticmethod
    def _manifest_for(tmp_path: Path, sources: list[str]) -> list[Target]:
        targets = []
        for index, name in enumerate(sources):
            (tmp_path / name).write_bytes(f"def f{index}():\n    return True\n".encode())
            targets.append(Target(source=name, function=f"f{index}"))
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        output_dir.mkdir(parents=True, exist_ok=True)
        generate_mod.write_provenance_manifest(
            project_root=tmp_path,
            output_dir=output_dir,
            regenerated_targets=targets,
            generated_at=GENERATED_AT,
            source_commit=SOURCE_COMMIT,
            catalog=targets,
        )
        return targets

    def test_only_retired_sources_are_named(self, tmp_path: Path) -> None:
        """A manifest entry with no target is reported as retired, nothing else."""
        example, _other = self._manifest_for(tmp_path, ["example.py", "other.py"])

        with pytest.raises(RuntimeError) as excinfo:
            generate_mod.verify_targets_match_provenance_manifest(
                project_root=tmp_path, targets=[example]
            )

        message = str(excinfo.value)
        assert "recorded sources no longer in _targets.py: ['other.py']" in message
        assert "newly charted" not in message

    def test_only_newly_charted_sources_are_named(self, tmp_path: Path) -> None:
        """A target with no manifest entry is reported as newly charted, nothing else."""
        (example,) = self._manifest_for(tmp_path, ["example.py"])
        (tmp_path / "other.py").write_bytes(b"def g():\n    return True\n")

        with pytest.raises(RuntimeError) as excinfo:
            generate_mod.verify_targets_match_provenance_manifest(
                project_root=tmp_path,
                targets=[example, Target(source="other.py", function="g")],
            )

        message = str(excinfo.value)
        assert "newly charted sources with no recorded provenance: ['other.py']" in message
        assert "no longer in _targets.py" not in message


class TestPruneRetiredMarkersBareMention:
    def test_a_bare_sentinel_mention_is_not_rewritten(self, tmp_path: Path) -> None:
        """Prose that merely names the sentinel is not a marker block and is left alone."""
        path = tmp_path / "cli" / "prose.py"
        path.parent.mkdir(parents=True)
        path.write_text(
            f'"""Talks about {SENTINEL} blocks."""\n\nimport os\n',
            encoding="utf-8",
        )
        before = path.read_bytes()

        assert generate_mod.prune_retired_markers(tmp_path, charted=set()) == 0
        assert path.read_bytes() == before


class TestCatalogReadmeEntries:
    def test_untouched_targets_are_reconstructed_from_the_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fresh results are used as-is; the rest come from provenance plus disk state."""
        output_dir = tmp_path / "diagrams" / "code_diagrams"
        output_dir.mkdir(parents=True)
        fresh_target = Target(source="cli/a.py", function="f")
        with_png = Target(source="gco/b.py", function="g")
        without_png = Target(source="lambda/c/handler.py", function="handler")
        catalogue = [fresh_target, with_png, without_png]
        for target in catalogue:
            path = tmp_path / target.source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"def {target.function}():\n    return True\n".encode())
        monkeypatch.setattr(generate_mod, "TARGETS", catalogue)
        generate_mod.write_provenance_manifest(
            project_root=tmp_path,
            output_dir=output_dir,
            regenerated_targets=[with_png, without_png],
            generated_at="2026-09-01T12:00:00Z",
            source_commit="a" * 40,
            catalog=catalogue,
        )
        stem_b = _output_stem_for(with_png, output_dir=output_dir)
        stem_b.parent.mkdir(parents=True)
        stem_b.with_name(f"{stem_b.name}.html").write_text("<html></html>", encoding="utf-8")
        stem_b.with_name(f"{stem_b.name}.png").write_bytes(b"\x89PNG")
        stem_c = _output_stem_for(without_png, output_dir=output_dir)
        stem_c.parent.mkdir(parents=True)
        stem_c.with_name(f"{stem_c.name}.html").write_text("<html></html>", encoding="utf-8")
        fresh = _make_rendered(tmp_path, "cli/a.py", "f")

        entries = generate_mod._catalog_readme_entries(
            project_root=tmp_path, output_dir=output_dir, results=[fresh]
        )

        assert [entry.target for entry in entries] == catalogue
        assert entries[0] is fresh
        assert entries[1].html_path == stem_b.with_name(f"{stem_b.name}.html")
        assert entries[1].png_path == stem_b.with_name(f"{stem_b.name}.png")
        assert entries[1].generated_at == "2026-09-01T12:00:00Z"
        assert entries[1].source_commit == "a" * 40
        assert entries[2].html_path == stem_c.with_name(f"{stem_c.name}.html")
        assert entries[2].png_path is None


class TestSyncSharedLambdaCopiesMissingConsumer:
    def test_a_copy_whose_lambda_directory_is_absent_is_skipped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A canonical source with no consumer directory creates nothing."""
        canonical, copies = next(iter(LAMBDA_SHARED_SOURCE_TARGETS.items()))
        source = tmp_path / canonical
        source.parent.mkdir(parents=True)
        source.write_bytes(b"# canonical\n")

        _sync_shared_lambda_copies(tmp_path)

        for copy in copies:
            assert not (tmp_path / copy).exists()
        assert "Synced shared copy" not in capsys.readouterr().out


class TestFilterTargets:
    _A = Target(source="cli/a.py", function="f")
    _B = Target(source="gco/b.py", function="g")
    _C = Target(source="lambda/c.py", function="h")

    def test_requested_targets_are_returned_in_catalogue_order(self) -> None:
        """Selection follows the catalogue, not the order flags were given in."""
        selected = generate_mod._filter_targets(
            [self._A, self._B, self._C], ["lambda/c.py:h", "cli/a.py:f"]
        )
        assert selected == [self._A, self._C]

    def test_no_request_selects_a_copy_of_the_whole_catalogue(self) -> None:
        """The default is every target, as a fresh list the caller may mutate."""
        catalogue = [self._A, self._B]
        selected = generate_mod._filter_targets(catalogue, None)
        assert selected == catalogue
        assert selected is not catalogue

    def test_unknown_targets_abort(self) -> None:
        """Any unknown ``PATH:FUNC`` aborts with every unknown name listed."""
        with pytest.raises(
            SystemExit, match=r"Unknown target\(s\): \['cli/a.py:nope', 'zzz.py:f'\]"
        ):
            generate_mod._filter_targets([self._A], ["cli/a.py:nope", "zzz.py:f", "cli/a.py:f"])


# ---------------------------------------------------------------------------
# main(): the CLI end to end against a throwaway repository
# ---------------------------------------------------------------------------


class _FakeRepo:
    """A throwaway project layout that ``main()`` can regenerate end to end.

    ``main()`` derives both the project root and the output directory from the
    generator module's ``__file__``, so that global is pointed into ``tmp_path``
    and the catalogue is swapped for two tiny charted sources. Git and ruff are
    faked at the ``subprocess.run`` seam: ``git cat-file`` resolves the
    configured commit, ``git show`` serves the bytes captured when the repo was
    created (the "committed" state), and ``ruff format`` is recorded, not run.
    PNG rendering goes through an injected :class:`_FakePngRenderer`; when none
    is injected the run behaves as if Playwright were unavailable.
    """

    ALPHA = Target(source="cli/alpha.py", function="f")
    BETA = Target(source="lambda/beta/handler.py", function="handler", title="Beta handler")
    SOURCES = {
        "cli/alpha.py": (
            "import os\n\n\ndef f(flag):\n    if flag:\n"
            "        return os.getcwd()\n    return None\n"
        ),
        "lambda/beta/handler.py": (
            '"""Beta Lambda."""\n\n\ndef handler(event):\n'
            "    for item in event:\n        print(item)\n    return True\n"
        ),
    }

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.root = tmp_path / "repo"
        self.output_dir = self.root / "diagrams" / "code_diagrams"
        self.output_dir.mkdir(parents=True)
        for relative, body in self.SOURCES.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        self.committed = {rel: (self.root / rel).read_bytes() for rel in self.SOURCES}
        self.git_calls: list[list[str]] = []
        self.ruff_calls: list[list[str]] = []
        self.png_renderer: _FakePngRenderer | None = None
        self.png_renderer_requested = False
        monkeypatch.setattr(generate_mod, "__file__", str(self.output_dir / "generate.py"))
        monkeypatch.setattr(generate_mod, "TARGETS", [self.ALPHA, self.BETA])
        monkeypatch.setattr(subprocess, "run", self._fake_run)
        monkeypatch.setattr(real_renderer_mod, "_make_png_renderer", self._make_png_renderer)
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "1788091200")  # 2026-08-30T12:00:00Z
        monkeypatch.setenv("GCO_DIAGRAM_SOURCE_COMMIT", SOURCE_COMMIT)

    def _fake_run(self, args: list[str], **_kwargs: object) -> SimpleNamespace:
        if args[:2] == ["git", "cat-file"]:
            self.git_calls.append(args)
            return SimpleNamespace(returncode=0, stdout=b"commit\n", stderr=b"")
        if args[:2] == ["git", "show"]:
            self.git_calls.append(args)
            relative = args[2].split(":", 1)[1]
            return SimpleNamespace(returncode=0, stdout=self.committed[relative], stderr=b"")
        if args[1:4] == ["-m", "ruff", "format"]:
            self.ruff_calls.append(args)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected subprocess: {args}")

    def _make_png_renderer(self) -> _FakePngRenderer | None:
        self.png_renderer_requested = True
        return self.png_renderer

    def commit(self, relative: str, body: str) -> None:
        """Rewrite a source and treat the new bytes as the committed state."""
        (self.root / relative).write_text(body, encoding="utf-8")
        self.committed[relative] = body.encode()

    def artifact(self, target: Target, suffix: str) -> Path:
        stem = _output_stem_for(target, output_dir=self.output_dir)
        return stem.parent / f"{stem.name}.{suffix}"

    def manifest(self) -> dict[str, dict[str, str]]:
        raw = json.loads((self.output_dir / "provenance.json").read_text(encoding="utf-8"))
        assert raw["schema_version"] == 2
        return raw["sources"]

    def readme(self) -> str:
        return (self.output_dir / "README.md").read_text(encoding="utf-8")

    def snapshot(self) -> dict[Path, bytes]:
        return {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def main(self, *argv: str) -> None:
        self.monkeypatch.setattr(sys, "argv", ["generate.py", *argv])
        generate_mod.main()


class TestMainCli:
    """``main()`` end to end: flags, incremental selection, and every output it owns."""

    def test_require_png_with_skip_png_is_a_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The two PNG flags contradict each other and are rejected by the parser."""
        repo = _FakeRepo(tmp_path, monkeypatch)

        with pytest.raises(SystemExit) as excinfo:
            repo.main("--require-png", "--skip-png")

        assert excinfo.value.code == 2
        assert "--require-png cannot be combined with --skip-png" in capsys.readouterr().err
        assert not (repo.output_dir / "provenance.json").exists()

    def test_strip_markers_only_strips_and_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--strip-markers`` needs no commit, touches no artifact, and consults no Git."""
        repo = _FakeRepo(tmp_path, monkeypatch)
        monkeypatch.delenv("GCO_DIAGRAM_SOURCE_COMMIT")
        alpha = repo.root / "cli" / "alpha.py"
        alpha.write_text(
            "import os\n\n"
            f"# <{SENTINEL}> BEGIN - auto-inserted, do not edit\n"
            "# Flowchart(s) generated from this file:\n"
            f"# <{SENTINEL}> END\n\n"
            "def f(flag):\n    return os.getcwd()\n",
            encoding="utf-8",
        )

        repo.main("--strip-markers")

        out = capsys.readouterr().out
        assert "🧹 Stripping pyflowchart markers from source files" in out
        assert "✅ Stripped markers from 1 file(s)." in out
        assert SENTINEL not in alpha.read_text(encoding="utf-8")
        assert repo.git_calls == []
        assert not repo.png_renderer_requested
        assert not (repo.output_dir / "provenance.json").exists()
        assert not (repo.output_dir / "README.md").exists()

    def test_canonical_run_writes_everything_and_a_rerun_is_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A full ``--require-png`` run produces HTML, PNG, markers, manifest, and index.

        Orphaned artifacts are pruned on the full-catalogue run, and running the
        generator again immediately afterwards changes no byte anywhere.
        """
        repo = _FakeRepo(tmp_path, monkeypatch)
        orphan = repo.output_dir / "gco" / "retired.f.html"
        orphan.parent.mkdir(parents=True)
        orphan.write_text("<html></html>", encoding="utf-8")
        repo.png_renderer = _FakePngRenderer()

        repo.main("--require-png")

        out = capsys.readouterr().out
        assert "Mode         : incremental" in out
        assert "Targets      : 2 of 2 selected" in out
        assert f"Generated at : {GENERATED_AT}" in out
        assert f"Source commit: {SOURCE_COMMIT}" in out
        assert "✅ Code flowchart generation complete!" in out
        assert ["git", "cat-file", "-t", SOURCE_COMMIT] in repo.git_calls
        for target in (repo.ALPHA, repo.BETA):
            html = repo.artifact(target, "html")
            png = repo.artifact(target, "png")
            assert html.is_file() and png.is_file()
            assert f'<meta name="gco-generated-at" content="{GENERATED_AT}">' in html.read_text(
                encoding="utf-8"
            )
            source = (repo.root / target.source).read_text(encoding="utf-8")
            assert f"# Generated at (UTC): {GENERATED_AT}" in source
            assert f"# Generated from Git commit: {SOURCE_COMMIT}" in source
            assert f"(PNG: ``{png.relative_to(repo.root)}``)" in source
        assert repo.png_renderer.closed
        assert len(repo.png_renderer.rendered) == 2
        assert not orphan.exists()
        assert not orphan.parent.exists()
        manifest = repo.manifest()
        assert set(manifest) == set(repo.SOURCES)
        for relative, entry in manifest.items():
            assert entry == {
                "digest": hashlib.sha256(repo.committed[relative]).hexdigest(),
                "generated_at": GENERATED_AT,
                "source_commit": SOURCE_COMMIT,
            }
        readme = repo.readme()
        assert "[HTML](./cli/alpha.f.html) · [PNG](./cli/alpha.f.png)" in readme
        assert "Beta handler &mdash; `lambda/beta/handler.py::handler`" in readme
        assert len(repo.ruff_calls) == 1
        assert set(repo.ruff_calls[0][5:]) == set(repo.SOURCES)

        before = repo.snapshot()
        repo.git_calls.clear()
        repo.png_renderer_requested = False
        repo.png_renderer = _FakePngRenderer()

        repo.main("--require-png")

        out = capsys.readouterr().out
        assert "Targets      : 0 of 2 selected" in out
        assert "✅ Every charted source is already current; nothing to re-render." in out
        assert repo.git_calls == []
        assert not repo.png_renderer_requested
        assert repo.png_renderer.rendered == []
        assert repo.snapshot() == before

    def test_incremental_rerun_restamps_only_the_changed_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """After one source changes, only its artifacts, marker, and manifest entry move."""
        repo = _FakeRepo(tmp_path, monkeypatch)
        repo.png_renderer = _FakePngRenderer()
        repo.main()
        capsys.readouterr()
        alpha_source = (repo.root / "cli" / "alpha.py").read_bytes()
        alpha_artifacts = {
            suffix: repo.artifact(repo.ALPHA, suffix).read_bytes() for suffix in ("html", "png")
        }
        new_body = '"""Beta Lambda."""\n\n\ndef handler(event):\n    return len(event)\n'
        repo.commit("lambda/beta/handler.py", new_body)
        monkeypatch.setenv("GCO_DIAGRAM_SOURCE_COMMIT", "d" * 40)
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "1788177600")  # 2026-08-31T12:00:00Z
        repo.png_renderer = _FakePngRenderer()

        repo.main()

        out = capsys.readouterr().out
        assert "Mode         : incremental" in out
        assert "Targets      : 1 of 2 selected" in out
        assert repo.png_renderer.rendered == [
            (repo.artifact(repo.BETA, "html"), repo.artifact(repo.BETA, "png"))
        ]
        assert (repo.root / "cli" / "alpha.py").read_bytes() == alpha_source
        assert {
            suffix: repo.artifact(repo.ALPHA, suffix).read_bytes() for suffix in ("html", "png")
        } == alpha_artifacts
        manifest = repo.manifest()
        assert manifest["cli/alpha.py"]["generated_at"] == GENERATED_AT
        assert manifest["cli/alpha.py"]["source_commit"] == SOURCE_COMMIT
        assert manifest["lambda/beta/handler.py"] == {
            "digest": hashlib.sha256(new_body.encode()).hexdigest(),
            "generated_at": "2026-08-31T12:00:00Z",
            "source_commit": "d" * 40,
        }
        beta_source = (repo.root / "lambda" / "beta" / "handler.py").read_text(encoding="utf-8")
        assert f"# Generated from Git commit: {'d' * 40}" in beta_source
        readme = repo.readme()
        assert "*Generated at (UTC): `2026-08-31T12:00:00Z`.*" in readme
        assert "[HTML](./cli/alpha.f.html) · [PNG](./cli/alpha.f.png)" in readme

    def test_skip_png_writes_html_only_and_removes_stale_pngs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--skip-png`` never asks for a renderer and leaves no mixed-age PNG behind."""
        repo = _FakeRepo(tmp_path, monkeypatch)
        stale = repo.artifact(repo.ALPHA, "png")
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"older-png")

        repo.main("--skip-png")

        out = capsys.readouterr().out
        assert not repo.png_renderer_requested
        assert "removed stale diagrams/code_diagrams/cli/alpha.f.png" in out
        assert not stale.exists()
        assert repo.artifact(repo.ALPHA, "html").is_file()
        assert repo.artifact(repo.BETA, "html").is_file()
        assert not repo.artifact(repo.BETA, "png").exists()
        assert "[PNG]" not in repo.readme()
        alpha_source = (repo.root / "cli" / "alpha.py").read_text(encoding="utf-8")
        assert SENTINEL in alpha_source
        assert "(PNG:" not in alpha_source

    @pytest.mark.parametrize("renderer", [None, _FakePngRenderer(succeed=False)])
    def test_require_png_aborts_before_recording_provenance_when_a_png_is_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        renderer: _FakePngRenderer | None,
    ) -> None:
        """Playwright unavailable or a failed capture fails a canonical run early.

        HTML is still written, but no marker, manifest, or index is produced, so
        the repository contract cannot record a run that produced no PNGs.
        """
        repo = _FakeRepo(tmp_path, monkeypatch)
        repo.png_renderer = renderer

        with pytest.raises(SystemExit) as excinfo:
            repo.main("--require-png")

        assert excinfo.value.code == (
            "Canonical generation requires every PNG; missing: "
            "['cli/alpha.py:f', 'lambda/beta/handler.py:handler']"
        )
        assert repo.png_renderer_requested
        assert repo.artifact(repo.ALPHA, "html").is_file()
        assert not repo.artifact(repo.ALPHA, "png").exists()
        assert not (repo.output_dir / "provenance.json").exists()
        assert not (repo.output_dir / "README.md").exists()
        assert SENTINEL not in (repo.root / "cli" / "alpha.py").read_text(encoding="utf-8")
        assert repo.ruff_calls == []

    def test_skip_marker_leaves_sources_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--skip-marker`` still writes artifacts, manifest, and index but no source."""
        repo = _FakeRepo(tmp_path, monkeypatch)

        repo.main("--skip-png", "--skip-marker")

        for relative, body in repo.committed.items():
            assert (repo.root / relative).read_bytes() == body
        assert repo.ruff_calls == []
        assert repo.artifact(repo.ALPHA, "html").is_file()
        assert set(repo.manifest()) == set(repo.SOURCES)
        assert "[HTML](./cli/alpha.f.html)" in repo.readme()

    def test_explicit_target_renders_only_that_target_without_pruning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--target`` bypasses staleness, skips pruning, and keeps the rest of the index."""
        repo = _FakeRepo(tmp_path, monkeypatch)
        repo.png_renderer = _FakePngRenderer()
        repo.main()
        capsys.readouterr()
        alpha_source = (repo.root / "cli" / "alpha.py").read_bytes()
        alpha_html = repo.artifact(repo.ALPHA, "html").read_bytes()
        orphan = repo.output_dir / "gco" / "retired.f.html"
        orphan.parent.mkdir(parents=True)
        orphan.write_text("<html></html>", encoding="utf-8")
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "1788177600")  # 2026-08-31T12:00:00Z
        repo.png_renderer = _FakePngRenderer()

        repo.main("--target", "lambda/beta/handler.py:handler")

        out = capsys.readouterr().out
        assert "Mode         : full" in out
        assert "Targets      : 1 of 1 selected" in out
        assert repo.png_renderer.rendered == [
            (repo.artifact(repo.BETA, "html"), repo.artifact(repo.BETA, "png"))
        ]
        assert orphan.is_file()
        assert (repo.root / "cli" / "alpha.py").read_bytes() == alpha_source
        assert repo.artifact(repo.ALPHA, "html").read_bytes() == alpha_html
        manifest = repo.manifest()
        assert manifest["cli/alpha.py"]["generated_at"] == GENERATED_AT
        assert manifest["lambda/beta/handler.py"]["generated_at"] == "2026-08-31T12:00:00Z"
        readme = repo.readme()
        assert "*Generated at (UTC): `2026-08-31T12:00:00Z`.*" in readme
        assert "[HTML](./cli/alpha.f.html) · [PNG](./cli/alpha.f.png)" in readme
        assert (
            "[HTML](./lambda/beta/handler.handler.html) · [PNG](./lambda/beta/handler.handler.png)"
            in readme
        )

    def test_all_flag_rerenders_a_current_catalogue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--all`` forces a full re-render even when nothing is stale."""
        repo = _FakeRepo(tmp_path, monkeypatch)
        repo.png_renderer = _FakePngRenderer()
        repo.main()
        capsys.readouterr()
        repo.png_renderer = _FakePngRenderer()

        repo.main("--all")

        out = capsys.readouterr().out
        assert "Mode         : full" in out
        assert "Targets      : 2 of 2 selected" in out
        assert len(repo.png_renderer.rendered) == 2
        assert repo.png_renderer.closed

    def test_unknown_target_is_rejected_before_any_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo in ``--target`` aborts before Git, rendering, or any write."""
        repo = _FakeRepo(tmp_path, monkeypatch)

        with pytest.raises(SystemExit, match=r"Unknown target\(s\): \['nope.py:f'\]"):
            repo.main("--target", "nope.py:f")

        assert repo.git_calls == []
        assert not repo.png_renderer_requested
        assert not repo.artifact(repo.ALPHA, "html").exists()
