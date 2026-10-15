"""End-to-end cdk-nag compliance regression test.

What this catches
-----------------
The class ``TestCdkNagCompliance`` synthesizes the full CDK application
exactly the way ``app.py`` does — Global, API Gateway, one or more
Regional and regional API bridge stacks, and the Monitoring stack, built by
``tests/_cdk_app_builder.py`` — with the five cdk-nag rule packs registered
as CDK policy-validation plugins. After ``app.synth()`` returns, the test
reads the cloud assembly's ``validation-report.json`` and asserts that no
unacknowledged findings survived.

Why read the validation report
-------------------------------
cdk-nag v3 runs as an ``IPolicyValidationPlugin`` (CDK's native policy
validation framework) rather than as an ``IAspect`` that emits synth
annotations. Violations are written to ``validation-report.json`` in the
cloud assembly directory; ``app.synth()`` itself does **not** raise on
findings (the CDK toolkit sets a non-zero exit code instead), so a plain
in-process synth would silently pass. Reading the report gives us a
deterministic, in-process signal: every unacknowledged finding lands in
the report and the test fails with the rule id, resource path, and a
short description — the three things you need to either scope an existing
acknowledgment or add a new one.

Scope
-----
Parameterized across the IAM-relevant subset of the cdk.json
configuration matrix (``tests/_cdk_config_matrix.NAG_CONFIGS``): the
configs that produce distinct IAM policy surfaces (default,
multi-region, single-region, api-gateway-region-split, FSx-enabled,
all-features-enabled, three-regions, plus the analytics-environment
fixtures). The full CONFIGS matrix runs
via ``tests/test_cdk_synthesis_matrix.py`` for synthesis correctness;
this test focuses on the configs that actually change IAM roles and
policies, which is where cdk-nag findings live.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest

from tests._cdk_app_builder import load_context, synthesize_app
from tests._cdk_config_matrix import NAG_CONFIGS as _CONFIGS


def _build_app(
    context_overrides: dict[str, Any] | None = None,
) -> cdk.App:
    """Construct a CDK ``App`` configured the same way ``app.py`` does, with
    the five cdk-nag rule packs registered as policy-validation plugins.

    Args:
        context_overrides: cdk.json context keys to override, merged over the
            baseline by :func:`tests._cdk_app_builder.load_context`. This is how
            each parameterized config exercises a different code path
            (multi-region, FSx on, etc.).

    Returns:
        The constructed, empty ``cdk.App``. Pass it to
        :func:`tests._cdk_app_builder.synthesize_app`, then read the findings
        with :func:`_collect_nag_violations`.
    """
    app = cdk.App(context=load_context(context_overrides))

    # Register the nag packs exactly the way ``app.py`` does: the
    # LambdaTracingAspect (a real Aspect) runs during synthesis, then the
    # five rule packs run as policy-validation plugins against the
    # synthesized templates.
    from app import LambdaTracingAspect
    from gco.stacks.nag_suppressions import nag_validation_plugins

    cdk.Aspects.of(app).add(LambdaTracingAspect())
    cdk.Validations.of(app).add_plugins(*nag_validation_plugins(app, verbose=True))

    return app


def _collect_nag_violations(app: cdk.App) -> list[dict[str, Any]]:
    """Read every unacknowledged finding from the app's validation report.

    cdk-nag writes ``validation-report.json`` into the cloud assembly
    directory (``app.outdir``) during ``app.synth()``. Returns one dict per
    finding with ``pack`` / ``rule`` / ``description`` / ``paths`` so the
    assertion message can point at exactly what fired and where.
    """
    report_path = Path(app.outdir) / "validation-report.json"
    if not report_path.exists():
        return []
    data = json.loads(report_path.read_text())
    findings: list[dict[str, Any]] = []
    for plugin_report in data.get("pluginReports", []):
        pack = plugin_report.get("pluginName", "?")
        for violation in plugin_report.get("violations", []):
            findings.append(
                {
                    "pack": pack,
                    "rule": violation.get("ruleName", "?"),
                    "description": violation.get("description", ""),
                    "paths": [
                        c.get("constructPath", "?")
                        for c in violation.get("violatingConstructs", [])
                    ],
                }
            )
    return findings


def _format_violations(findings: list[dict[str, Any]]) -> str:
    """Multi-line, deterministic summary suitable for a pytest assertion message."""
    lines: list[str] = []
    for f in sorted(findings, key=lambda x: (x["pack"], x["rule"], "".join(x["paths"]))):
        for path in f["paths"] or ["(no construct path)"]:
            lines.append(f"  [{f['pack']}] {f['rule']} at {path}")
        info = f["description"]
        if len(info) > 200:
            info = info[:197] + "..."
        lines.append(f"    -> {info}")
    return "\n".join(lines) if lines else "(no findings)"


class TestCdkNagCompliance:
    """End-to-end regression: ``app.synth()`` must produce zero
    unacknowledged cdk-nag findings across every representative
    configuration.

    When a test fails, the assertion message lists every finding by
    rule ID, resource path, and a short description — the same three
    pieces of information you'd need to either scope an existing
    acknowledgment or add a new one.
    """

    # The IAM-relevant config subset is shared with
    # ``tests/test_cdk_synthesis_matrix.py`` via
    # ``tests/_cdk_config_matrix.NAG_CONFIGS``. Only configs that
    # produce distinct IAM policy surfaces are included — the rest
    # (helm toggles, thresholds, etc.) don't change IAM and would
    # just burn CI time. See the module docstring in
    # ``_cdk_config_matrix.py`` for the rationale.
    CONFIGS: list[tuple[str, dict[str, Any]]] = _CONFIGS

    @pytest.mark.parametrize(("config_name", "overrides"), CONFIGS, ids=[c[0] for c in CONFIGS])
    def test_no_unsuppressed_findings(self, config_name: str, overrides: dict[str, Any]) -> None:
        app = _build_app(context_overrides=overrides)
        synthesize_app(app)

        findings = _collect_nag_violations(app)
        assert not findings, (
            f"cdk-nag found {len(findings)} unacknowledged finding(s) "
            f"with config {config_name!r}.\n\n{_format_violations(findings)}\n\n"
            f"Each finding either needs its underlying wildcard scoped "
            f"further or a targeted ``acknowledge_nag_findings`` entry with a "
            f"justification and an ``applies_to`` scoped to the "
            f"specific resource. Do NOT add broad ``Resource::*`` or "
            f"``Action::*`` entries — those defeat the whole point of "
            f"cdk-nag."
        )

    # An obviously-fake 12-digit account used only to force
    # environment-specific synthesis. It never reaches AWS — it just makes CDK
    # render account-bearing ARNs as a literal instead of the <AWS::AccountId>
    # pseudo-parameter.
    _CONCRETE_ACCOUNT = "123456789012"

    def test_no_unsuppressed_findings_with_concrete_account(self) -> None:
        """The full app must also synthesize with zero unacknowledged cdk-nag
        findings when the stacks are environment-specific (a concrete ``env``
        account).

        ``app.py`` resolves ``CDK_DEFAULT_ACCOUNT`` into every stack's ``env``
        so the regional VPC can enumerate the region's real AZ list. That flips
        account-bearing ARNs from the ``<AWS::AccountId>`` pseudo-parameter to a
        literal — exactly the rendering a ``cdk deploy`` uses, and one the
        agnostic ``test_no_unsuppressed_findings`` parametrization never
        exercises. This is the regression guard for suppressions whose
        ``applies_to`` previously matched only the pseudo-parameter form (see
        ``acknowledge_nag_findings`` in ``gco/stacks/nag_suppressions.py``). The
        account rendering is config-independent, so the default config suffices,
        and CI runs this in the ``default-regions`` cell of
        ``unit:cdk:nag-compliance``.
        """
        app = _build_app()
        synthesize_app(app, account=self._CONCRETE_ACCOUNT)

        findings = _collect_nag_violations(app)
        assert not findings, (
            f"cdk-nag found {len(findings)} unacknowledged finding(s) when synthesizing with "
            f"a concrete env account ({self._CONCRETE_ACCOUNT}).\n\n"
            f"{_format_violations(findings)}\n\n"
            f"These do NOT appear in the environment-agnostic matrix because account-bearing "
            f"ARNs render as the <AWS::AccountId> pseudo-parameter there, but as a literal "
            f"account id under a real ``cdk deploy``. Fix by making the acknowledgment's "
            f"applies_to match both renderings — ``acknowledge_nag_findings`` registers both "
            f"forms when the stack account is concrete."
        )
