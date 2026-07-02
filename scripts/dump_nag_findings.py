#!/usr/bin/env python3
"""Dev-only helper: run the cdk-nag compliance test harness and print
a human-readable inventory of every finding, grouped by pack + rule +
resource-path, with the config name(s) each finding appears under.

When to use this
----------------
Reach for this script when ``tests/test_nag_compliance.py`` starts
failing and you need a compact view of what cdk-nag is actually
objecting to. pytest's default output buries the per-finding detail
inside an ``AssertionError`` repr — useful for CI gate failures, but
hard to read when you're trying to scope a new acknowledgment. This
script:

1. Iterates the same ``CONFIGS`` list pytest does (imported from
   ``tests._cdk_config_matrix`` so the two can never drift).
2. For each config, builds the full CDK app the way ``app.py`` does,
   registers the five rule packs as policy-validation plugins, calls
   ``app.synth()``, and reads the resulting ``validation-report.json``.
3. Groups them by ``(pack, rule, resource_path)`` so a wildcard that
   appears in N configs shows up once with the list of config names
   rather than N repeated entries.
4. Exits 0 if zero findings, 1 otherwise — which matches the pytest
   gate and means you can pipe this script's output to
   ``pre-commit`` or use it as a quick smoke before pushing.

When NOT to use this
--------------------
CI should use the pytest gate, not this script. This prints to stdout
and doesn't produce a junit.xml. For development use only.

Relationship to other tooling
-----------------------------
* ``tests/test_nag_compliance.py`` — the PR gate. Parameterizes over
  the same ``CONFIGS`` and fails if any unacknowledged finding exists.
  cdk-nag v3 writes findings to ``validation-report.json`` in the cloud
  assembly; both this script and the gate read that file.
* ``tests/test_cdk_synthesis_matrix.py`` — runs ``app.synth()``
  in-process for each config with pytest-xdist parallelism. Catches
  synth-time breakage; does NOT catch cdk-nag findings because
  ``app.synth()`` exits 0 even when unacknowledged findings exist.

Typical workflow
----------------
    # A finding shows up in CI on your PR. Reproduce locally:
    python3 scripts/dump_nag_findings.py

    # Scope the acknowledgment in gco/stacks/nag_suppressions.py or
    # whichever construct owns the resource, ideally with
    # ``applies_to`` as tight as possible (prefer an exact literal
    # ARN/detail over an unscoped blanket).
    # Re-run:
    python3 scripts/dump_nag_findings.py
    # -> exits 0, no findings.

    # Then run the full pytest gate to confirm:
    pytest tests/test_nag_compliance.py -n auto -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Put the repo root on sys.path so ``tests._cdk_config_matrix`` and
# ``tests.test_nag_compliance`` are importable when this script is
# invoked as ``python3 scripts/dump_nag_findings.py`` from any CWD.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._cdk_config_matrix import CONFIGS  # noqa: E402
from tests.test_nag_compliance import (  # noqa: E402
    _build_all_stacks,
    _build_app,
    _collect_nag_violations,
    _mock_helm_installer,
)


def run_config(name: str, overrides: dict[str, object]) -> list[dict[str, object]]:
    """Build and synthesize the full CDK app under one config overlay.

    Mirrors what ``tests/test_nag_compliance.py::TestCdkNagCompliance``
    does, minus the pytest wiring. The Docker image asset and the
    helm installer Lambda are both mocked so no Docker daemon is
    required — same as the regional-stack unit tests.

    Returns the list of finding dicts read from ``validation-report.json``
    (keys: ``pack`` / ``rule`` / ``description`` / ``paths``). An empty
    list means the config is clean.
    """
    # Late import because ``_build_app`` also imports gco.stacks under
    # the hood — keep the import side-effects inside the call so
    # ``python3 scripts/dump_nag_findings.py --help`` (or future arg
    # parsing) doesn't pay the CDK-init cost.
    from gco.stacks.regional_stack import GCORegionalStack

    app = _build_app(context_overrides=overrides)
    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(GCORegionalStack, "_create_helm_installer_lambda", _mock_helm_installer),
    ):
        mock_image = MagicMock()
        mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
        mock_docker.return_value = mock_image
        _build_all_stacks(app)
        # ``app.synth()`` is what runs the validation plugins and writes
        # validation-report.json — must happen inside the ``with`` block
        # so the Docker/helm mocks are still active during synthesis.
        app.synth()
    return _collect_nag_violations(app)


def main() -> int:
    # Track findings as {(pack, rule, resource_path): [config_names]}
    # so findings that repeat across configs collapse to one entry
    # with a list of where they showed up. The keys are deliberately
    # tuples (not dicts) so the ``sorted()`` call below is
    # deterministic — same input produces same output, which matters
    # when this output gets pasted into bug reports or commit
    # messages.
    all_findings: dict[tuple[str, str, str], list[str]] = {}

    for name, overrides in CONFIGS:
        print(f"\n{'=' * 72}")
        print(f"CONFIG: {name}")
        print(f"{'=' * 72}")
        findings = run_config(name, overrides)
        print(f"  total findings: {len(findings)}")
        for f in findings:
            paths = f["paths"] or ["(no construct path)"]
            for path in paths:  # type: ignore[union-attr]
                key = (str(f["pack"]), str(f["rule"]), str(path))
                all_findings.setdefault(key, []).append(name)

    print(f"\n{'=' * 72}")
    print(f"UNIQUE FINDINGS ACROSS ALL CONFIGS: {len(all_findings)}")
    print(f"{'=' * 72}")
    for (pack, rule, path), cfgs in sorted(all_findings.items()):
        print(f"\n  [{pack}] {rule}")
        print(f"    path:      {path}")
        # dedupe + sort configs so order is stable
        print(f"    seen in:   {', '.join(sorted(set(cfgs)))}")

    # Non-zero exit if any finding exists — matches the pytest gate,
    # so this script can be used as a quick pre-push smoke.
    return 0 if not all_findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
