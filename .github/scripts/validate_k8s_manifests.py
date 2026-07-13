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
Kubernetes. CRDs this repo depends on (Karpenter `NodePool`, EKS Auto Mode
`IngressClassParams`, Kueue, KEDA) are resolved via the community
datreeio/CRDs-catalog as a second `-schema-location`. Two CRDs used in
`examples/` aren't in that catalog yet (KubeRay's `RayCluster`, Volcano's
`Job`) — those are validated for YAML-shape only (via the structural check)
and explicitly `-skip`ped in the schema pass so kubeconform doesn't report a
false "no schema found" error for them.

Usage::

    # Validate both directories with the real kubeconform binary (the CI gate):
    python3 .github/scripts/validate_k8s_manifests.py

    # Validate a specific directory/pattern:
    python3 .github/scripts/validate_k8s_manifests.py --path examples

    # Point at a different kubeconform binary:
    python3 .github/scripts/validate_k8s_manifests.py --kubeconform-binary /usr/local/bin/kubeconform

Exit codes::

    0  every scanned manifest is schema-valid (or intentionally skipped)
    1  one or more manifests failed validation
    2  unexpected I/O / argument error (kubeconform missing, directory absent)

The module is importable from the test suite — call ``render_placeholders()``
or ``iter_target_files()`` directly to exercise the logic without invoking
the binary.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess  # nosec B404 - used only to invoke the pinned `kubeconform` binary with fixed argv
import sys
import tempfile
from pathlib import Path

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


def iter_target_files(target_dirs: tuple[str, ...] = DEFAULT_TARGET_DIRS) -> list[Path]:
    """List every manifest candidate under the given directories.

    Only ``*.yaml``/``*.yml`` directly inside each directory (no recursion —
    neither target nests further today; a future subdirectory would need an
    explicit decision about whether it's in scope). Filters out
    ``NON_MANIFEST_FILENAMES`` by name.
    """
    files: list[Path] = []
    for rel_dir in target_dirs:
        directory = _REPO_ROOT / rel_dir
        if not directory.is_dir():
            continue
        for pattern in ("*.yaml", "*.yml"):
            for path in sorted(directory.glob(pattern)):
                if path.name in NON_MANIFEST_FILENAMES:
                    continue
                files.append(path)
    return files


def render_tree(files: list[Path], dest: Path) -> None:
    """Render every file into ``dest`` (flat — filenames are unique per source dir).

    Files with no ``{{`` token are copied byte-for-byte; templated files are
    rendered through ``render_placeholders``. kubeconform is pointed at
    ``dest`` instead of the real paths so the reported ``filename`` in
    kubeconform's own output stays close to the original (same basename),
    while the content it actually parses is the rendered version.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for path in files:
        text = path.read_text(encoding="utf-8")
        rendered = render_placeholders(text) if "{{" in text else text
        (dest / path.name).write_text(rendered, encoding="utf-8")


def run_kubeconform(
    directory: Path,
    *,
    kubeconform_binary: str = "kubeconform",
    strict: bool = True,
    extra_schema_locations: tuple[str, ...] = (CRD_CATALOG_SCHEMA_LOCATION,),
    skip_gvks: tuple[str, ...] = SCHEMA_UNAVAILABLE_SKIPS,
) -> tuple[int, dict]:
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


def format_failures(result: dict) -> list[str]:
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
            "Directory to scan (relative to repo root). Repeatable. "
            "Defaults to both lambda/kubectl-applier-simple/manifests and examples."
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    target_dirs = tuple(args.paths) if args.paths else DEFAULT_TARGET_DIRS

    if shutil.which(args.kubeconform_binary) is None:
        print(
            f"ERROR: '{args.kubeconform_binary}' not found on PATH. "
            "Install it (see Dockerfile.dev / docs/MAINTENANCE.md) or pass "
            "--kubeconform-binary.",
            file=sys.stderr,
        )
        return 2

    files = iter_target_files(target_dirs)
    if not files:
        print(f"ERROR: no *.yaml/*.yml files found under {target_dirs}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="gco-k8s-validate-") as tmp:
        rendered_dir = Path(tmp)
        render_tree(files, rendered_dir)

        rc, result = run_kubeconform(
            rendered_dir,
            kubeconform_binary=args.kubeconform_binary,
            strict=not args.no_strict,
        )

    if args.verbose:
        for resource in result.get("resources", []):
            if resource.get("status") == "statusValid":
                kind = resource.get("kind") or ""
                name = resource.get("name") or ""
                print(f"ok    {resource.get('filename')}: {kind} {name}".rstrip())

    failures = format_failures(result)
    summary = result.get("summary", {})

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
        return 1

    if rc != 0 and not result:
        # kubeconform exited non-zero but produced no parseable JSON —
        # something went wrong beyond a normal validation failure (crash,
        # bad flag, etc). Surface it rather than silently reporting success.
        print("ERROR: kubeconform exited non-zero with no parseable output.", file=sys.stderr)
        return 2

    print(
        f"OK: {summary.get('valid', 0)} manifest(s) are schema-valid "
        f"({summary.get('skipped', 0)} intentionally skipped: no upstream "
        "schema yet for KubeRay/Volcano CRDs)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
