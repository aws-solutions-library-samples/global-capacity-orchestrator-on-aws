"""CI guard: every helper in ``.github/scripts`` must have a README table row.

``.github/scripts/README.md`` carries a "Files" table that maps each helper to
the job that invokes it. Four scripts had drifted out of that table before this
guard existed (``check_npm_audit.py``, ``use-pinned-npm.sh``,
``validate_k8s_manifests.py``, ``verify_inference_streaming_bundle_freshness.py``),
which is the failure mode this closes: the table is the only index of what CI
shells out to, so a missing row makes a script effectively invisible to anyone
auditing the pipeline.

Modeled on ``tests/test_bats_readme_coverage.py`` — same forward/reverse pair,
and the same deliberate scoping to *table rows* (backtick-wrapped names on lines
starting with ``|``). Prose mentions like the ``my-script.sh`` placeholder in
"Adding a New Script" therefore neither satisfy the forward check nor trip the
reverse one.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _PROJECT_ROOT / ".github" / "scripts"
_README = _SCRIPTS_DIR / "README.md"

# Suffixes that represent an invocable helper. Anything else in the directory
# (README.md itself, __pycache__, stray fixtures) is not a script to document.
_SCRIPT_SUFFIXES = (".sh", ".py")

_ROW_NAME = re.compile(r"`([A-Za-z0-9_.-]+\.(?:sh|py))`")


def _script_files() -> set[str]:
    return {
        path.name
        for path in _SCRIPTS_DIR.iterdir()
        if path.is_file() and path.suffix in _SCRIPT_SUFFIXES
    }


def _documented_in_table() -> set[str]:
    """Script names taken from the *first* cell of each table row.

    Only the leading cell is read, because that column is what indexes this
    directory. Later cells routinely reference files owned elsewhere — the
    ``dev_alias_live.sh`` row names ``scripts/setup-dev-alias.sh``, others name
    config files under ``.github/config`` — and treating those as rows would
    make the reverse check demand that files from other directories live here.
    """
    names: set[str] = set()
    for line in _README.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("|"):
            continue
        first_cell = stripped.split("|")[1] if "|" in stripped[1:] else ""
        names.update(_ROW_NAME.findall(first_cell))
    return names


def test_readme_exists() -> None:
    assert _README.is_file(), f"missing CI scripts README: {_README}"


def test_every_ci_script_has_a_readme_table_row() -> None:
    undocumented = sorted(_script_files() - _documented_in_table())
    assert not undocumented, (
        "Undocumented CI helper script(s) — add a row to the 'Files' table in "
        f".github/scripts/README.md naming the invoking job: {undocumented}"
    )


def test_readme_table_has_no_rows_for_missing_scripts() -> None:
    missing = sorted(name for name in _documented_in_table() if not (_SCRIPTS_DIR / name).is_file())
    assert not missing, (
        ".github/scripts/README.md documents script(s) that no longer exist in "
        f".github/scripts — remove or correct their row(s): {missing}"
    )
