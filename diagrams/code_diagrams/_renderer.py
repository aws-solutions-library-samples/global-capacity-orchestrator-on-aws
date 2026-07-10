"""pyflowchart + Playwright rendering helpers.

Splitting the rendering concerns out of
:mod:`diagrams.code_diagrams.generate` keeps the entry point small and
makes it easy to unit-test the path math without importing Playwright.
"""

from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

from diagrams.code_diagrams._targets import Target


@dataclass(frozen=True)
class RenderedTarget:
    """Output paths produced for a single :class:`Target`.

    Paths are all absolute so callers don't need to know where the
    project root lives.
    """

    target: Target
    html_path: Path
    png_path: Path | None
    """``None`` if PNG rendering was skipped or failed."""


def render_all(
    *,
    targets: list[Target],
    project_root: Path,
    output_dir: Path,
    render_png: bool,
) -> list[RenderedTarget]:
    """Render every target, returning where each output landed."""
    _require_pyflowchart()
    renderer = _make_png_renderer() if render_png else None
    try:
        results: list[RenderedTarget] = []
        for target in targets:
            result = _render_one(
                target=target,
                project_root=project_root,
                output_dir=output_dir,
                renderer=renderer,
            )
            results.append(result)
        return results
    finally:
        if renderer is not None:
            renderer.close()


def _render_one(
    *,
    target: Target,
    project_root: Path,
    output_dir: Path,
    renderer: _PlaywrightRenderer | None,
) -> RenderedTarget:
    """Render a single target and return its output paths."""
    from pyflowchart import Flowchart, output_html  # local import: optional dep

    source_path = (project_root / target.source).resolve()
    source = source_path.read_text(encoding="utf-8")
    print(f"\n🧭 {target.source}::{target.function}")

    # ``Flowchart.from_code`` handles simplification and the field selector
    # in one call; ``inner=True`` gives a control-flow chart of the body.
    flowchart = Flowchart.from_code(source, field=target.function, inner=target.inner)
    dsl = flowchart.flowchart()

    stem = _output_stem_for(target, output_dir=output_dir)
    html_path = stem.parent / f"{stem.name}.html"
    png_path = stem.parent / f"{stem.name}.png"
    html_path.parent.mkdir(parents=True, exist_ok=True)

    title = target.title or f"{target.source}::{target.function}"
    output_html(str(html_path), title, dsl)
    print(f"   ✓ HTML  {html_path.relative_to(project_root)}")

    if renderer is not None:
        ok = renderer.render(html_path=html_path, png_path=png_path)
        if ok:
            print(f"   ✓ PNG   {png_path.relative_to(project_root)}")
            return RenderedTarget(target=target, html_path=html_path, png_path=png_path)
        return RenderedTarget(target=target, html_path=html_path, png_path=None)

    # ``--skip-png`` or Playwright unavailable. If a previously generated
    # PNG is still on disk from an earlier run, keep pointing at it so
    # the README and source-file markers stay useful. Otherwise omit the
    # PNG reference.
    existing_png = png_path if png_path.is_file() else None
    return RenderedTarget(target=target, html_path=html_path, png_path=existing_png)


def _output_stem_for(target: Target, *, output_dir: Path) -> Path:
    """Compute the output path stem for ``target`` (no suffix).

    The output mirrors the source layout so large trees stay navigable.
    For a source at ``lambda/analytics-presigned-url/handler.py`` with
    function ``lambda_handler``, the stem is
    ``<output_dir>/lambda/analytics-presigned-url/handler.lambda_handler``
    (callers add ``.html`` / ``.png`` themselves; we cannot use
    :meth:`Path.with_suffix` here because ``.lambda_handler`` would be
    interpreted as a suffix and stripped).
    """
    src = Path(target.source)
    return output_dir / src.parent / f"{src.stem}.{target.slug()}"


def write_readme(results: list[RenderedTarget], *, output_dir: Path) -> None:
    """(Re)generate ``code_diagrams/README.md`` with a grouped index."""
    from diagrams.code_diagrams._readme import render_readme

    readme_path = output_dir / "README.md"
    content = render_readme(results, output_dir=output_dir)
    readme_path.write_text(content, encoding="utf-8")
    print(f"\n📝 Wrote {readme_path}")


def _require_pyflowchart() -> None:
    try:
        import pyflowchart  # noqa: F401
    except ImportError as exc:
        sys.exit(
            "pyflowchart is not installed. Install the project's "
            "``diagrams`` extra: ``pip install -e '.[diagrams]'``. "
            f"(underlying error: {exc})"
        )


class _PlaywrightRenderer:
    """Thin wrapper that keeps a single Playwright browser alive.

    We intentionally open/close the browser at the batch boundary (not
    per-target) so rendering dozens of targets doesn't pay the ~1s
    browser start-up cost each time.
    """

    def __init__(self) -> None:  # pragma: no cover - requires browser
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()

    def render(self, *, html_path: Path, png_path: Path) -> bool:
        """Screenshot the flowchart SVG from ``html_path`` into ``png_path``.

        Returns ``True`` on success.
        """
        # ``Error`` is Playwright's base exception; ``TimeoutError`` subclasses
        # it. We catch both so a single un-renderable diagram degrades to
        # HTML-only (``png_path=None``) instead of aborting the whole batch.
        from playwright.sync_api import Error as PlaywrightError  # pragma: no cover
        from playwright.sync_api import TimeoutError as PwTimeout  # pragma: no cover

        page = self._browser.new_page(
            viewport={"width": 2400, "height": 1800},
            device_scale_factor=2,
        )
        try:
            page.goto(html_path.absolute().as_uri())
            # flowchart.js renders into ``<div id="canvas">`` — wait for
            # the first child SVG node before screenshotting. Otherwise
            # we capture the empty pre-render container.
            page.wait_for_function(
                "document.querySelector('#canvas svg') !== null",
                timeout=30_000,
            )
            page.wait_for_timeout(500)  # give layout a beat to settle
            page.locator("#canvas svg").screenshot(path=str(png_path))
            return True
        except PwTimeout as exc:
            warnings.warn(
                f"Playwright timed out rendering {html_path}: {exc}",
                stacklevel=2,
            )
            return False
        except PlaywrightError as exc:
            # Most common cause: the flowchart is taller/wider than
            # Chromium's maximum screenshot dimensions (~32k px), so
            # ``Page.captureScreenshot`` returns "Unable to capture
            # screenshot". The interactive HTML is still written and
            # remains the primary artifact for these large diagrams.
            warnings.warn(
                f"Playwright could not screenshot {html_path} "
                f"(diagram may exceed Chromium's max size): {exc}",
                stacklevel=2,
            )
            return False
        finally:
            page.close()

    def close(self) -> None:
        """Shut down the browser and Playwright driver."""
        try:
            self._browser.close()
        finally:
            self._pw.stop()


def _make_png_renderer() -> _PlaywrightRenderer | None:
    """Best-effort Playwright initialisation.

    Returns ``None`` (with a warning) if Playwright or its browsers
    aren't installed, so the generator still produces the interactive
    HTML even in environments that can't run Chromium.
    """
    try:
        return _PlaywrightRenderer()
    except ImportError:
        warnings.warn(
            "Playwright not installed — skipping PNG rendering. "
            "Install with ``pip install -e '.[diagrams]'`` and then "
            "``playwright install chromium``.",
            stacklevel=2,
        )
        return None
    except Exception as exc:  # pragma: no cover - environment dependent
        warnings.warn(
            f"Playwright failed to start ({exc}) — skipping PNG rendering. "
            "Run ``playwright install chromium`` to fetch the browser.",
            stacklevel=2,
        )
        return None
