"""Validate Kubernetes manifests with kubeconform (schema, not just YAML syntax).

Confirms that every hand-authored manifest this repo ships — the
kubectl-applier's own manifests and the `examples/` gallery — is not just
parseable YAML but a *schema-valid* Kubernetes (or supported CRD) resource.
`kubeconform` (https://github.com/yannh/kubeconform) does the actual schema
check; this script exists to bridge two gaps kubeconform can't close on its
own:

  Template placeholders: `lambda/kubectl-applier-simple/manifests/*.yaml`
    contains `{{PLACEHOLDER}}` tokens the kubectl-applier Lambda substitutes
    at deploy time (see `handler.py` / `regional_stack.py`). Raw, these
    aren't valid YAML in several spots (bare scalars, a block-list
    placeholder), so kubeconform can't even parse the file. This script
    renders every placeholder to a schema-shaped stub first — enough to make
    the YAML parse and the field types check out, not a claim about the real
    runtime value.

  Non-Kubernetes files under `examples/`: that directory also ships a DAG
    orchestration file (`pipeline-dag.yaml`, a GCO-specific format, not a K8s
    manifest) and JSON metric/state fixtures used by other examples. Those
    are excluded by construction (only `*.yaml`/`*.yml` under `examples/` are
    scanned, and `pipeline-dag.yaml` is skipped by name).

Schema resolution: kubeconform's bundled catalog only covers upstream
Kubernetes. CRDs this repo depends on (Karpenter `NodePool`, the AWS Load
Balancer Controller Gateway API configuration CRDs, Kueue, KEDA) are resolved
via the community datreeio/CRDs-catalog as a second `-schema-location`. Two CRDs used in
`examples/` aren't in that catalog yet (KubeRay's `RayCluster`, Volcano's
`Job`) — those are validated for YAML-shape only (via the structural check)
and explicitly `-skip`ped in the schema pass so kubeconform doesn't report a
false "no schema found" error for them.

Usage::

    # Validate both directories with the real kubeconform binary (the CI gate):
    python3 .github/scripts/validate_k8s_manifests.py

    # Validate a specific directory, file, or quoted glob. --path is repeatable:
    python3 .github/scripts/validate_k8s_manifests.py --path examples/simple-job.yaml
    python3 .github/scripts/validate_k8s_manifests.py --path 'examples/**/*.yaml'

    # Point at a different kubeconform binary:
    python3 .github/scripts/validate_k8s_manifests.py --kubeconform-binary /usr/local/bin/kubeconform

Exit codes::

    0  every scanned manifest is schema-valid (or intentionally skipped)
    1  one or more manifests failed validation
    2  unexpected I/O / argument error (kubeconform missing, directory absent)

The module is importable from the test suite — call ``render_placeholders()``,
``collect_target_files()``, or ``iter_target_files()`` directly to exercise the
logic without invoking the binary.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import shutil
import subprocess  # nosec B404 - used only to invoke the pinned `kubeconform` binary with fixed argv
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories this script scans by default. Each entry is a directory
# relative to the repo root; every ``*.yaml``/``*.yml`` file directly inside
# it is a candidate (no recursion needed — neither directory nests further).
DEFAULT_TARGET_DIRS = (
    "lambda/kubectl-applier-simple/manifests",
    "examples",
)

# Files that are YAML but not Kubernetes manifests, so they're excluded by
# name rather than left for kubeconform to fail on a missing 'kind'/'apiVersion'.
#   - pipeline-dag.yaml: a GCO DAG-orchestration definition (see `gco dag`,
#     tests/test_mission_validation.py-style docs). Its own structure is
#     checked separately in CI (integration:k8s:manifest-schema's "Validate
#     DAG pipeline definitions" step) — validating it against the K8s schema
#     would always fail because it deliberately has no 'kind'.
NON_MANIFEST_FILENAMES = frozenset({"pipeline-dag.yaml"})

# GVK-qualified (not bare-Kind) skips for CRDs used in examples/ that the
# datreeio/CRDs-catalog fallback doesn't (yet) carry a schema for. GVK
# qualification matters here specifically because Volcano's Job kind
# collides with the built-in batch/v1 Job — a bare `-skip Job` would also
# skip every ordinary Job manifest in the repo.
SCHEMA_UNAVAILABLE_SKIPS = (
    "ray.io/v1/RayCluster",  # KubeRay — not in datreeio/CRDs-catalog
    "batch.volcano.sh/v1alpha1/Job",  # Volcano — not in datreeio/CRDs-catalog
)

# kubeconform's own default schema catalog (upstream Kubernetes resources).
DEFAULT_SCHEMA_LOCATION = "default"

# Community-maintained CRD catalog covering Karpenter, EKS Auto Mode, Kueue,
# KEDA, and hundreds of other CRDs — see https://github.com/datreeio/CRDs-catalog.
# kubeconform tries -schema-location entries in order and stops at the first
# match, so this is consulted only when a Kind isn't a built-in K8s resource.
CRD_CATALOG_SCHEMA_LOCATION = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)

_PLACEHOLDER_RE = re.compile(r"\{\{[A-Za-z_]+\}\}")

# A handful of placeholders sit in *structural* or *typed* positions where a
# generic string stub would either break YAML parsing or fail the schema's
# type check. Each needs a stub shaped like the real substitution.
#
#   {{VPC_ENDPOINT_CIDR_BLOCKS}} expands to one or more `- ipBlock: {cidr: ...}`
#   list entries (see regional_stack.py::_compute... / the NetworkPolicy
#   `to:` block in 03-network-policies.yaml) — the placeholder sits at the
#   start of a YAML sequence, so it needs a real sequence item, not a string.
_STRUCTURAL_STUBS: dict[str, str] = {
    "{{VPC_ENDPOINT_CIDR_BLOCKS}}": '- ipBlock:\n            cidr: "10.0.0.0/16"',
}

# Placeholders that sit in a bare (unquoted) numeric scalar position — e.g.
# `pollingInterval: {{QP_POLLING_INTERVAL}}` in post-helm-sqs-consumer.yaml.
# These must render to a bare integer, not a quoted string, or the field
# fails the schema's `type: integer` check.
_INTEGER_PLACEHOLDER_TOKENS: frozenset[str] = frozenset(
    {
        "{{QP_POLLING_INTERVAL}}",
        "{{QP_SUCCESSFUL_JOBS_HISTORY}}",
        "{{QP_FAILED_JOBS_HISTORY}}",
        "{{QP_MAX_CONCURRENT_JOBS}}",
    }
)

# Every other placeholder (quoted string values, and the couple of bare
# `image: {{...}}` lines) renders fine as a generic string stub — YAML
# treats an unquoted bare word as a string scalar automatically.
_GENERIC_STUB = "placeholder-value"


def render_placeholders(text: str) -> str:
    """Replace every ``{{PLACEHOLDER}}`` token with a schema-shaped stub.

    This is a validation-time rendering only — it exists to make templated
    manifests parseable and schema-checkable, not to model what the
    kubectl-applier Lambda actually substitutes at deploy time. See the
    module docstring for why each stub category exists.
    """
    for token, stub in _STRUCTURAL_STUBS.items():
        text = text.replace(token, stub)
    for token in _INTEGER_PLACEHOLDER_TOKENS:
        text = text.replace(token, "1")
    return _PLACEHOLDER_RE.sub(_GENERIC_STUB, text)


def _manifest_files_in_directory(directory: Path) -> list[Path]:
    """Return supported direct-child manifests from one directory."""
    files: list[Path] = []
    for pattern in ("*.yaml", "*.yml"):
        for path in sorted(directory.glob(pattern)):
            if path.name not in NON_MANIFEST_FILENAMES:
                files.append(path.resolve())
    return files


def collect_target_files(targets: tuple[str, ...]) -> tuple[list[Path], list[str]]:
    """Resolve every explicit directory, file, or glob independently.

    Valid files are returned even when another input is invalid so callers can
    still validate and report them. Errors preserve one entry per bad explicit
    input. Directories retain the historical direct-child-only behavior; quoted
    globs can opt into recursion with ``**``.
    """
    files: list[Path] = []
    errors: list[str] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            files.append(resolved)

    for raw_target in targets:
        expanded = Path(raw_target).expanduser()
        resolved_input = expanded if expanded.is_absolute() else _REPO_ROOT / expanded

        if glob.has_magic(str(expanded)):
            matches = [
                Path(match) for match in sorted(glob.glob(str(resolved_input), recursive=True))
            ]
            matched_manifests: list[Path] = []
            for match in matches:
                if match.is_dir():
                    matched_manifests.extend(_manifest_files_in_directory(match))
                elif (
                    match.is_file()
                    and match.suffix in (".yaml", ".yml")
                    and match.name not in NON_MANIFEST_FILENAMES
                ):
                    matched_manifests.append(match.resolve())
            if not matched_manifests:
                errors.append(f"{raw_target}: glob matched no Kubernetes YAML manifests")
                continue
            for path in matched_manifests:
                add(path)
            continue

        if not resolved_input.exists():
            errors.append(f"{raw_target}: path does not exist")
            continue
        if resolved_input.is_dir():
            directory_files = _manifest_files_in_directory(resolved_input)
            if not directory_files:
                errors.append(f"{raw_target}: directory contains no Kubernetes YAML manifests")
                continue
            for path in directory_files:
                add(path)
            continue
        if resolved_input.is_file():
            if resolved_input.suffix not in (".yaml", ".yml"):
                errors.append(f"{raw_target}: explicit file is not .yaml or .yml")
            elif resolved_input.name in NON_MANIFEST_FILENAMES:
                errors.append(f"{raw_target}: explicit file is not a Kubernetes manifest")
            else:
                add(resolved_input)
            continue
        errors.append(f"{raw_target}: unsupported input type")

    return files, errors


def iter_target_files(target_dirs: tuple[str, ...] = DEFAULT_TARGET_DIRS) -> list[Path]:
    """Compatibility wrapper returning valid manifest files only.

    Use :func:`collect_target_files` when input errors must be surfaced.
    """
    files, _errors = collect_target_files(target_dirs)
    return files


def _rendered_relative_path(path: Path) -> Path:
    """Return a collision-safe relative location for a rendered source file."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(_REPO_ROOT.resolve())
    except ValueError:
        # Explicit absolute paths outside the repository are still supported.
        # Preserve their path below a marker rather than flattening basenames.
        return Path("_external", *resolved.parts[1:])


def render_tree(files: list[Path], dest: Path) -> list[Path]:
    """Render files beneath ``dest`` while preserving source-relative paths.

    Repository files retain their repository-relative path, preventing files
    with the same basename in different inputs from overwriting one another.
    Returns the rendered paths for callers/tests that need the mapping.
    """
    rendered_paths: list[Path] = []
    dest.mkdir(parents=True, exist_ok=True)
    for path in files:
        text = path.read_text(encoding="utf-8")
        rendered = render_placeholders(text) if "{{" in text else text
        rendered_path = dest / _rendered_relative_path(path)
        rendered_path.parent.mkdir(parents=True, exist_ok=True)
        rendered_path.write_text(rendered, encoding="utf-8")
        rendered_paths.append(rendered_path)
    return rendered_paths


def run_kubeconform(
    directory: Path,
    *,
    kubeconform_binary: str = "kubeconform",
    strict: bool = True,
    extra_schema_locations: tuple[str, ...] = (CRD_CATALOG_SCHEMA_LOCATION,),
    skip_gvks: tuple[str, ...] = SCHEMA_UNAVAILABLE_SKIPS,
) -> tuple[int, object]:
    """Run kubeconform against every manifest in ``directory``, JSON output.

    Returns ``(returncode, parsed_json)``. ``parsed_json`` is ``{}`` if
    kubeconform produced no parseable JSON (e.g. the binary is missing —
    callers should check that separately via ``shutil.which`` before calling).
    """
    cmd = [
        kubeconform_binary,
        "-output",
        "json",
        "-summary",
        "-verbose",
        "-schema-location",
        DEFAULT_SCHEMA_LOCATION,
    ]
    for location in extra_schema_locations:
        cmd.extend(["-schema-location", location])
    if skip_gvks:
        cmd.extend(["-skip", ",".join(skip_gvks)])
    if strict:
        cmd.append("-strict")
    cmd.append(str(directory))

    proc = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed argv, no shell=True
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    try:
        parsed = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        parsed = {}
    return proc.returncode, parsed


_KUBECONFORM_STATUS_TO_SUMMARY = {
    "statusValid": "valid",
    "statusInvalid": "invalid",
    "statusError": "errors",
    "statusSkipped": "skipped",
}


def validate_kubeconform_output(result: object, *, expected_files: int) -> list[str]:
    """Validate kubeconform's JSON envelope and resource accounting.

    A zero process exit code is not sufficient: blank, malformed, truncated,
    or structurally incomplete JSON must fail closed rather than reporting
    ``OK: 0 manifest(s)``.
    """
    if not isinstance(result, dict):
        return ["top-level JSON value is not an object"]

    errors: list[str] = []
    resources = result.get("resources")
    if not isinstance(resources, list):
        return ["'resources' is missing or is not an array"]
    if not resources:
        errors.append("'resources' is empty")
    elif len(resources) < expected_files:
        errors.append(
            f"only {len(resources)} resource result(s) were returned for "
            f"{expected_files} input file(s)"
        )

    actual_counts = dict.fromkeys(_KUBECONFORM_STATUS_TO_SUMMARY.values(), 0)
    for index, resource in enumerate(resources):
        if not isinstance(resource, dict):
            errors.append(f"resources[{index}] is not an object")
            continue
        status = resource.get("status")
        summary_field = (
            _KUBECONFORM_STATUS_TO_SUMMARY.get(status) if isinstance(status, str) else None
        )
        if summary_field is None:
            errors.append(f"resources[{index}] has unknown status {status!r}")
            continue
        actual_counts[summary_field] += 1

    summary = result.get("summary")
    if not isinstance(summary, dict):
        errors.append("'summary' is missing or is not an object")
        return errors

    for field, actual_count in actual_counts.items():
        reported_count = summary.get(field)
        if (
            isinstance(reported_count, bool)
            or not isinstance(reported_count, int)
            or reported_count < 0
        ):
            errors.append(f"summary.{field} is missing or is not a non-negative integer")
        elif reported_count != actual_count:
            errors.append(
                f"summary.{field} reports {reported_count}, but resources contain {actual_count}"
            )

    return errors


def format_failures(result: dict[str, Any]) -> list[str]:
    """Turn kubeconform's per-resource JSON records into readable error lines.

    Only ``statusInvalid`` and ``statusError`` are failures — ``statusValid``
    and ``statusSkipped`` (the two explicitly ``-skip``ped CRDs) are fine.
    """
    lines: list[str] = []
    for resource in result.get("resources", []):
        status = resource.get("status")
        if status not in ("statusInvalid", "statusError"):
            continue
        filename = resource.get("filename", "<unknown file>")
        kind = resource.get("kind") or "<unparsed>"
        name = resource.get("name") or ""
        label = f"{kind} {name}".strip()
        msg = resource.get("msg", "")
        lines.append(f"{filename}: {label}: {msg}")
    return lines


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        help=(
            "Directory, YAML file, or glob to validate (relative to repo root or absolute). "
            "Repeatable; quote glob patterns so Python expands them. Defaults to both "
            "lambda/kubectl-applier-simple/manifests and examples."
        ),
    )
    parser.add_argument(
        "--kubeconform-binary",
        default="kubeconform",
        help="kubeconform executable to use (default: 'kubeconform' on PATH).",
    )
    parser.add_argument(
        "--no-strict",
        action="store_true",
        help="Disable kubeconform's -strict mode (allow unknown/additional properties).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print a per-resource OK line in addition to failures.",
    )
    return parser


def _print_input_errors(errors: list[str]) -> None:
    if not errors:
        return
    print(f"ERROR: {len(errors)} manifest input problem(s) found:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    targets = tuple(args.paths) if args.paths else DEFAULT_TARGET_DIRS
    files, input_errors = collect_target_files(targets)

    if shutil.which(args.kubeconform_binary) is None:
        _print_input_errors(input_errors)
        print(
            f"ERROR: '{args.kubeconform_binary}' not found on PATH. "
            "Install it (see Dockerfile.dev / docs/MAINTENANCE.md) or pass "
            "--kubeconform-binary.",
            file=sys.stderr,
        )
        return 2

    if not files:
        _print_input_errors(input_errors)
        if not input_errors:
            print(f"ERROR: no *.yaml/*.yml files found for {targets}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="gco-k8s-validate-") as tmp:
        rendered_dir = Path(tmp)
        render_tree(files, rendered_dir)

        rc, result = run_kubeconform(
            rendered_dir,
            kubeconform_binary=args.kubeconform_binary,
            strict=not args.no_strict,
        )

    output_errors = validate_kubeconform_output(result, expected_files=len(files))
    result_dict = result if isinstance(result, dict) else {}

    if args.verbose and not output_errors:
        for resource in result_dict.get("resources", []):
            if resource.get("status") == "statusValid":
                kind = resource.get("kind") or ""
                name = resource.get("name") or ""
                print(f"ok    {resource.get('filename')}: {kind} {name}".rstrip())

    failures = format_failures(result_dict) if not output_errors else []
    summary = result_dict.get("summary", {})

    if failures:
        print()
        print(f"ERROR: {len(failures)} Kubernetes manifest validation problem(s) found:")
        for line in failures:
            print(f"  - {line}")
        print()
        print(
            "Fix the manifest (or, if this is a new CRD with no upstream schema "
            "yet, add it to SCHEMA_UNAVAILABLE_SKIPS in "
            "validate_k8s_manifests.py with a comment explaining why)."
        )

    _print_input_errors(input_errors)

    runtime_failure = bool(output_errors) or (rc != 0 and not failures)
    if output_errors:
        print("ERROR: kubeconform returned unusable JSON output:", file=sys.stderr)
        for error in output_errors:
            print(f"  - {error}", file=sys.stderr)
    elif rc != 0 and not failures:
        # A non-zero process result without resource validation failures is an
        # invocation/runtime error, even if a partial JSON document was emitted.
        print("ERROR: kubeconform exited non-zero without validation failures.", file=sys.stderr)

    # Input/runtime errors take precedence, but valid supplied files were still
    # rendered and validated above so one missing path cannot mask their report.
    if input_errors or runtime_failure:
        return 2
    if failures:
        return 1

    print(
        f"OK: {summary.get('valid', 0)} manifest(s) are schema-valid "
        f"({summary.get('skipped', 0)} intentionally skipped: no upstream "
        "schema yet for KubeRay/Volcano CRDs)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
