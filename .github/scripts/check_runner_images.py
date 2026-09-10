"""Compare the ``runs-on:`` labels in this repository against upstream runner images.

Every other pinned surface in the project is watched by something. The runner
image is not: ``runs-on: ubuntu-latest`` is not a version pin, so Dependabot has
nothing to bump, and a floating label quietly changes underneath the workflows
whenever GitHub moves it. The two failure modes that matters are the opposite of
each other:

* **Pinned too tightly.** ``macos-15`` keeps working long after a newer image is
  generally available, so CI silently runs on an ageing platform — and
  eventually on one upstream has marked deprecated, which is a removal notice
  with a date attached.
* **Chasing a preview.** A brand-new image (``ubuntu-26.04`` today) appears in
  the catalog months before it is GA. Moving to it early trades a stable CI
  platform for an unannounced one, so a preview must never be reported as
  something to act on.

So this reports two different things. A newer *GA* image, or a label upstream
has deprecated, is **drift** — something to do. A newer image that is still
**preview** is a *note*: recorded so the next reader knows it exists and that
the current pin is deliberate, not stale.

The catalog comes from the ``Available Images`` table in ``actions/runner-images``'s
README, which is the canonical published list — there is no API for it. Status
comes from the badges upstream puts in the image name cell (``preview``,
``beta``, ``deprecated``); an image with no badge is GA. Parsing is deliberately
strict: an unrecognised table shape yields *nothing* rather than a guess, and
the caller treats an empty catalog as "skip", never as "no drift".

Usage::

    python3 .github/scripts/check_runner_images.py --format rows
    python3 .github/scripts/check_runner_images.py --format notes
    python3 .github/scripts/check_runner_images.py --format report
    python3 .github/scripts/check_runner_images.py --readme fixture.md --root .

Exit codes::

    0  the comparison completed (with or without findings)
    2  the catalog could not be fetched or parsed, or no labels were found

``main`` prints nothing on exit 2 except a diagnostic on stderr, so the shell
caller can treat empty output as "skip" exactly the way it treats an empty
``get_latest_*`` result.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
README_URL = "https://raw.githubusercontent.com/actions/runner-images/main/README.md"
FETCH_TIMEOUT_SECONDS = 20

# Badges upstream stamps into the image-name cell. Anything unbadged is GA.
STATUS_PREVIEW = "preview"
STATUS_DEPRECATED = "deprecated"
STATUS_GA = "ga"
_BADGE_PATTERN = re.compile(r"!\[(preview|beta|deprecated)\]", re.IGNORECASE)

# `runs-on: X`, `runs-on: [X]`, and the matrix indirection `runner: X`. An
# expression (${{ ... }}) names no image, so it is skipped and the matrix values
# it points at are collected instead.
_RUNS_ON_PATTERN = re.compile(r"^\s*runs-on:\s*(?:\[\s*)?([A-Za-z0-9._-]+)", re.MULTILINE)
_MATRIX_RUNNER_PATTERN = re.compile(r"^\s*-?\s*runner:\s*([A-Za-z0-9._-]+)\s*$", re.MULTILINE)

# An image family plus its comparable version, e.g. ("ubuntu", (24, 4)). Only
# labels within one family are ever compared: moving between families is not
# drift, it is a different platform.
_FAMILY_PATTERN = re.compile(
    r"^(?P<family>ubuntu|windows server|windows|macos|xcode)\s*(?P<version>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


class CatalogError(Exception):
    """The upstream catalog could not be fetched, or held no usable table."""


@dataclass(frozen=True)
class RunnerImage:
    """One row of the upstream Available Images table."""

    name: str
    architecture: str
    labels: tuple[str, ...]
    status: str

    @property
    def family(self) -> str | None:
        match = _FAMILY_PATTERN.match(self.name)
        return match.group("family").lower().replace("windows server", "windows") if match else None

    @property
    def version(self) -> tuple[int, ...] | None:
        match = _FAMILY_PATTERN.match(self.name)
        if not match:
            return None
        return tuple(int(part) for part in match.group("version").split("."))

    @property
    def is_arm(self) -> bool:
        return self.architecture.strip().lower() == "arm64"

    @property
    def preferred_label(self) -> str:
        """The label to recommend moving to.

        Prefers an explicit version (``macos-26``) over a floating one
        (``macos-latest``): a floating label silently changes platform under the
        workflow later, which is the problem this check exists to surface. Also
        skips size variants (``-large`` / ``-xlarge``), which are a billing
        decision rather than a platform one.
        """
        explicit = [
            label
            for label in self.labels
            if not label.endswith("-latest") and not label.endswith(("-large", "-xlarge"))
        ]
        return (explicit or list(self.labels))[0]


@dataclass
class Finding:
    """A single actionable difference, or an informational note."""

    label: str
    current: str
    recommended: str
    reason: str

    def as_row(self) -> str:
        """Render as the ``a|b|c`` shape the scan's Markdown tables consume."""
        return f"{self.label}|{self.current}|{self.recommended}"


def fetch_readme(url: str = README_URL, timeout: int = FETCH_TIMEOUT_SECONDS) -> str:
    """Return the upstream README text, or raise :class:`CatalogError`.

    The URL is a fixed literal, not built from any repository content, which is
    what makes the ``urlopen`` call here auditable.
    """
    try:
        with urllib.request.urlopen(  # nosec B310 - fixed https literal, no interpolation  # noqa: S310
            url, timeout=timeout
        ) as response:
            return str(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise CatalogError(f"could not fetch {url}: {exc}") from exc


def _cell_labels(cell: str) -> tuple[str, ...]:
    """Pull the backtick-quoted YAML labels out of a table cell.

    Upstream writes these as ``` `a`, `b`, or `c` ```, so the separators vary;
    reading only the code spans sidesteps that entirely.
    """
    return tuple(match.group(1).strip() for match in re.finditer(r"`([^`]+)`", cell))


def _cell_status(cell: str) -> str:
    """Classify an image-name cell by the badge upstream stamped on it."""
    match = _BADGE_PATTERN.search(cell)
    if not match:
        return STATUS_GA
    badge = match.group(1).lower()
    return STATUS_DEPRECATED if badge == STATUS_DEPRECATED else STATUS_PREVIEW


def _cell_name(cell: str) -> str:
    """Strip badges, images, links and ``<br>`` noise down to the image name."""
    text = re.sub(r"\[?!\[[^\]]*\]\([^)]*\)\]?(\([^)]*\))?", " ", cell)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return " ".join(text.split())


def parse_catalog(readme: str) -> list[RunnerImage]:
    """Parse the ``Available Images`` table into records.

    Returns ``[]`` when the section or its table cannot be found, which the
    caller must treat as "skip" rather than "nothing to report".
    """
    section = re.search(
        r"^##\s+Available Images\s*$(?P<body>.*?)^(?:##|###)\s+", readme, re.MULTILINE | re.DOTALL
    )
    body = section.group("body") if section else ""
    images: list[RunnerImage] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        # Skip the header and its separator row.
        if cells[0].lower().startswith("image") or set(cells[0]) <= {"-", " ", ":"}:
            continue
        labels = _cell_labels(cells[2])
        name = _cell_name(cells[0])
        if not labels or not name:
            continue
        images.append(
            RunnerImage(
                name=name,
                architecture=cells[1],
                labels=labels,
                status=_cell_status(cells[0]),
            )
        )
    return images


def collect_used_labels(root: Path) -> dict[str, int]:
    """Return ``{runner label: occurrences}`` across every workflow.

    Counts both direct ``runs-on:`` values and the ``runner:`` matrix values an
    expression-valued ``runs-on:`` resolves to, so a matrix-driven job is not
    invisible to the check.
    """
    counts: dict[str, int] = {}
    workflows = sorted((root / ".github" / "workflows").glob("*.yml"))
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        for pattern in (_RUNS_ON_PATTERN, _MATRIX_RUNNER_PATTERN):
            for match in pattern.finditer(text):
                # Expressions need no explicit guard: both patterns require a
                # bare `[A-Za-z0-9._-]` label, so `runs-on: ${{ matrix.runner }}`
                # simply does not match and the matrix values it resolves to are
                # picked up by _MATRIX_RUNNER_PATTERN instead.
                counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    return counts


def _newest_in_family(
    images: list[RunnerImage], reference: RunnerImage, status: str
) -> RunnerImage | None:
    """Newest image sharing ``reference``'s family, architecture and ``status``."""
    candidates = [
        image
        for image in images
        if image.status == status
        and image.family is not None
        and image.family == reference.family
        and image.is_arm == reference.is_arm
        and image.version is not None
        and reference.version is not None
        and image.version > reference.version
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda image: image.version or ())


def evaluate(
    used: dict[str, int], images: list[RunnerImage]
) -> tuple[list[Finding], list[Finding], list[str]]:
    """Return ``(drift, notes, unknown_labels)`` for the labels in use.

    Drift is a deprecated image or a newer GA one. A newer preview image is a
    note. Labels upstream does not list at all are returned separately: they are
    most likely self-hosted, so they are reported rather than guessed about.
    """
    by_label: dict[str, RunnerImage] = {}
    for catalog_image in images:
        for catalog_label in catalog_image.labels:
            # `-latest` resolves to whichever image claims it; every other label
            # is unique, so first-wins is stable here.
            by_label.setdefault(catalog_label, catalog_image)

    drift: list[Finding] = []
    notes: list[Finding] = []
    unknown: list[str] = []

    for label in sorted(used):
        image = by_label.get(label)
        if image is None:
            unknown.append(label)
            continue

        if image.status == STATUS_DEPRECATED:
            replacement = _newest_in_family(images, image, STATUS_GA)
            drift.append(
                Finding(
                    label=label,
                    current=f"{image.name} (deprecated)",
                    recommended=replacement.preferred_label if replacement else "see upstream",
                    reason="upstream has deprecated this image; it will be removed",
                )
            )
            continue

        newer_ga = _newest_in_family(images, image, STATUS_GA)
        if newer_ga is not None:
            drift.append(
                Finding(
                    label=label,
                    current=image.name,
                    recommended=newer_ga.preferred_label,
                    reason="a newer generally-available image exists",
                )
            )
            continue

        newer_preview = _newest_in_family(images, image, STATUS_PREVIEW)
        if newer_preview is not None:
            notes.append(
                Finding(
                    label=label,
                    current=image.name,
                    recommended=newer_preview.preferred_label,
                    reason="newer image exists but is still in preview; staying put is correct",
                )
            )
    return drift, notes, unknown


def format_report(
    used: dict[str, int],
    images: list[RunnerImage],
    drift: list[Finding],
    notes: list[Finding],
    unknown: list[str],
) -> str:
    """Render the human-facing summary."""
    lines = [
        f"runner images: {len(used)} label(s) in use, {len(images)} upstream image(s) catalogued"
    ]
    for finding in drift:
        lines.append(f"  - {finding.label}: {finding.current} -> {finding.recommended}")
        lines.append(f"      {finding.reason}")
    for finding in notes:
        lines.append(
            f"  note: {finding.label} pins {finding.current}; {finding.recommended} exists"
        )
        lines.append(f"      {finding.reason}")
    if unknown:
        lines.append(
            f"  note: {len(unknown)} label(s) are not GitHub-hosted images "
            f"(self-hosted, or a typo): {', '.join(unknown)}"
        )
    if not drift:
        lines.append("  every label in use resolves to the newest generally-available image.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--format",
        choices=("rows", "notes", "report"),
        default="report",
        help="rows: drift as name|current|latest. notes: preview/self-hosted context. "
        "report: human summary.",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=None,
        help="Read the catalog from a local file instead of fetching it (used by tests).",
    )
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="Repository root holding .github/workflows."
    )
    args = parser.parse_args(argv)

    try:
        if args.readme is not None:
            readme = args.readme.read_text(encoding="utf-8")
        else:
            readme = fetch_readme()
        images = parse_catalog(readme)
        if not images:
            raise CatalogError("the Available Images table could not be parsed")
        used = collect_used_labels(args.root)
        if not used:
            raise CatalogError(f"no runs-on labels found under {args.root}/.github/workflows")
    except (CatalogError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    drift, notes, unknown = evaluate(used, images)
    if args.format == "rows":
        for finding in drift:
            print(finding.as_row())
    elif args.format == "notes":
        for finding in notes:
            print(finding.as_row())
    else:
        print(format_report(used, images, drift, notes, unknown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
