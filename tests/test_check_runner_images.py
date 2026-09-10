"""Tests for the runner-image drift check (``.github/scripts/check_runner_images.py``).

``runs-on: ubuntu-latest`` is not a version pin, so nothing else in the project
watches it. This check compares the labels the workflows use against the
``Available Images`` table upstream publishes, and the whole value of it rests on
one distinction: a newer **GA** image is drift worth acting on, a newer
**preview** image is not. Reporting a preview as actionable would push CI onto an
unannounced platform, so the split gets the most attention here.

The catalog is parsed from Markdown, which means the realistic failure is a
harmless-looking upstream edit — a badge added, a label list reworded, the table
moved — silently yielding an empty catalog that reads as "no drift". Every
parsing test therefore checks that a malformed shape produces *nothing* and that
``main`` turns nothing into exit 2 rather than success.

Fixtures mirror the real table's shape (badges in the name cell, ``<br>`` noise,
``or``-separated label lists). Network access is exercised through an injected
opener, so the suite never reaches raw.githubusercontent.com.
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_runner_images.py"

_spec = importlib.util.spec_from_file_location("check_runner_images", _SCRIPT)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_runner_images", checker)
_spec.loader.exec_module(checker)

PREVIEW_BADGE = "![preview](https://img.shields.io/badge/preview-0969DA)"
DEPRECATED_BADGE = "[![deprecated](https://img.shields.io/badge/deprecated-E5534B)](https://x/1)"
ENDPOINT_BADGE = "![Endpoint Badge](https://img.shields.io/endpoint?url=https%3A%2F%2Fx)"

CATALOG = f"""\
# Runner images

## Available Images
| Image | Architecture | YAML Label | Included Software |
| ------|--------------|------------|------------------|
| Ubuntu 26.04 {PREVIEW_BADGE}<br>{ENDPOINT_BADGE} | x64 | `ubuntu-26.04` | [x] |
| Ubuntu 24.04<br>{ENDPOINT_BADGE} | x64 | `ubuntu-latest` or `ubuntu-24.04` | [x] |
| Ubuntu 24.04 Arm64<br>{ENDPOINT_BADGE} | arm64 | `ubuntu-24.04-arm` | [x] |
| Ubuntu 22.04<br>{ENDPOINT_BADGE} | x64 | `ubuntu-22.04` | [x] |
| macOS 26 Arm64<br>{ENDPOINT_BADGE} | arm64 | `macos-latest`, `macos-26` or `macos-26-xlarge` | [x] |
| macOS 15 Arm64<br>{ENDPOINT_BADGE} | arm64 | `macos-15`, or `macos-15-xlarge` | [x] |
| macOS 14 Arm64 {DEPRECATED_BADGE}<br>{ENDPOINT_BADGE} | arm64 | `macos-14` | [x] |
| Windows Server 2025<br>{ENDPOINT_BADGE} | x64 | `windows-latest`, or `windows-2025` | [x] |

### Label scheme
- Prose that must not be parsed as a row.
"""


def _write_workflow(root: Path, body: str, name: str = "w.yml") -> Path:
    directory = root / ".github" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def _catalog_file(tmp_path: Path, text: str = CATALOG) -> Path:
    path = tmp_path / "README.md"
    path.write_text(text, encoding="utf-8")
    return path


def _image(name: str, arch: str, labels: tuple[str, ...], status: str) -> object:
    return checker.RunnerImage(name=name, architecture=arch, labels=labels, status=status)


# --------------------------------------------------------------------------
# RunnerImage
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "family", "version"),
    [
        ("Ubuntu 24.04", "ubuntu", (24, 4)),
        ("Windows Server 2025", "windows", (2025,)),
        ("macOS 26 Arm64", "macos", (26,)),
        ("Xcode 27", "xcode", (27,)),
    ],
)
def test_runner_image_parses_family_and_version(
    name: str, family: str, version: tuple[int, ...]
) -> None:
    """Windows Server folds into "windows" so its labels compare with each other."""
    image = _image(name, "x64", ("x",), checker.STATUS_GA)
    assert image.family == family
    assert image.version == version


def test_runner_image_without_a_recognised_family_compares_with_nothing() -> None:
    """An unparseable name must not be silently grouped with a real family."""
    image = _image("Ubuntu Slim", "x64", ("ubuntu-slim",), checker.STATUS_GA)
    assert image.family is None
    assert image.version is None


def test_runner_image_detects_architecture() -> None:
    assert _image("macOS 26 Arm64", "arm64", ("a",), checker.STATUS_GA).is_arm is True
    assert _image("macOS 26", "x64", ("a",), checker.STATUS_GA).is_arm is False


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        pytest.param(
            ("macos-latest", "macos-26", "macos-26-xlarge"), "macos-26", id="prefers-exact"
        ),
        pytest.param(("macos-latest",), "macos-latest", id="only-floating"),
        pytest.param(("macos-15-large",), "macos-15-large", id="only-sized"),
    ],
)
def test_preferred_label_avoids_floating_and_sized_labels(
    labels: tuple[str, ...], expected: str
) -> None:
    """Recommending ``-latest`` would re-introduce the very drift being reported."""
    assert _image("macOS 26 Arm64", "arm64", labels, checker.STATUS_GA).preferred_label == expected


def test_finding_renders_the_scan_row_shape() -> None:
    finding = checker.Finding(
        label="macos-15", current="macOS 15", recommended="macos-26", reason="r"
    )
    assert finding.as_row() == "macos-15|macOS 15|macos-26"


# --------------------------------------------------------------------------
# Catalog parsing
# --------------------------------------------------------------------------


def test_parse_catalog_reads_names_labels_and_status(tmp_path: Path) -> None:
    images = checker.parse_catalog(CATALOG)
    by_label = {label: image for image in images for label in image.labels}

    assert len(images) == 8
    assert by_label["ubuntu-latest"].name == "Ubuntu 24.04"
    assert by_label["ubuntu-latest"].status == checker.STATUS_GA
    assert by_label["ubuntu-24.04"].name == "Ubuntu 24.04"
    assert by_label["ubuntu-26.04"].status == checker.STATUS_PREVIEW
    assert by_label["macos-14"].status == checker.STATUS_DEPRECATED
    # Endpoint badges and <br> must not leak into the name.
    assert "Endpoint" not in by_label["ubuntu-latest"].name
    assert "br" not in by_label["ubuntu-latest"].name.lower()


def test_parse_catalog_reads_every_label_in_an_or_separated_cell() -> None:
    images = checker.parse_catalog(CATALOG)
    macos26 = next(image for image in images if image.name.startswith("macOS 26"))
    assert macos26.labels == ("macos-latest", "macos-26", "macos-26-xlarge")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("# No section here\n", id="section-absent"),
        pytest.param("## Available Images\nprose only, no table\n\n### Next\n", id="table-absent"),
        pytest.param(
            "## Available Images\n| Image | Arch |\n| --- | --- |\n| Ubuntu 24.04 | x64 |\n\n### N\n",
            id="too-few-columns",
        ),
        pytest.param(
            "## Available Images\n| Image | Arch | YAML Label |\n| --- | --- | --- |\n"
            "| Ubuntu 24.04 | x64 | no code span |\n\n### N\n",
            id="labels-not-in-backticks",
        ),
        pytest.param(
            "## Available Images\n| Image | Arch | YAML Label |\n| --- | --- | --- |\n"
            "|  | x64 | `ubuntu-latest` |\n\n### N\n",
            id="empty-name",
        ),
    ],
)
def test_parse_catalog_yields_nothing_for_an_unrecognised_shape(text: str) -> None:
    """An upstream edit must produce an empty catalog, never a wrong one.

    ``main`` converts empty into exit 2, so "we could not read the catalog" can
    never be mistaken for "there is no drift".
    """
    assert checker.parse_catalog(text) == []


def test_parse_catalog_ignores_the_header_and_separator_rows() -> None:
    images = checker.parse_catalog(CATALOG)
    assert not [image for image in images if image.name.lower().startswith("image")]
    assert not [image for image in images if set(image.name) <= {"-", " ", ":"}]


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("Ubuntu 24.04", checker.STATUS_GA),
        (f"Ubuntu 26.04 {PREVIEW_BADGE}", checker.STATUS_PREVIEW),
        ("Ubuntu 26.04 ![beta](https://x)", checker.STATUS_PREVIEW),
        (f"macOS 14 {DEPRECATED_BADGE}", checker.STATUS_DEPRECATED),
    ],
)
def test_cell_status_classifies_by_badge(cell: str, expected: str) -> None:
    """`beta` is treated as preview: both mean "not GA", which is what matters."""
    assert checker._cell_status(cell) == expected


# --------------------------------------------------------------------------
# Label collection
# --------------------------------------------------------------------------


def test_collect_used_labels_counts_direct_and_matrix_runners(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        """
jobs:
  a:
    runs-on: ubuntu-latest
  b:
    runs-on: [ubuntu-latest]
  c:
    runs-on: ${{ matrix.runner }}
    strategy:
      matrix:
        include:
          - runner: ubuntu-24.04-arm
          - runner: macos-15
""",
    )
    assert checker.collect_used_labels(tmp_path) == {
        "ubuntu-latest": 2,
        "ubuntu-24.04-arm": 1,
        "macos-15": 1,
    }


def test_collect_used_labels_skips_expressions(tmp_path: Path) -> None:
    """An expression names no image; the matrix values it resolves to are counted."""
    _write_workflow(tmp_path, "jobs:\n  a:\n    runs-on: ${{ matrix.runner }}\n")
    assert checker.collect_used_labels(tmp_path) == {}


def test_collect_used_labels_spans_every_workflow(tmp_path: Path) -> None:
    _write_workflow(tmp_path, "jobs:\n  a:\n    runs-on: ubuntu-latest\n", name="one.yml")
    _write_workflow(tmp_path, "jobs:\n  b:\n    runs-on: ubuntu-latest\n", name="two.yml")
    assert checker.collect_used_labels(tmp_path) == {"ubuntu-latest": 2}


# --------------------------------------------------------------------------
# evaluate
# --------------------------------------------------------------------------


def test_evaluate_reports_a_newer_ga_image_as_drift() -> None:
    images = checker.parse_catalog(CATALOG)
    drift, notes, unknown = checker.evaluate({"macos-15": 1}, images)

    assert [finding.as_row() for finding in drift] == ["macos-15|macOS 15 Arm64|macos-26"]
    assert notes == []
    assert unknown == []


def test_evaluate_reports_a_newer_preview_image_as_a_note_only() -> None:
    """The core distinction: a preview is context, never something to act on."""
    images = checker.parse_catalog(CATALOG)
    drift, notes, unknown = checker.evaluate({"ubuntu-latest": 70}, images)

    assert drift == []
    assert [finding.as_row() for finding in notes] == ["ubuntu-latest|Ubuntu 24.04|ubuntu-26.04"]
    assert "preview" in notes[0].reason


def test_evaluate_reports_a_deprecated_label_with_its_replacement() -> None:
    images = checker.parse_catalog(CATALOG)
    drift, _, _ = checker.evaluate({"macos-14": 1}, images)

    assert drift[0].current == "macOS 14 Arm64 (deprecated)"
    assert drift[0].recommended == "macos-26"
    assert "deprecated" in drift[0].reason


def test_evaluate_reports_a_deprecated_label_with_no_successor() -> None:
    """Deprecation still has to be reported when nothing newer is catalogued."""
    images = [_image("macOS 14 Arm64", "arm64", ("macos-14",), checker.STATUS_DEPRECATED)]
    drift, _, _ = checker.evaluate({"macos-14": 1}, images)

    assert drift[0].recommended == "see upstream"


def test_evaluate_is_quiet_when_the_newest_image_is_in_use() -> None:
    images = checker.parse_catalog(CATALOG)
    drift, notes, unknown = checker.evaluate({"windows-latest": 1}, images)

    assert (drift, notes, unknown) == ([], [], [])


def test_evaluate_separates_labels_upstream_does_not_publish() -> None:
    """A self-hosted label is not drift and must not be guessed about."""
    images = checker.parse_catalog(CATALOG)
    drift, notes, unknown = checker.evaluate({"our-self-hosted-box": 1}, images)

    assert (drift, notes) == ([], [])
    assert unknown == ["our-self-hosted-box"]


def test_evaluate_never_compares_across_families_or_architectures() -> None:
    """macOS 26 must not be offered as an upgrade for Ubuntu, or x64 for arm64."""
    images = checker.parse_catalog(CATALOG)
    drift, _, _ = checker.evaluate({"ubuntu-22.04": 1}, images)

    assert [finding.recommended for finding in drift] == ["ubuntu-24.04"]

    arm_only = [
        _image("Ubuntu 24.04 Arm64", "arm64", ("ubuntu-24.04-arm",), checker.STATUS_GA),
        _image("Ubuntu 26.04", "x64", ("ubuntu-26.04",), checker.STATUS_GA),
    ]
    drift_arm, _, _ = checker.evaluate({"ubuntu-24.04-arm": 1}, arm_only)
    assert drift_arm == [], "an x64 image was offered as an upgrade for an arm64 label"


def test_evaluate_ignores_images_with_no_comparable_version() -> None:
    images = [
        _image("Ubuntu Slim", "x64", ("ubuntu-slim",), checker.STATUS_GA),
        _image("Ubuntu 26.04", "x64", ("ubuntu-26.04",), checker.STATUS_GA),
    ]
    drift, notes, unknown = checker.evaluate({"ubuntu-slim": 1}, images)

    assert (drift, notes, unknown) == ([], [], [])


# --------------------------------------------------------------------------
# format_report
# --------------------------------------------------------------------------


def test_format_report_renders_drift_notes_and_unknown_labels() -> None:
    images = checker.parse_catalog(CATALOG)
    used = {"macos-15": 1, "ubuntu-latest": 2, "our-box": 1}
    drift, notes, unknown = checker.evaluate(used, images)
    rendered = checker.format_report(used, images, drift, notes, unknown)

    assert "3 label(s) in use, 8 upstream image(s)" in rendered
    assert "macos-15: macOS 15 Arm64 -> macos-26" in rendered
    assert "note: ubuntu-latest pins Ubuntu 24.04; ubuntu-26.04 exists" in rendered
    assert "not GitHub-hosted images" in rendered
    assert "our-box" in rendered


def test_format_report_states_the_all_clear_explicitly() -> None:
    images = checker.parse_catalog(CATALOG)
    used = {"windows-latest": 1}
    drift, notes, unknown = checker.evaluate(used, images)

    assert "newest generally-available image" in checker.format_report(
        used, images, drift, notes, unknown
    )


# --------------------------------------------------------------------------
# fetch_readme
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_fetch_readme_decodes_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checker.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(b"# hi\n"))
    assert checker.fetch_readme() == "# hi\n"


@pytest.mark.parametrize(
    "error",
    [urllib.error.URLError("down"), TimeoutError("slow"), OSError("boom"), ValueError("bad url")],
)
def test_fetch_readme_turns_transport_errors_into_catalog_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """A network blip must become "skip", never a false all-clear."""

    def _raise(*_: object, **__: object) -> None:
        raise error

    monkeypatch.setattr(checker.urllib.request, "urlopen", _raise)
    with pytest.raises(checker.CatalogError, match="could not fetch"):
        checker.fetch_readme()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def test_main_rows_emits_only_actionable_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_workflow(tmp_path, "jobs:\n  a:\n    runs-on: macos-15\n    b: ubuntu-latest\n")
    code = checker.main(
        ["--format", "rows", "--readme", str(_catalog_file(tmp_path)), "--root", str(tmp_path)]
    )
    assert code == 0
    assert capsys.readouterr().out == "macos-15|macOS 15 Arm64|macos-26\n"


def test_main_notes_emits_preview_context(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_workflow(tmp_path, "jobs:\n  a:\n    runs-on: ubuntu-latest\n")
    code = checker.main(
        ["--format", "notes", "--readme", str(_catalog_file(tmp_path)), "--root", str(tmp_path)]
    )
    assert code == 0
    assert capsys.readouterr().out == "ubuntu-latest|Ubuntu 24.04|ubuntu-26.04\n"


def test_main_report_is_the_default_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_workflow(tmp_path, "jobs:\n  a:\n    runs-on: macos-15\n")
    code = checker.main(["--readme", str(_catalog_file(tmp_path)), "--root", str(tmp_path)])
    assert code == 0
    assert "runner images:" in capsys.readouterr().out


def test_main_fetches_upstream_when_no_readme_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_workflow(tmp_path, "jobs:\n  a:\n    runs-on: macos-15\n")
    monkeypatch.setattr(
        checker.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(CATALOG.encode())
    )
    assert checker.main(["--format", "rows", "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "macos-15|macOS 15 Arm64|macos-26\n"


@pytest.mark.parametrize(
    ("catalog", "workflow", "expected_error"),
    [
        pytest.param("# nothing\n", "runs-on: macos-15\n", "could not be parsed", id="bad-catalog"),
        pytest.param(CATALOG, None, "no runs-on labels found", id="no-labels"),
    ],
)
def test_main_exits_two_without_printing_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    catalog: str,
    workflow: str | None,
    expected_error: str,
) -> None:
    """Exit 2 with empty stdout is what lets the shell caller treat this as "skip"."""
    if workflow is not None:
        _write_workflow(tmp_path, workflow)
    else:
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
    code = checker.main(
        [
            "--format",
            "rows",
            "--readme",
            str(_catalog_file(tmp_path, catalog)),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert captured.out == ""
    assert expected_error in captured.err


def test_main_exits_two_when_the_readme_file_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_workflow(tmp_path, "jobs:\n  a:\n    runs-on: macos-15\n")
    code = checker.main(["--readme", str(tmp_path / "absent.md"), "--root", str(tmp_path)])

    assert code == 2
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------
# Against this repository
# --------------------------------------------------------------------------


def test_this_repository_uses_only_labels_the_check_understands() -> None:
    """Every label in use must be a published image, so drift is really measured.

    A typo, or a self-hosted label nobody documented, would otherwise sit in the
    "unknown" bucket forever and never be compared against anything.
    """
    used = checker.collect_used_labels(REPO_ROOT)
    assert used, "no runs-on labels found in this repository"

    images = checker.parse_catalog(CATALOG)
    _, _, unknown = checker.evaluate(used, images)
    known_families = {"ubuntu", "macos", "windows"}
    unexpected = [
        label for label in unknown if not any(label.startswith(f) for f in known_families)
    ]
    assert not unexpected, (
        f"workflow labels that are not GitHub-hosted images: {unexpected}. "
        "If one is self-hosted, say so in .github/CI.md."
    )
