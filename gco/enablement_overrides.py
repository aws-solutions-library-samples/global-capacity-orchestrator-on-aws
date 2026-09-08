"""Run-scoped enablement overrides shared by the CLI and the recorders.

GCO ships every optional add-on **off** in ``cdk.json`` because each one
carries real recurring cost (an FSx for Lustre filesystem has a 1.2 TiB
provisioned floor; Aurora Serverless v2 keeps a writer and a reader; Valkey
Serverless bills storage and ECPUs). Two CDK context keys exist so a single
run can force those features on *without rewriting the committed config*:

``feature_enabled_overrides``
    Infrastructure blocks whose top-level ``enabled`` flag may be forced on
    (see ``gco.config.config_loader.parse_feature_enabled_overrides``).

``helm_enabled_overrides``
    ``helm`` block keys whose chart may be forced on (see
    ``gco.stacks.regional_stack._parse_helm_enabled_overrides``).

Both consumers live in modules that import ``aws_cdk``. The CLI must be able
to validate ``--enable`` names and build the ``--context`` pairs *before* the
CDK Python toolchain is known to be importable — ``StackManager._run_cdk``
deliberately fails with an actionable message in that case rather than an
``ImportError`` at startup. So the canonical name sets live here, in a module
with no third-party imports, and ``tests/test_enablement_overrides.py`` pins
them in lockstep with the two authoritative parsers.

Overrides are one-way: they can only *enable*. A feature an operator turned
off stays off unless it is named explicitly.
"""

from __future__ import annotations

from collections.abc import Iterable

#: CDK context key carrying forced-on infrastructure feature blocks.
FEATURE_OVERRIDE_CONTEXT_KEY = "feature_enabled_overrides"

#: CDK context key carrying forced-on Helm chart keys.
HELM_OVERRIDE_CONTEXT_KEY = "helm_enabled_overrides"

#: cdk.json blocks ``feature_enabled_overrides`` accepts. Lockstep with
#: ``gco.config.config_loader.FEATURE_OVERRIDE_KEYS``.
FEATURE_OVERRIDE_KEYS = frozenset({"aurora_pgvector", "valkey", "fsx_lustre", "vector_store"})

#: cdk.json ``helm`` block keys ``helm_enabled_overrides`` accepts. Lockstep
#: with ``gco.stacks.regional_stack._HELM_CHART_CONFIG_KEYS``.
HELM_CHART_CONFIG_KEYS = frozenset(
    {
        "aws_load_balancer_controller",
        "keda",
        "aws_efa_device_plugin",
        "aws_neuron_device_plugin",
        "volcano",
        "kuberay",
        "cert_manager",
        "slurm",
        "yunikorn",
        "kubeflow_trainer",
        "kueue",
    }
)


class EnablementOverrideError(ValueError):
    """Raised when a requested override name is not a known feature or chart."""


def split_override_names(raw: Iterable[str]) -> list[str]:
    """Flatten repeated and comma-joined ``--enable`` values into bare names.

    Accepts the two shapes a caller can produce interchangeably —
    ``--enable valkey --enable slurm`` and ``--enable valkey,slurm`` — and
    drops empty segments so a trailing comma is not an error. Order is
    preserved for the caller's benefit; duplicates are left intact because
    :func:`route_enablement_overrides` de-duplicates when it groups.
    """
    names: list[str] = []
    for entry in raw:
        for part in entry.split(","):
            candidate = part.strip()
            if candidate:
                names.append(candidate)
    return names


def route_enablement_overrides(raw: Iterable[str]) -> dict[str, str]:
    """Group requested names into the CDK ``--context`` pairs they belong to.

    The two namespaces are disjoint, so each name routes unambiguously: a
    caller says ``--enable fsx_lustre,slurm`` and gets both
    ``feature_enabled_overrides=fsx_lustre`` and
    ``helm_enabled_overrides=slurm``. Names are sorted so the resulting
    context is byte-identical for any input ordering, which keeps a resumed
    or re-recorded run's argv stable.

    Returns an empty mapping when nothing was requested, so callers can pass
    the result straight to ``StackManager.set_extra_cdk_context``.

    Raises:
        EnablementOverrideError: if any name is neither a known feature block
            nor a known Helm chart key. Failing here — before any AWS call —
            means a typo can never silently deploy without the feature the
            operator asked for.
    """
    requested = set(split_override_names(raw))
    unknown = sorted(requested - FEATURE_OVERRIDE_KEYS - HELM_CHART_CONFIG_KEYS)
    if unknown:
        valid = ", ".join(sorted(FEATURE_OVERRIDE_KEYS | HELM_CHART_CONFIG_KEYS))
        raise EnablementOverrideError(
            f"Unknown --enable name(s): {', '.join(unknown)}. Valid: {valid}"
        )

    context: dict[str, str] = {}
    features = sorted(requested & FEATURE_OVERRIDE_KEYS)
    charts = sorted(requested & HELM_CHART_CONFIG_KEYS)
    if features:
        context[FEATURE_OVERRIDE_CONTEXT_KEY] = ",".join(features)
    if charts:
        context[HELM_OVERRIDE_CONTEXT_KEY] = ",".join(charts)
    return context
