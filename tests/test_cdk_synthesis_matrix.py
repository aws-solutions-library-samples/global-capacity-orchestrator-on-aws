"""CDK configuration-matrix synthesis test.

Parameterized over every entry in ``tests/_cdk_config_matrix.CONFIGS``
(the full set a user can pick from cdk.json — multi-region, FSx
toggles, analytics fixtures, and so on).

What this replaces
------------------
The previous workflow invoked the standalone script, which:
    * rewrote ``cdk.json`` in-place for each config (race-prone; can't
      parallelize without a lock),
    * shelled out to ``cdk synth --quiet`` 24 times, paying the
      Node.js + JSII cold start per invocation.

Running the same configs as an in-process pytest gives us:
    * serial execution in CI, avoiding CDK's shared in-process asset-staging
      cache races while still removing subprocess cold-start overhead,
    * context passed via ``cdk.App(context=…)`` — the supported
      CDK-level injection point, no file mutation,
    * the same builder and Docker image stand-in as ``test_nag_compliance.py``
      (``tests/_cdk_app_builder.py``), so Docker isn't required on the
      runner.

What this does *not* replace
----------------------------
``test_nag_compliance.py`` parameterizes over ``NAG_CONFIGS`` — a
curated IAM-relevant subset — and asserts zero unsuppressed cdk-nag
findings. That test stays as-is. This file asserts that ``app.synth()``
completes without raising for every config in ``CONFIGS``, mirroring what
the old script checked, plus two template invariants cdk-nag cannot see:
every Secrets Manager grant names exactly one secret, and the API Gateway
auth secret's grants target the Region that secret lives in; and every
CloudWatch alarm reads metrics of, and notifies a topic in, its own Region.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aws_cdk as cdk
import pytest

from tests._cdk_app_builder import load_context, synthesize_app
from tests._cdk_config_matrix import CONFIGS

#: One ARN field as ``_render`` prints it: a literal, or a pseudo parameter such
#: as ``<AWS::AccountId>``, whose own colons a plain ``[^:]+`` would stop at.
_ARN_FIELD = r"(?:<[^>]+>|[^:<>]+)"
#: A secret ARN that names one secret: the six random characters Secrets Manager
#: appends either spelled out or matched with ``-??????``. A trailing ``*`` would
#: also match every other secret whose name starts the same way.
_SECRET_ARN = re.compile(
    rf"arn:{_ARN_FIELD}:secretsmanager:(?P<region>{_ARN_FIELD}):{_ARN_FIELD}:secret:"
    r"(?P<name>.+)-(?:\?{6}|[A-Za-z0-9]{6})"
)
#: CloudFormation functions that resolve to one secret's complete ARN.
_EXACT_REFERENCES = frozenset({"Ref", "Fn::GetAtt", "Fn::ImportValue"})


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _render(value: Any) -> str:
    """Flatten a CloudFormation value the way cdk-nag prints one in a finding.

    ``Fn::Join`` parts are joined, ``Ref`` becomes ``<Name>`` and ``Fn::GetAtt``
    ``<Name.Attr>``; anything else renders as compact JSON.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and len(value) == 1:
        ((function, argument),) = value.items()
        if function == "Fn::Join":
            separator, parts = argument
            return separator.join(_render(part) for part in parts)
        if function == "Ref":
            return f"<{argument}>"
        if function == "Fn::GetAtt":
            return f"<{'.'.join(argument) if isinstance(argument, list) else argument}>"
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _secret_grants(assembly: Any) -> list[tuple[str, Any]]:
    """``(location, resource)`` for every resource an Allow statement grants a
    Secrets Manager action on, from managed and inline policies alike."""
    grants: list[tuple[str, Any]] = []
    for stack in assembly.stacks:
        for logical_id, resource in (stack.template.get("Resources") or {}).items():
            properties = resource.get("Properties") or {}
            if resource.get("Type") in {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}:
                documents = [properties.get("PolicyDocument") or {}]
            elif resource.get("Type") == "AWS::IAM::Role":
                documents = [
                    policy.get("PolicyDocument") or {}
                    for policy in properties.get("Policies") or []
                ]
            else:
                continue
            for document in documents:
                for statement in _as_list(document.get("Statement")):
                    actions = _as_list(statement.get("Action"))
                    if statement.get("Effect") != "Allow" or not any(
                        str(action).startswith("secretsmanager:") for action in actions
                    ):
                        continue
                    grants.extend(
                        (f"{stack.stack_name}/{logical_id}", granted)
                        for granted in _as_list(statement.get("Resource"))
                    )
    return grants


def _secret_grant_problems(
    assembly: Any, *, auth_secret: str, api_gateway_region: str
) -> list[str]:
    """Every Secrets Manager grant in ``assembly`` that does not name one secret
    in the Region it lives in, described for an assertion message.

    A grant is either an exact reference to a secret (``Ref``, ``Fn::GetAtt``,
    ``Fn::ImportValue``), an ARN ending in the six-character suffix or in
    ``-??????``, or ``*`` (actions such as ``GetRandomPassword`` that take no
    resource, covered by cdk-nag's IAM5 and the role's acknowledgment). The API
    Gateway auth secret is read from other stacks through an ARN built from its
    name, which must use the API Gateway Region: ``deployment_regions`` lets that
    differ from the global Region (``api-gateway-region-split``), where a grant in
    the wrong Region synthesizes cleanly and then denies every read. cdk-nag
    cannot catch that, because only ``*`` counts as a wildcard to IAM5.
    """
    problems = []
    for location, granted in _secret_grants(assembly):
        rendered = _render(granted)
        if rendered == "*" or (isinstance(granted, dict) and set(granted) <= _EXACT_REFERENCES):
            continue
        match = _SECRET_ARN.fullmatch(rendered)
        if match is None:
            problems.append(f"{location}: {rendered} does not name exactly one secret")
        elif match["name"] == auth_secret and match["region"] != api_gateway_region:
            problems.append(
                f"{location}: {rendered} reads the auth secret outside the API Gateway "
                f"Region {api_gateway_region}, where the API Gateway stack creates it"
            )
    return problems


def _fake_assembly(*resources: Any) -> SimpleNamespace:
    """A one-stack assembly holding one IAM policy per resource value."""
    template = {
        "Resources": {
            f"Policy{index}": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": ["secretsmanager:GetSecretValue"],
                                "Resource": resource,
                            }
                        ]
                    }
                },
            }
            for index, resource in enumerate(resources)
        }
    }
    return SimpleNamespace(stacks=[SimpleNamespace(stack_name="gco-us-east-1", template=template)])


def _joined_arn(region: str, name: str) -> dict[str, Any]:
    """An ARN the way a stack builds it from ``self.partition`` and ``self.account``."""
    return {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                f":secretsmanager:{region}:",
                {"Ref": "AWS::AccountId"},
                f":secret:{name}",
            ],
        ]
    }


_AUTH_SECRET = "gco/api-gateway-auth-token"


@pytest.mark.parametrize(
    ("resource", "problem"),
    [
        (_joined_arn("us-west-2", f"{_AUTH_SECRET}-??????"), None),
        ({"Ref": "GCOAuthSecret12345678"}, None),
        ({"Fn::ImportValue": "gco-api-gateway:ExportsOutputRefGCOAuthSecret1234"}, None),
        ("arn:aws:secretsmanager:eu-west-1:123456789012:secret:other-AbCd12", None),
        ("*", None),
        (_joined_arn("us-west-2", f"{_AUTH_SECRET}*"), "does not name exactly one secret"),
        (_joined_arn("us-west-2", _AUTH_SECRET), "does not name exactly one secret"),
        (
            {
                "Fn::Join": [
                    "",
                    [{"Fn::ImportValue": "gco-api-gateway:ExportsOutputRefGCOAuthSecret1"}, "*"],
                ]
            },
            "does not name exactly one secret",
        ),
        (_joined_arn("us-east-2", f"{_AUTH_SECRET}-??????"), "outside the API Gateway Region"),
    ],
    ids=[
        "deterministic",
        "same-stack-ref",
        "import",
        "literal-full-arn",
        "bare-star",
        "trailing-star",
        "partial-arn",
        "import-star",
        "global-region",
    ],
)
def test_secret_grant_check(resource: Any, problem: str | None) -> None:
    """The template check behind ``test_synth_succeeds`` accepts each exact form
    and rejects each wildcard or wrong-Region one (here us-west-2 is the API
    Gateway Region), so it cannot pass vacuously."""
    problems = _secret_grant_problems(
        _fake_assembly(resource), auth_secret=_AUTH_SECRET, api_gateway_region="us-west-2"
    )
    if problem is None:
        assert problems == []
    else:
        assert len(problems) == 1 and problem in problems[0], problems


#: Resource types an alarm dimension can name by a literal physical name, and
#: the property that holds the name.
_NAMED_RESOURCES = {
    "AWS::ApiGateway::RestApi": "Name",
    "AWS::DynamoDB::Table": "TableName",
    "AWS::EKS::Cluster": "Name",
    "AWS::Lambda::Function": "FunctionName",
    "AWS::SQS::Queue": "QueueName",
}
#: The custom resource through which CDK reads a value from another Region.
_CROSS_REGION_READER = "Custom::CrossRegionExportReader"
_ALARM_TYPES = frozenset({"AWS::CloudWatch::Alarm", "AWS::CloudWatch::CompositeAlarm"})
_ACTION_KEYS = ("AlarmActions", "OKActions", "InsufficientDataActions")


def _alarm_metrics(properties: dict[str, Any]) -> list[dict[str, Any]]:
    """The metrics an alarm reads: its single metric, or each metric of its math."""
    if "Namespace" in properties:
        return [properties]
    return [
        query["MetricStat"]["Metric"]
        for query in properties.get("Metrics") or []
        if "MetricStat" in query
    ]


def _alarm_region_problems(assembly: Any, *, publisher_regions: Mapping[str, str]) -> list[str]:
    """Every alarm that reads a metric, or notifies a topic, outside its own Region.

    CloudWatch evaluates an alarm only against metrics of its own Region and an
    alarm's SNS action must be a topic of that Region, so an alarm on another
    Region's resource synthesizes and deploys cleanly and then never leaves
    ``OK``. A metric's Region is that of the resource one of its dimensions
    names: a ``Ref`` or ``Fn::GetAtt`` is the alarm's own stack, an
    ``Fn::ImportValue`` its Region (CloudFormation exports are regional), CDK's
    cross-Region reader another Region, and a literal the Region of every
    resource deployed under that physical name. A metric whose dimensions name
    no resource must be in a namespace of ``publisher_regions``, which says
    where it is published; any other namespace is a problem, so a new alarm
    cannot pass unchecked.
    """
    names: dict[str, set[str]] = {}
    for stack in assembly.stacks:
        for resource in (stack.template.get("Resources") or {}).values():
            name_property = _NAMED_RESOURCES.get(resource.get("Type"), "")
            name = (resource.get("Properties") or {}).get(name_property)
            if isinstance(name, str):
                names.setdefault(name, set()).add(stack.environment.region)

    problems = []
    for stack in assembly.stacks:
        region = stack.environment.region
        resources = stack.template.get("Resources") or {}

        def regions_of(value: Any, *, own_region: str = region, own: dict = resources) -> Any:
            """The Regions ``value`` places its resource in, or None if it names none."""
            if isinstance(value, str):
                return names.get(value)
            if isinstance(value, dict) and len(value) == 1:
                ((function, argument),) = value.items()
                if function == "Fn::ImportValue":
                    return {own_region}
                if function in {"Ref", "Fn::GetAtt"}:
                    target = argument if isinstance(argument, str) else argument[0]
                    if (own.get(target) or {}).get("Type") == _CROSS_REGION_READER:
                        return {"another Region"}
                    return {own_region}
            return None

        for logical_id, resource in resources.items():
            if resource.get("Type") not in _ALARM_TYPES:
                continue
            where = f"{stack.stack_name}/{logical_id}"
            properties = resource.get("Properties") or {}
            for metric in _alarm_metrics(properties):
                namespace = metric.get("Namespace")
                sources = [
                    found
                    for dimension in metric.get("Dimensions") or []
                    if (found := regions_of(dimension.get("Value"))) is not None
                ]
                if not sources:
                    publisher = publisher_regions.get(namespace)
                    if publisher is None:
                        problems.append(
                            f"{where}: no dimension of its {namespace} metric names a resource "
                            "and the namespace has no known publisher Region"
                        )
                    elif publisher != region:
                        problems.append(f"{where}: {namespace} is published in {publisher}")
                problems.extend(
                    f"{where}: reads {namespace} data from {', '.join(sorted(found))}"
                    for found in sources
                    if region not in found
                )
            for key in _ACTION_KEYS:
                for action in properties.get(key) or []:
                    found = regions_of(action)
                    if found is None:
                        match = re.search(r":sns:([^:]+):", _render(action))
                        found = {match[1].replace("<AWS::Region>", region)} if match else set()
                    if region not in found:
                        problems.append(f"{where}: notifies {_render(action)} in another Region")
    return problems


def _alarm_template(region: str, *resources: tuple[str, dict[str, Any]]) -> SimpleNamespace:
    """One synthesized stack in ``region`` holding ``(logical id, resource)`` pairs."""
    return SimpleNamespace(
        stack_name=f"gco-{region}",
        environment=SimpleNamespace(region=region),
        template={"Resources": dict(resources)},
    )


def _alarm(namespace: str, dimensions: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A single-metric alarm on ``namespace`` with the given dimension values."""
    return {
        "Type": "AWS::CloudWatch::Alarm",
        "Properties": {
            "Namespace": namespace,
            "MetricName": "Errors",
            "Dimensions": [{"Name": key, "Value": value} for key, value in dimensions.items()],
            **extra,
        },
    }


_TOPIC = ("Topic", {"Type": "AWS::SNS::Topic"})
_REST_API = ("Api", {"Type": "AWS::ApiGateway::RestApi", "Properties": {"Name": "gco-global-api"}})
_READER = ("ExportsReader", {"Type": _CROSS_REGION_READER})


@pytest.mark.parametrize(
    ("stacks", "problem"),
    [
        (
            [
                _alarm_template(
                    "us-east-2",
                    _REST_API,
                    _TOPIC,
                    (
                        "A",
                        _alarm(
                            "AWS/ApiGateway",
                            {"ApiName": "gco-global-api"},
                            AlarmActions=[{"Ref": "Topic"}],
                        ),
                    ),
                )
            ],
            None,
        ),
        (
            [
                _alarm_template(
                    "us-east-2",
                    (
                        "A",
                        _alarm(
                            "AWS/Lambda",
                            {"FunctionName": {"Fn::ImportValue": "gco-api-gateway:Proxy"}},
                        ),
                    ),
                )
            ],
            None,
        ),
        (
            [
                _alarm_template(
                    "us-east-2",
                    (
                        "A",
                        {
                            "Type": "AWS::CloudWatch::Alarm",
                            "Properties": {
                                "Metrics": [
                                    {"Id": "e", "Expression": "SUM(METRICS())"},
                                    {
                                        "Id": "m0",
                                        "MetricStat": {
                                            "Metric": {
                                                "Namespace": "AWS/DynamoDB",
                                                "Dimensions": [
                                                    {"Name": "Operation", "Value": "GetItem"},
                                                    {
                                                        "Name": "TableName",
                                                        "Value": {"Ref": "Table"},
                                                    },
                                                ],
                                            }
                                        },
                                    },
                                ]
                            },
                        },
                    ),
                )
            ],
            None,
        ),
        ([_alarm_template("us-west-2", ("A", _alarm("GCO/BackendTLS", {"Project": "gco"})))], None),
        (
            [
                _alarm_template(
                    "us-east-2",
                    _REST_API,
                    (
                        "A",
                        _alarm(
                            "AWS/ApiGateway",
                            {"ApiName": "gco-global-api"},
                            AlarmActions=["arn:aws:sns:us-east-2:123456789012:alerts"],
                        ),
                    ),
                )
            ],
            None,
        ),
        (
            [
                _alarm_template("us-west-2", _REST_API),
                _alarm_template(
                    "us-east-2", ("A", _alarm("AWS/ApiGateway", {"ApiName": "gco-global-api"}))
                ),
            ],
            "reads AWS/ApiGateway data from us-west-2",
        ),
        (
            [
                _alarm_template(
                    "us-east-2",
                    _READER,
                    (
                        "A",
                        _alarm(
                            "AWS/Lambda",
                            {
                                "FunctionName": {
                                    "Fn::GetAtt": ["ExportsReader", "/cdk/exports/proxy"]
                                }
                            },
                        ),
                    ),
                )
            ],
            "reads AWS/Lambda data from another Region",
        ),
        (
            [_alarm_template("us-east-2", ("A", _alarm("GCO/BackendTLS", {"Project": "gco"})))],
            "GCO/BackendTLS is published in us-west-2",
        ),
        (
            [_alarm_template("us-east-2", ("A", _alarm("GCO/Unknown", {"Project": "gco"})))],
            "has no known publisher Region",
        ),
        (
            [
                _alarm_template(
                    "us-east-2",
                    _REST_API,
                    _READER,
                    (
                        "A",
                        _alarm(
                            "AWS/ApiGateway",
                            {"ApiName": "gco-global-api"},
                            AlarmActions=[{"Fn::GetAtt": ["ExportsReader", "/cdk/exports/topic"]}],
                        ),
                    ),
                )
            ],
            "notifies",
        ),
        (
            [
                _alarm_template(
                    "us-east-2",
                    _REST_API,
                    (
                        "A",
                        _alarm(
                            "AWS/ApiGateway",
                            {"ApiName": "gco-global-api"},
                            OKActions=["arn:aws:sns:us-west-2:123456789012:alerts"],
                        ),
                    ),
                )
            ],
            "notifies arn:aws:sns:us-west-2",
        ),
        (
            [
                _alarm_template(
                    "us-east-2",
                    (
                        "A",
                        {
                            "Type": "AWS::CloudWatch::CompositeAlarm",
                            "Properties": {
                                "AlarmActions": [
                                    {
                                        "Fn::Join": [
                                            "",
                                            [
                                                "arn:aws:sns:us-west-2:",
                                                {"Ref": "AWS::AccountId"},
                                                ":alerts",
                                            ],
                                        ]
                                    }
                                ]
                            },
                        },
                    ),
                )
            ],
            "notifies",
        ),
    ],
    ids=[
        "literal-name-same-region",
        "import",
        "metric-math-ref",
        "known-publisher",
        "literal-topic-arn",
        "literal-name-other-region",
        "cross-region-reader",
        "publisher-other-region",
        "unknown-publisher",
        "cross-region-topic",
        "literal-topic-other-region",
        "joined-topic-other-region",
    ],
)
def test_alarm_region_check(stacks: list[SimpleNamespace], problem: str | None) -> None:
    """The template check behind ``test_synth_succeeds`` accepts each same-Region
    form and rejects each other-Region one (``GCO/BackendTLS`` is published in
    us-west-2 here), so it cannot pass vacuously."""
    problems = _alarm_region_problems(
        SimpleNamespace(stacks=stacks), publisher_regions={"GCO/BackendTLS": "us-west-2"}
    )
    if problem is None:
        assert problems == []
    else:
        assert len(problems) == 1 and problem in problems[0], problems


@pytest.mark.parametrize(("config_name", "overrides"), CONFIGS, ids=[c[0] for c in CONFIGS])
def test_synth_succeeds(config_name: str, overrides: dict[str, Any]) -> None:
    """Every config in the shared matrix must synthesize without raising; every
    Secrets Manager grant it renders must name exactly one secret, in the Region
    that secret lives in (see ``_secret_grant_problems``); and every alarm must
    read metrics of, and notify a topic in, its own Region
    (see ``_alarm_region_problems``)."""
    from gco.config.config_loader import ConfigLoader
    from gco.stacks.constants import api_gateway_auth_secret_name

    app = cdk.App(context=load_context(overrides))
    assembly = synthesize_app(app)

    config = ConfigLoader(app)
    problems = _secret_grant_problems(
        assembly,
        auth_secret=api_gateway_auth_secret_name(config.get_project_name()),
        api_gateway_region=config.get_api_gateway_region(),
    )
    assert not problems, "Secrets Manager grants that IAM would scope wrongly:\n" + "\n".join(
        problems
    )
    # The backend TLS certificate manager, in the API Gateway stack, publishes
    # GCO/BackendTLS from that Region (lambda/tls-certificate-manager/handler.py).
    alarm_problems = _alarm_region_problems(
        assembly, publisher_regions={"GCO/BackendTLS": config.get_api_gateway_region()}
    )
    assert not alarm_problems, "Alarms outside the Region of their data:\n" + "\n".join(
        alarm_problems
    )


def test_every_config_knob_is_matrix_covered() -> None:
    """Every context key the loader reads is varied by the matrix or allowlisted.

    The knob inventory is extracted from ``gco/config/config_loader.py``'s
    AST (every ``try_get_context("...")`` literal — the loader's real read
    surface, not cdk.json's incidental contents), so a new configuration
    block cannot ship without either a synthesis-matrix entry exercising it
    or an explicit justification in ``MATRIX_COVERAGE_ALLOWLIST``. This
    guard exists because the matrix had silently drifted 11-of-22 knobs
    behind the loader: ``global_accelerator``, ``historical``,
    ``vector_store``, ``backend_tls``, and others had never synthesized off
    their defaults.
    """
    import ast

    import gco.config.config_loader as loader_module
    from tests._cdk_config_matrix import MATRIX_COVERAGE_ALLOWLIST

    loader_path = Path(loader_module.__file__)
    tree = ast.parse(loader_path.read_text(encoding="utf-8"))

    def _context_key(argument: ast.expr) -> str | None:
        """Resolve a ``try_get_context`` argument: a literal, or a module-level constant."""
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return argument.value
        if isinstance(argument, ast.Name):
            value = getattr(loader_module, argument.id, None)
            if isinstance(value, str):
                return value
        return None

    read_keys = {
        key
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "try_get_context"
        and node.args
        and (key := _context_key(node.args[0])) is not None
    }
    assert read_keys, "no try_get_context reads found; the extraction is broken"
    assert "gco:control-plane-only" in read_keys, "constant-named context keys must resolve"

    covered = {key for _, overrides in CONFIGS for key in overrides}
    allowlisted = set(MATRIX_COVERAGE_ALLOWLIST)

    uncovered = sorted(read_keys - covered - allowlisted)
    assert not uncovered, (
        "ConfigLoader reads these context keys but no synthesis-matrix entry "
        "varies them and they carry no allowlist justification — add a config "
        f"to tests/_cdk_config_matrix.CONFIGS or allowlist with a reason: {uncovered}"
    )

    stale_allowlist = sorted(allowlisted & covered)
    assert not stale_allowlist, (
        "these keys are allowlisted as deliberately-unvaried but a matrix "
        f"entry now covers them — delete the stale allowlist entries: {stale_allowlist}"
    )

    unknown_allowlist = sorted(allowlisted - read_keys)
    assert not unknown_allowlist, (
        f"these allowlist entries name keys the loader no longer reads: {unknown_allowlist}"
    )
