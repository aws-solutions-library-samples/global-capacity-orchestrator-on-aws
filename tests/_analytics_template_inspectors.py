"""Template inspectors for the analytics-environment property tests.

Four pure helpers over a synthesized CloudFormation template dict:

* :func:`collect_job_pod_role_statements` — walks every ``AWS::IAM::Role``
  + ``AWS::IAM::Policy`` in a regional template, filters on role name
  containing ``service-account`` / ``ServiceAccount`` (the EKS Pod
  Identity role created in ``GCORegionalStack._create_service_account_role``
  used by every pod in ``gco-jobs``), and returns every policy-document
  statement attached to it. The bucket-isolation property test scans
  these for the S3 ARN allow-list and deny-list assertions.

* :func:`get_kubectl_replacements` — extracts the ``ImageReplacements``
  property from the regional stack's kubectl-applier CustomResource and
  returns it as a flat dict. Used by the cluster-shared ConfigMap
  property test and helpful for the toggle-invariant test to
  cross-check the ConfigMap is present.

* :func:`collect_sagemaker_role_statements` — walks the analytics stack
  and returns every policy-document statement attached to the SageMaker
  execution role (role_name starts with ``AmazonSageMaker``, logical id
  contains ``Sagemaker`` + ``ExecutionRole``). The toggle-invariant test
  uses this to assert the cluster-shared grant's presence under an
  enabled toggle.

* :func:`get_sagemaker_role_actions` — convenience wrapper over
  :func:`collect_sagemaker_role_statements` returning the deduped set of
  action strings. The round-trip property test consumes this to derive
  the HyperPod toggle from the synthesized template.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Shared: normalize statement.Action / statement.Resource to list[str].
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    """Return ``value`` as a list. IAM policy elements may be a string,
    a dict (for tokens), or a list of either. Everything downstream
    expects a list.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _extract_resources(statement: dict[str, Any]) -> list[Any]:
    """Return the statement's ``Resource`` entries as a list.

    A resource entry may be a literal ARN string, a CloudFormation
    intrinsic-function dict (``{"Fn::GetAtt": [...]}``, ``{"Ref": "..."}``,
    ``{"Fn::Join": [...]}``, ``{"Fn::Sub": "..."}``), or a list mixing the
    two. This helper flattens each entry to a string form callers can
    pattern-match against.
    """
    return _as_list(statement.get("Resource"))


def _extract_actions(statement: dict[str, Any]) -> list[str]:
    """Return the statement's ``Action`` entries as a list of strings.

    Actions are always string literals in practice — CDK never emits
    a token for an IAM action — but we defensively coerce non-strings to
    their ``repr`` so the caller doesn't crash on a malformed template.
    """
    actions = _as_list(statement.get("Action"))
    return [a if isinstance(a, str) else repr(a) for a in actions]


# ---------------------------------------------------------------------------
# Role + Policy walker.
# ---------------------------------------------------------------------------


def _resources_of_type(template: dict[str, Any], resource_type: str) -> dict[str, dict[str, Any]]:
    """Return ``{logical_id: resource_dict}`` for every resource of the
    given CloudFormation type in ``template``. Returns an empty dict if
    the template has no ``Resources`` section.
    """
    resources: dict[str, Any] = template.get("Resources", {}) or {}
    return {
        lid: res
        for lid, res in resources.items()
        if isinstance(res, dict) and res.get("Type") == resource_type
    }


def _logical_ids_matching(template: dict[str, Any], name_fragments: tuple[str, ...]) -> set[str]:
    """Return the set of ``AWS::IAM::Role`` logical ids whose logical id
    (the CDK construct id) contains any of ``name_fragments``
    case-insensitively.

    We use the logical id rather than the ``RoleName`` property because
    many roles have ``RoleName`` omitted (CDK auto-generates the name
    at deploy time) but all roles have a logical id, and the CDK
    construct id is always emitted as the logical id.
    """
    matches: set[str] = set()
    for lid in _resources_of_type(template, "AWS::IAM::Role"):
        lower = lid.lower()
        if any(frag.lower() in lower for frag in name_fragments):
            matches.add(lid)
    return matches


def _policy_targets_role(policy_res: dict[str, Any], role_lids: set[str]) -> bool:
    """Return True if the ``AWS::IAM::Policy`` resource attaches to any
    of the given role logical ids.

    Policy ``Roles`` is a list of references; each reference is a ``Ref``
    dict whose value is the role's logical id.
    """
    roles = policy_res.get("Properties", {}).get("Roles", []) or []
    return any(isinstance(ref, dict) and ref.get("Ref") in role_lids for ref in roles)


def _extract_inline_policy_statements(
    role_res: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return every statement from every inline ``Policies`` entry on the
    role. An inline policy is a dict with ``PolicyName`` +
    ``PolicyDocument.Statement``.
    """
    statements: list[dict[str, Any]] = []
    inline = role_res.get("Properties", {}).get("Policies", []) or []
    for policy in inline:
        if not isinstance(policy, dict):
            continue
        doc = policy.get("PolicyDocument") or {}
        for st in _as_list(doc.get("Statement")):
            if isinstance(st, dict):
                statements.append(st)
    return statements


def _extract_managed_policy_statements(
    template: dict[str, Any], role_lids: set[str]
) -> list[dict[str, Any]]:
    """Walk every ``AWS::IAM::Policy`` that attaches to any of
    ``role_lids`` and return the union of their statements.
    """
    statements: list[dict[str, Any]] = []
    for _pol_lid, pol_res in _resources_of_type(template, "AWS::IAM::Policy").items():
        if _policy_targets_role(pol_res, role_lids):
            doc = pol_res.get("Properties", {}).get("PolicyDocument") or {}
            for st in _as_list(doc.get("Statement")):
                if isinstance(st, dict):
                    statements.append(st)
    return statements


# ---------------------------------------------------------------------------
# Public API: collect_job_pod_role_statements
# ---------------------------------------------------------------------------


def collect_job_pod_role_statements(
    template: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return every IAM policy-document statement attached to the regional
    job-pod / service-account role in ``template``.

    Walks both inline ``Policies`` on the role resource itself and every
    standalone ``AWS::IAM::Policy`` whose ``Roles`` list references the
    role. The role is identified by its CDK construct id containing
    ``service-account`` or ``ServiceAccount`` (which matches
    ``ServiceAccountRole`` from
    ``GCORegionalStack._create_service_account_role`` and any future
    sibling role with the same logical-id fragment).

    Args:
        template: A CloudFormation template dict, typically an entry
            from the dict returned by
            :func:`tests._analytics_cdk_overlays.synth_all_stacks`.

    Returns:
        Every statement dict attached to the role. Order is
        inline-first then attached-policies in template order. Non-role
        templates (global, api-gateway, monitoring, analytics) return an
        empty list.
    """
    role_lids = _logical_ids_matching(template, ("service-account", "ServiceAccount"))
    if not role_lids:
        return []

    statements: list[dict[str, Any]] = []
    for role_lid in role_lids:
        role_res = template["Resources"][role_lid]
        statements.extend(_extract_inline_policy_statements(role_res))

    statements.extend(_extract_managed_policy_statements(template, role_lids))
    return statements


# ---------------------------------------------------------------------------
# Public API: get_kubectl_replacements
# ---------------------------------------------------------------------------


def get_kubectl_replacements(
    template: dict[str, Any],
) -> dict[str, Any]:
    """Return the ``ImageReplacements`` property from the regional stack's
    convergence-trigger CustomResource as a flat dict.

    The ``HelmInstallCharts`` trigger CustomResource carries the manifest
    ``ImageReplacements`` that the convergence state machine's base and
    post-Helm kubectl tasks consume. Returns an empty dict if no matching
    resource is present in the template.

    Args:
        template: A regional CloudFormation template dict.

    Returns:
        The ``ImageReplacements`` property as a dict. Values may be
        strings (literal replacements) or dicts (CloudFormation intrinsic
        functions like ``Fn::GetAtt``).
    """
    resources: dict[str, Any] = template.get("Resources", {}) or {}

    # Prefer the well-known logical id first to keep the walk stable.
    trigger = resources.get("HelmInstallCharts")
    if isinstance(trigger, dict):
        return dict(trigger.get("Properties", {}).get("ImageReplacements") or {})

    # Fallback: any custom resource exposing an ImageReplacements property.
    for _lid, res in resources.items():
        if not isinstance(res, dict):
            continue
        rtype = res.get("Type", "")
        if rtype == "AWS::CloudFormation::CustomResource" or rtype.startswith("Custom::"):
            replacements = res.get("Properties", {}).get("ImageReplacements")
            if isinstance(replacements, dict):
                return dict(replacements)
    return {}


# ---------------------------------------------------------------------------
# Public API: collect_sagemaker_role_statements
# ---------------------------------------------------------------------------


def _sagemaker_role_logical_ids(template: dict[str, Any]) -> set[str]:
    """Return the set of SageMaker-execution-role logical ids in ``template``.

    A role is classified as SageMaker-execution if either its logical id
    contains both ``sagemaker`` and ``executionrole`` (case-insensitive)
    *or* its ``RoleName`` property starts with the documented
    ``AmazonSageMaker`` prefix. The logical-id path catches
    the current CDK construct id ``SagemakerExecutionRole``; the
    role-name path catches future rewrites that keep the naming
    contract but rename the construct.
    """
    role_lids: set[str] = set()
    for lid, res in _resources_of_type(template, "AWS::IAM::Role").items():
        lower = lid.lower()
        if "sagemaker" in lower and "executionrole" in lower:
            role_lids.add(lid)
            continue
        role_name = res.get("Properties", {}).get("RoleName")
        if isinstance(role_name, str) and role_name.startswith("AmazonSageMaker"):
            role_lids.add(lid)
    return role_lids


def collect_sagemaker_role_statements(
    template: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return every IAM policy-document statement attached to the analytics
    stack's ``SageMaker_Execution_Role`` in ``template``.

    Walks both the inline ``Policies`` on the role resource itself and
    every standalone ``AWS::IAM::Policy`` whose ``Roles`` list references
    the role. The role is identified by either its CDK construct id
    (logical id contains ``sagemaker`` + ``executionrole``) or its
    ``RoleName`` property beginning with ``AmazonSageMaker`` (the
    documented naming contract). Returns an empty list for non-analytics
    templates.

    Args:
        template: A CloudFormation template dict, typically the
            ``gco-analytics`` entry in the dict returned by
            :func:`tests._analytics_cdk_overlays.synth_all_stacks`.

    Returns:
        Every statement dict attached to the SageMaker execution role.
        Order is inline-first then attached-policies in template order.
    """
    role_lids = _sagemaker_role_logical_ids(template)
    if not role_lids:
        return []

    statements: list[dict[str, Any]] = []
    for role_lid in role_lids:
        role_res = template["Resources"][role_lid]
        statements.extend(_extract_inline_policy_statements(role_res))

    statements.extend(_extract_managed_policy_statements(template, role_lids))
    return statements


# ---------------------------------------------------------------------------
# Public API: get_sagemaker_role_actions
# ---------------------------------------------------------------------------


def get_sagemaker_role_actions(template: dict[str, Any]) -> list[str]:
    """Return every unique IAM action string granted to the
    ``SageMaker_Execution_Role`` in the analytics template.

    Thin wrapper over :func:`collect_sagemaker_role_statements` — the
    deduped / sorted action set is what the round-trip / HyperPod
    deriver walks.

    Returns:
        A list of unique action strings, sorted for determinism. Returns
        an empty list if no matching role is present (e.g. the analytics
        stack is absent because the toggle is off, or the caller passed a
        non-analytics template).
    """
    statements = collect_sagemaker_role_statements(template)
    actions: set[str] = set()
    for st in statements:
        actions.update(_extract_actions(st))
    return sorted(actions)
