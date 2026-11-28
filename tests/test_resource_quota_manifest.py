"""
Tests for the gco-jobs resource-governance manifest
(lambda/kubectl-applier-simple/manifests/04-resource-quotas.yaml).

Renders the manifest by replacing {{QUOTA_*}} and {{LIMIT_*}}
placeholders with the default values from cdk.json, then asserts the
resulting YAML holds the ResourceQuota, the LimitRange and the
per-container GPU cap (a ValidatingAdmissionPolicy and its binding) with
the expected limits and scoping. Also cross-checks that the default values
encoded in regional_stack.py and cdk.json can't silently drift — if either
side changes, the mirror test fails.

The LimitRange must name no GPU. Any ``nvidia.com/gpu`` entry there, even
``0``, is written into every container that sets none, and the
ExtendedResourceToleration admission plugin (on in EKS) then gives every pod
a toleration of the GPU node taint: CPU-only Jobs ran on GPU nodes and EKS
Auto Mode provisioned GPU instances for them. The GPU cap policy's own CEL
runs here through cel-expr-python (``tests/_cel.py``), as the tenant write
fence's does in tests/test_tenant_write_fence.py; ``integration:kind:cluster-e2e``
runs it in a real API server with the plugin on
(tests/test_supply_chain_integrity.py pins those probes).
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from gco.resource_governance import (
    CONTAINER_GPU_LIMIT_POLICY,
    CONTAINER_MAX_GPU_ANNOTATION,
    GOVERNED_NAMESPACE,
)
from tests._cel import CelError, Variables, check, evaluate, evaluate_bool

MANIFEST_PATH = Path("lambda/kubectl-applier-simple/manifests/04-resource-quotas.yaml")
CDK_JSON_PATH = Path("cdk.json")
REGIONAL_STACK_PATH = Path("gco/stacks/regional_stack.py")
EXAMPLES_DIR = Path("examples")
HARNESS_MANIFESTS_DIR = Path("scripts/live_release_validation/manifests")

PLACEHOLDERS = (
    "QUOTA_MAX_CPU",
    "QUOTA_MAX_MEMORY",
    "QUOTA_MAX_GPU",
    "QUOTA_MAX_PODS",
    "LIMIT_MAX_CPU",
    "LIMIT_MAX_MEMORY",
    "LIMIT_MAX_GPU",
)

# Default substitution values that mirror cdk.json defaults.
DEFAULT_SUBSTITUTIONS = {
    "QUOTA_MAX_CPU": "400",
    "QUOTA_MAX_MEMORY": "4096Gi",
    "QUOTA_MAX_GPU": "32",
    "QUOTA_MAX_PODS": "50",
    "LIMIT_MAX_CPU": "192",
    "LIMIT_MAX_MEMORY": "2048Gi",
    "LIMIT_MAX_GPU": "8",
}


def _render(substitutions: dict) -> str:
    """Render the manifest by replacing `{{KEY}}` placeholders with values."""
    content = MANIFEST_PATH.read_text()
    for key, value in substitutions.items():
        content = content.replace("{{" + key + "}}", str(value))
    return content


def _parse(substitutions: dict) -> list:
    """Render and load every YAML document in the manifest."""
    rendered = _render(substitutions)
    return [doc for doc in yaml.safe_load_all(rendered) if doc is not None]


def _find(docs: list, kind: str, name: str) -> dict:
    for doc in docs:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    raise AssertionError(f"{kind}/{name} not found in manifest")


def _keys(node: Any) -> set[str]:
    """Every mapping key anywhere under ``node``."""
    if isinstance(node, dict):
        return set(node) | {key for value in node.values() for key in _keys(value)}
    if isinstance(node, list):
        return {key for value in node for key in _keys(value)}
    return set()


class TestManifestStructure:
    """Basic shape and identity of the manifest documents."""

    def test_manifest_file_exists(self):
        assert MANIFEST_PATH.exists(), f"Expected manifest at {MANIFEST_PATH}"

    def test_manifest_contains_all_placeholders(self):
        """Every placeholder we substitute in code must actually appear in the file."""
        raw = MANIFEST_PATH.read_text()
        for placeholder in PLACEHOLDERS:
            assert "{{" + placeholder + "}}" in raw, (
                f"Placeholder {placeholder} missing from manifest"
            )

    def test_manifest_has_the_quota_the_limits_and_the_gpu_cap(self):
        docs = _parse(DEFAULT_SUBSTITUTIONS)
        assert [(d["kind"], d["metadata"]["name"]) for d in docs] == [
            ("ResourceQuota", "gco-jobs-quota"),
            ("LimitRange", "gco-jobs-limits"),
            ("ValidatingAdmissionPolicy", CONTAINER_GPU_LIMIT_POLICY),
            ("ValidatingAdmissionPolicyBinding", CONTAINER_GPU_LIMIT_POLICY),
        ]

    def test_the_namespaced_objects_govern_the_namespace_the_services_read(self):
        docs = _parse(DEFAULT_SUBSTITUTIONS)
        assert {
            d["metadata"]["namespace"] for d in docs if d["kind"] in {"ResourceQuota", "LimitRange"}
        } == {GOVERNED_NAMESPACE}


class TestResourceQuota:
    """The ResourceQuota object and its hard limits."""

    @pytest.fixture
    def quota(self):
        docs = _parse(DEFAULT_SUBSTITUTIONS)
        return _find(docs, "ResourceQuota", "gco-jobs-quota")

    def test_api_version_and_namespace(self, quota):
        assert quota["apiVersion"] == "v1"
        assert quota["metadata"]["namespace"] == "gco-jobs"

    def test_has_required_hard_limits(self, quota):
        hard = quota["spec"]["hard"]
        assert "requests.cpu" in hard
        assert "requests.memory" in hard
        assert "requests.nvidia.com/gpu" in hard
        assert "pods" in hard

    def test_default_values_from_cdk_json(self, quota):
        hard = quota["spec"]["hard"]
        assert hard["requests.cpu"] == "400"
        assert hard["requests.memory"] == "4096Gi"
        assert hard["requests.nvidia.com/gpu"] == "32"
        assert hard["pods"] == "50"


class TestLimitRange:
    """The LimitRange object, its CPU and memory defaults and per-container caps."""

    @pytest.fixture
    def limit_range(self):
        docs = _parse(DEFAULT_SUBSTITUTIONS)
        return _find(docs, "LimitRange", "gco-jobs-limits")

    def test_api_version_and_namespace(self, limit_range):
        assert limit_range["apiVersion"] == "v1"
        assert limit_range["metadata"]["namespace"] == "gco-jobs"

    def test_has_container_limit_with_required_fields(self, limit_range):
        limits = limit_range["spec"]["limits"]
        assert len(limits) == 1
        container_limit = limits[0]
        assert container_limit["type"] == "Container"
        assert "default" in container_limit
        assert "defaultRequest" in container_limit
        assert "max" in container_limit

    def test_default_limits(self, limit_range):
        """Per task spec: default cpu: 1, memory: 4Gi."""
        default = limit_range["spec"]["limits"][0]["default"]
        assert default == {"cpu": "1", "memory": "4Gi"}

    def test_default_requests(self, limit_range):
        """Per task spec: defaultRequest cpu: 100m, memory: 256Mi."""
        request = limit_range["spec"]["limits"][0]["defaultRequest"]
        assert request == {"cpu": "100m", "memory": "256Mi"}

    def test_names_no_gpu_anywhere(self, limit_range):
        """Regression: a GPU entry, even ``0``, put every gco-jobs pod on GPU nodes.

        The LimitRanger plugin writes a ``default``/``defaultRequest`` entry
        into every container that does not set it, and ExtendedResourceToleration
        then gives a pod that names an extended resource, at any quantity, a
        toleration of that resource's taint. Proven live: CPU-only Jobs ran on
        a GPU node with the ``kubernetes.io/limit-ranger`` annotation and that
        toleration. A ``max`` alone is no fix either: the API server copies
        ``max`` into ``default``, so every container would request the cap.
        """
        assert not {key for key in _keys(limit_range) if "nvidia.com" in key}
        assert set(limit_range["spec"]["limits"][0]["max"]) == {"cpu", "memory"}
        assert "LIMIT_MAX_GPU" not in yaml.safe_dump(
            _find(_parse({}), "LimitRange", "gco-jobs-limits")
        )

    def test_max_values_from_cdk_json(self, limit_range):
        # One full accelerator-node slice (p5.48xlarge / trn2.48xlarge):
        # anything smaller rejects full-node distributed-training pods at
        # admission (live run ex241-df723811). The GPU cap is the policy's.
        maxes = limit_range["spec"]["limits"][0]["max"]
        assert maxes == {"cpu": "192", "memory": "2048Gi"}


# ─── The per-container GPU cap ───────────────────────────────────────────────


def _policy(cap: str = "8") -> dict[str, Any]:
    docs = _parse({**DEFAULT_SUBSTITUTIONS, "LIMIT_MAX_GPU": cap})
    return _find(docs, "ValidatingAdmissionPolicy", CONTAINER_GPU_LIMIT_POLICY)


def _binding() -> dict[str, Any]:
    docs = _parse(DEFAULT_SUBSTITUTIONS)
    return _find(docs, "ValidatingAdmissionPolicyBinding", CONTAINER_GPU_LIMIT_POLICY)


def _selected(policy: dict[str, Any], namespace: str, operation: str, resource: str) -> bool:
    """Whether the policy's matchConstraints select one core/v1 request."""
    constraints = policy["spec"]["matchConstraints"]
    (expression,) = constraints["namespaceSelector"]["matchExpressions"]
    assert (expression["key"], expression["operator"]) == ("kubernetes.io/metadata.name", "In")
    if namespace not in expression["values"]:
        return False
    return any(
        "" in rule["apiGroups"]
        and "v1" in rule["apiVersions"]
        and operation in rule["operations"]
        and resource in rule["resources"]
        for rule in constraints["resourceRules"]
    )


def _review(
    pod: dict[str, Any],
    *,
    cap: str = "8",
    namespace: str = GOVERNED_NAMESPACE,
    operation: str = "CREATE",
    resource: str = "pods",
) -> list[str] | None:
    """Run the policy as the API server does for one pod request.

    None when its matchConstraints do not select the request; otherwise the
    messages of the validations that failed ([] admits the pod).
    """
    policy = _policy(cap)
    if not _selected(policy, namespace, operation, resource):
        return None
    activation: dict[str, Any] = {
        "object": pod,
        "oldObject": None,
        "request": {
            "operation": operation,
            "namespace": namespace,
            "resource": {"group": "", "version": "v1", "resource": resource},
            "userInfo": {"username": "system:serviceaccount:kube-system:job-controller"},
        },
    }
    activation["variables"] = Variables(policy["spec"]["variables"], activation)
    failures = []
    for validation in policy["spec"]["validations"]:
        try:
            if evaluate_bool(validation["expression"], activation):
                continue
            message = evaluate(validation["messageExpression"], activation)
        except CelError as error:
            pytest.fail(f"CEL error in {validation['expression']!r}: {error}")
        assert isinstance(message, str) and message.strip() and "\n" not in message
        failures.append(message)
    return failures


def _container(name: str = "main", *, limits: Any = None, requests: Any = None) -> dict:
    container: dict[str, Any] = {"name": name, "image": "registry.example/train:v1"}
    resources = {}
    if limits is not None:
        resources["limits"] = limits
    if requests is not None:
        resources["requests"] = requests
    if resources:
        container["resources"] = resources
    return container


def _pod(*containers: dict, init_containers: tuple[dict, ...] = ()) -> dict[str, Any]:
    spec: dict[str, Any] = {"containers": list(containers) or [_container()]}
    if init_containers:
        spec["initContainers"] = list(init_containers)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "train-0", "namespace": GOVERNED_NAMESPACE},
        "spec": spec,
    }


def _refusal(container: str, value: str, field: str, cap: str = "8") -> str:
    return (
        f"container {container} asks for {value} nvidia.com/gpu in resources.{field}; "
        f"a gco-jobs container may ask for at most {cap}, set by "
        "resource_quota.container_max_gpu in cdk.json"
    )


def _gpus(count: Any) -> dict[str, Any]:
    return {"nvidia.com/gpu": count}


class TestContainerGpuLimitWiring:
    """The policy, its binding, and the names the services and RBAC read."""

    def test_policy_and_binding_deny_and_are_labelled_like_the_other_gco_policies(self):
        policy, binding = _policy(), _binding()
        for doc in (policy, binding):
            assert doc["apiVersion"] == "admissionregistration.k8s.io/v1"
            assert doc["metadata"]["labels"] == {"app": "gco", "project": "gco"}
            assert "namespace" not in doc["metadata"]
        assert policy["spec"]["failurePolicy"] == "Fail"
        assert binding["spec"] == {
            "policyName": CONTAINER_GPU_LIMIT_POLICY,
            "validationActions": ["Deny"],
        }

    def test_matches_pod_creates_in_gco_jobs_only(self):
        constraints = _policy()["spec"]["matchConstraints"]
        assert constraints["namespaceSelector"] == {
            "matchExpressions": [
                {
                    "key": "kubernetes.io/metadata.name",
                    "operator": "In",
                    "values": [GOVERNED_NAMESPACE],
                }
            ]
        }
        assert constraints["resourceRules"] == [
            {
                "apiGroups": [""],
                "apiVersions": ["v1"],
                "operations": ["CREATE"],
                "resources": ["pods"],
            }
        ]
        assert _review(_pod(), namespace="gco-inference") is None
        assert _review(_pod(), operation="UPDATE") is None
        assert _review(_pod(), resource="pods/ephemeralcontainers") is None

    def test_the_cap_is_one_setting_in_the_annotation_and_the_cel(self):
        """GET /api/v1/policy reports the annotation, so it must be what the CEL enforces."""
        raw = MANIFEST_PATH.read_text()
        assert raw.count("{{LIMIT_MAX_GPU}}") == 2
        unrendered = _find(_parse({}), "ValidatingAdmissionPolicy", CONTAINER_GPU_LIMIT_POLICY)
        assert unrendered["metadata"]["annotations"] == {
            CONTAINER_MAX_GPU_ANNOTATION: "{{LIMIT_MAX_GPU}}"
        }
        (cap,) = [v for v in unrendered["spec"]["variables"] if v["name"] == "cap"]
        assert cap["expression"] == "{{LIMIT_MAX_GPU}}"

    def test_every_expression_compiles_against_the_variables_before_it(self):
        spec = _policy()["spec"]
        defined: list[str] = []
        for variable in spec["variables"]:
            check(variable["expression"], defined)
            defined.append(variable["name"])
        for validation in spec["validations"]:
            check(validation["expression"], defined)
            check(validation["messageExpression"], defined)
            assert validation["reason"] == "Forbidden"
            assert validation["message"].strip()

    def test_every_map_literal_holds_strings_only(self):
        """The API server's CEL takes a map literal only when its values share one type.

        It type-checks with homogeneous aggregate literals, which the CEL
        these tests run does not, and a container taken from
        ``variables.containers`` is ``dyn``: ``{'container': c.name, ...}``
        beside string values was refused at deploy time (422, "expected type
        'dyn' but found 'string'") while every test here passed. So each map
        value must be a string literal or a ``string(...)`` conversion.
        """
        spec = _policy()["spec"]
        expressions = [v["expression"] for v in spec["variables"]] + [
            text for v in spec["validations"] for text in (v["expression"], v["messageExpression"])
        ]
        literals = [m for e in expressions for m in re.findall(r"\{('[^{}]*)\}", e)]
        assert literals, "the policy builds its refusals as maps"
        for literal in literals:
            for value in re.split(r",\s*'[a-z]+':\s*", "," + literal)[1:]:
                assert value.startswith(("'", "string(")), (literal, value)

    def test_a_cap_that_is_not_an_integer_literal_does_not_compile(self):
        """Why synth requires container_max_gpu in plain digits (TestValidatedResourceQuota)."""
        with pytest.raises(CelError, match="does not compile"):
            check(_policy(cap="1k")["spec"]["variables"][0]["expression"])


class TestContainerGpuLimit:
    """The policy's CEL, run as the API server runs it."""

    @pytest.mark.parametrize(
        "container",
        [
            _container(),
            _container(limits={}, requests={}),
            _container(limits={"cpu": "2", "memory": "8Gi"}, requests={"cpu": "1"}),
            _container(limits={"vpc.amazonaws.com/efa": "4"}),
        ],
        ids=["no-resources", "empty-resources", "cpu-and-memory", "other-extended-resource"],
    )
    def test_cpu_only_pods_are_admitted(self, container):
        assert _review(_pod(container)) == []

    @pytest.mark.parametrize(
        ("limits", "requests"),
        [
            (_gpus("8"), _gpus("8")),
            (_gpus("8"), None),
            (None, _gpus("8")),
            (_gpus("1"), _gpus("1")),
            (_gpus("0"), _gpus("0")),
            (_gpus(8), _gpus(8)),
        ],
        ids=["at-cap", "at-cap-limits", "at-cap-requests", "under", "zero", "yaml-integer"],
    )
    def test_gpus_up_to_the_cap_are_admitted(self, limits, requests):
        assert _review(_pod(_container(limits=limits, requests=requests))) == []

    @pytest.mark.parametrize(
        ("limits", "requests", "refusal"),
        [
            (_gpus("9"), _gpus("9"), _refusal("main", "9", "limits")),
            (_gpus("16"), None, _refusal("main", "16", "limits")),
            (None, _gpus("12"), _refusal("main", "12", "requests")),
            (_gpus("8"), _gpus("9"), _refusal("main", "9", "requests")),
            (_gpus(9), None, _refusal("main", "9", "limits")),
        ],
        ids=["both", "limits", "requests", "requests-over-limits-at", "yaml-integer"],
    )
    def test_gpus_over_the_cap_are_refused_naming_container_count_cap_and_setting(
        self, limits, requests, refusal
    ):
        assert _review(_pod(_container(limits=limits, requests=requests))) == [refusal]

    def test_init_containers_are_held_to_the_same_cap(self):
        setup_at_cap = _container("setup", limits=_gpus("8"))
        setup_over = _container("setup", requests=_gpus("9"))
        assert _review(_pod(init_containers=(setup_at_cap,))) == []
        assert _review(_pod(init_containers=(setup_over,))) == [_refusal("setup", "9", "requests")]

    def test_the_container_over_the_cap_is_the_one_named(self):
        pod = _pod(
            _container("main", limits=_gpus("8")),
            _container("sidecar", limits=_gpus("10")),
            _container("logger", limits={"cpu": "1"}),
        )
        assert _review(pod) == [_refusal("sidecar", "10", "limits")]

    @pytest.mark.parametrize("count", ["1k", "2e3", "1Ki", "8.0", "+8", "-1"])
    def test_a_count_not_written_in_plain_digits_is_refused_never_let_through(self, count):
        """Any other form is refused, whatever it means.

        The API server stores a count below 1000 as plain digits, so only a
        count of 1000 or more (``1k``) reaches the policy in another form, and
        that is over any cap synth allows.
        """
        assert _review(_pod(_container(limits=_gpus(count)))) == [_refusal("main", count, "limits")]

    def test_a_count_too_long_for_an_integer_is_refused_without_a_cel_error(self):
        count = "9" * 20
        assert _review(_pod(_container(limits=_gpus(count)))) == [_refusal("main", count, "limits")]

    def test_the_cap_is_the_rendered_setting(self):
        two = _pod(_container(limits=_gpus("2")))
        three = _pod(_container(limits=_gpus("3")))
        assert _review(two, cap="2") == []
        assert _review(three, cap="2") == [_refusal("main", "3", "limits", cap="2")]
        assert _review(_pod(_container(limits=_gpus("0"))), cap="0") == []
        assert _review(_pod(_container(limits=_gpus("1"))), cap="0") == [
            _refusal("main", "1", "limits", cap="0")
        ]
        assert _review(_pod(_container(limits=_gpus("999"))), cap="999") == []

    def test_every_shipped_gco_jobs_pod_template_is_admitted_at_the_default_cap(self):
        """The examples and harness manifests a job namespace receives fit the cap.

        tests/test_example_job_validation.py proves the same numerically
        (static_checks); this runs the policy's own CEL on each pod spec.
        """
        paths = sorted(EXAMPLES_DIR.glob("*.yaml")) + sorted(HARNESS_MANIFESTS_DIR.glob("*.yaml"))
        reviewed = 0
        for path in paths:
            text = re.sub(r"\{\{[A-Za-z0-9_]+\}\}", "placeholder", path.read_text())
            for doc in yaml.safe_load_all(text):
                if not isinstance(doc, dict):
                    continue
                namespace = (doc.get("metadata") or {}).get("namespace", GOVERNED_NAMESPACE)
                if namespace != GOVERNED_NAMESPACE:
                    continue
                for spec in _pod_specs(doc):
                    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {}, "spec": spec}
                    assert _review(pod) == [], f"{path}: {doc.get('kind')}"
                    reviewed += 1
        assert reviewed >= 10


def _pod_specs(node: Any) -> list[dict[str, Any]]:
    """Every pod spec (a mapping with a ``containers`` list) under ``node``."""
    if isinstance(node, list):
        return [spec for value in node for spec in _pod_specs(value)]
    if not isinstance(node, dict):
        return []
    found = [node] if isinstance(node.get("containers"), list) else []
    return found + [spec for value in node.values() for spec in _pod_specs(value)]


class TestPlaceholderSubstitution:
    """Parameterized checks that user-supplied config values flow through."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {
                "QUOTA_MAX_CPU": "200",
                "QUOTA_MAX_MEMORY": "1Ti",
                "QUOTA_MAX_GPU": "32",
                "QUOTA_MAX_PODS": "100",
                "LIMIT_MAX_CPU": "20",
                "LIMIT_MAX_MEMORY": "128Gi",
                "LIMIT_MAX_GPU": "8",
            },
            {
                "QUOTA_MAX_CPU": "50",
                "QUOTA_MAX_MEMORY": "256Gi",
                "QUOTA_MAX_GPU": "4",
                "QUOTA_MAX_PODS": "25",
                "LIMIT_MAX_CPU": "5",
                "LIMIT_MAX_MEMORY": "32Gi",
                "LIMIT_MAX_GPU": "2",
            },
            {
                "QUOTA_MAX_CPU": "1",
                "QUOTA_MAX_MEMORY": "1Gi",
                "QUOTA_MAX_GPU": "0",
                "QUOTA_MAX_PODS": "1",
                "LIMIT_MAX_CPU": "1",
                "LIMIT_MAX_MEMORY": "1Gi",
                "LIMIT_MAX_GPU": "0",
            },
        ],
    )
    def test_substitution_produces_expected_quota_limit_and_gpu_cap(self, overrides):
        docs = _parse(overrides)

        quota = _find(docs, "ResourceQuota", "gco-jobs-quota")
        hard = quota["spec"]["hard"]
        # ResourceQuota values are always rendered as strings by safe_load because
        # the template wraps them in quotes.
        assert hard["requests.cpu"] == overrides["QUOTA_MAX_CPU"]
        assert hard["requests.memory"] == overrides["QUOTA_MAX_MEMORY"]
        assert hard["requests.nvidia.com/gpu"] == overrides["QUOTA_MAX_GPU"]
        assert hard["pods"] == overrides["QUOTA_MAX_PODS"]

        limit_range = _find(docs, "LimitRange", "gco-jobs-limits")
        maxes = limit_range["spec"]["limits"][0]["max"]
        assert maxes == {
            "cpu": overrides["LIMIT_MAX_CPU"],
            "memory": overrides["LIMIT_MAX_MEMORY"],
        }

        policy = _find(docs, "ValidatingAdmissionPolicy", CONTAINER_GPU_LIMIT_POLICY)
        cap = overrides["LIMIT_MAX_GPU"]
        assert policy["metadata"]["annotations"] == {CONTAINER_MAX_GPU_ANNOTATION: cap}
        assert policy["spec"]["variables"][0] == {"name": "cap", "expression": cap}

    def test_no_unsubstituted_placeholders_remain(self):
        rendered = _render(DEFAULT_SUBSTITUTIONS)
        assert "{{" not in rendered
        assert "}}" not in rendered


class TestDefaultsMatchCdkJson:
    """Regression guard: constants defaults must match cdk.json defaults."""

    @pytest.fixture
    def cdk_defaults(self):
        with CDK_JSON_PATH.open() as f:
            cdk = json.load(f)
        return cdk["context"]["resource_quota"]

    def test_cdk_json_mirrors_default_resource_quota(self, cdk_defaults):
        # DEFAULT_RESOURCE_QUOTA (gco/stacks/constants.py) is the single
        # source of truth the regional stack merges context over; cdk.json
        # documents the same values for operators. If either side drifts,
        # operators on older config files get silently different limits
        # than operators with fresh defaults.
        from gco.stacks.constants import DEFAULT_RESOURCE_QUOTA

        assert dict(cdk_defaults) == dict(DEFAULT_RESOURCE_QUOTA)

    def test_stack_reads_defaults_from_constants(self):
        # The stack must consume the shared defaults (via the validated
        # helper), not carry its own inline copies.
        source = REGIONAL_STACK_PATH.read_text()
        assert "_validated_resource_quota(" in source
        assert 'resource_quota.get("max_cpu"' not in source


class TestValidatedResourceQuota:
    """Synth-time validation of the resource_quota context.

    The values are substituted verbatim into the gco-jobs ResourceQuota,
    LimitRange and GPU cap policy; before this validation a typo or an
    incoherent pair deployed silently and surfaced only as pods forbidden at
    admission (live example-job validation run ex241-df723811).
    """

    @staticmethod
    def _validate(raw):
        from gco.stacks.regional_stack import _validated_resource_quota

        return _validated_resource_quota(raw)

    def test_empty_context_yields_defaults(self):
        from gco.stacks.constants import DEFAULT_RESOURCE_QUOTA

        assert self._validate({}) == dict(DEFAULT_RESOURCE_QUOTA)

    def test_partial_override_merges_over_defaults(self):
        merged = self._validate({"container_max_gpu": "4"})
        assert merged["container_max_gpu"] == "4"
        assert merged["max_gpu"] == "32"

    def test_non_mapping_context_is_rejected(self):
        with pytest.raises(ValueError, match="must be an object"):
            self._validate("not-a-dict")

    def test_unknown_key_is_rejected(self):
        with pytest.raises(ValueError, match="unknown key"):
            self._validate({"max_cpus": "100"})

    def test_unparseable_quantity_is_rejected(self):
        with pytest.raises(ValueError, match="not a valid Kubernetes quantity"):
            self._validate({"container_max_memory": "64G i"})

    def test_container_ceiling_may_not_exceed_namespace_ceiling(self):
        with pytest.raises(ValueError, match="could never be admitted"):
            self._validate({"container_max_gpu": "64"})

    def test_mixed_units_compare_correctly(self):
        # 1Ti fits inside the 4096Gi namespace default; quantities must be
        # compared numerically, not lexically.
        merged = self._validate({"container_max_memory": "1Ti"})
        assert merged["container_max_memory"] == "1Ti"

    @pytest.mark.parametrize(
        ("value", "max_gpu", "expected"),
        [("0", "32", "0"), (8, "32", "8"), ("999", "1000", "999"), ("007", "32", "007")],
        ids=["zero", "json-number", "largest", "leading-zeros"],
    )
    def test_gpu_cap_in_plain_digits_below_1000_is_accepted(self, value, max_gpu, expected):
        merged = self._validate({"container_max_gpu": value, "max_gpu": max_gpu})
        assert merged["container_max_gpu"] == expected
        # It is the CEL integer literal of the GPU cap policy, so it compiles.
        check(_policy(cap=expected)["spec"]["variables"][0]["expression"])

    @pytest.mark.parametrize("value", ["1k", "8.0", "1e1", "8Ki", "1000", 1.5], ids=str)
    def test_gpu_cap_not_a_whole_number_below_1000_in_plain_digits_is_rejected(self, value):
        """It is the CEL integer literal the policy compares each GPU count with.

        Each value parses as a Kubernetes quantity, but ``1k`` and ``8Ki`` do
        not compile as CEL, so the API server would refuse the policy and the
        deploy would fail, and ``8.0``, ``1e1`` and ``1.5`` are CEL doubles. A
        cap of 1000 or more could refuse a pod within it: the policy admits
        plain digits only, and the API server writes 1000 as ``1k``.
        """
        expected = (
            f"resource_quota.container_max_gpu={str(value)!r} must be a whole number of GPUs "
            "from 0 to 999 written in plain digits, such as '8': the "
            "gco-jobs-container-gpu-limit admission policy compares each container's GPU "
            "count with it"
        )
        with pytest.raises(ValueError, match=f"^{re.escape(expected)}$"):
            self._validate({"container_max_gpu": value, "max_gpu": "4096"})

    def test_a_negative_gpu_cap_is_reported_as_negative(self):
        with pytest.raises(ValueError, match="must not be negative"):
            self._validate({"container_max_gpu": "-1"})

    def test_the_config_loader_reports_the_same_gpu_cap_error(self):
        """ConfigLoader shares the merge, so `gco` config validation names it too."""
        from unittest.mock import MagicMock

        from gco.config.config_loader import ConfigLoader, ConfigValidationError

        loader = ConfigLoader.__new__(ConfigLoader)
        loader.app = MagicMock()
        loader.app.node.try_get_context.return_value = {"container_max_gpu": "1k"}
        with pytest.raises(ConfigValidationError, match="from 0 to 999 written in plain digits"):
            loader.get_resource_quota()


class TestValidatedManifestCaps:
    """Synth-time validation of job_validation_policy.resource_quotas.

    These caps are what the manifest/queue processors enforce per submitted
    manifest; the layering invariant (per-container ceiling <= per-manifest
    cap <= namespace quota) is what keeps the three enforcement layers
    telling one story. The old defaults disagreed (4-GPU manifest cap vs
    the platform's own 16-GPU EFA training example).
    """

    @staticmethod
    def _validate(raw, quota=None):
        from gco.stacks.regional_stack import (
            _validated_manifest_caps,
            _validated_resource_quota,
        )

        return _validated_manifest_caps(raw, _validated_resource_quota(quota or {}))

    def test_empty_context_yields_defaults(self):
        from gco.stacks.constants import DEFAULT_MANIFEST_RESOURCE_CAPS

        assert self._validate({}) == {
            key: str(value) for key, value in DEFAULT_MANIFEST_RESOURCE_CAPS.items()
        }

    def test_partial_override_merges_over_defaults(self):
        merged = self._validate({"max_gpu_per_manifest": "8"})
        assert merged["max_gpu_per_manifest"] == "8"
        assert merged["max_cpu_per_manifest"] == "384"

    def test_non_mapping_context_is_rejected(self):
        with pytest.raises(ValueError, match="must be an object"):
            self._validate(["max_gpu_per_manifest"])

    def test_unknown_key_is_rejected(self):
        with pytest.raises(ValueError, match="unknown key"):
            self._validate({"max_gpus_per_manifest": 8})

    def test_unparseable_quantity_is_rejected(self):
        with pytest.raises(ValueError, match="not a valid Kubernetes quantity"):
            self._validate({"max_memory_per_manifest": "lots"})

    def test_cap_below_container_ceiling_is_rejected(self):
        # A manifest cap below the per-container GPU cap means the front door
        # rejects manifests whose single container the namespace would
        # happily admit.
        with pytest.raises(
            ValueError,
            match=re.escape(
                "below resource_quota.container_max_gpu='8': the front door would reject a "
                "manifest whose single container the per-container ceiling admits"
            ),
        ):
            self._validate({"max_gpu_per_manifest": 4})

    def test_cap_above_namespace_quota_is_rejected(self):
        # A manifest cap above the namespace quota accepts manifests whose
        # pods can never all run.
        with pytest.raises(ValueError, match=re.escape("exceeds resource_quota.max_gpu")):
            self._validate({"max_gpu_per_manifest": 64})

    def test_custom_quota_moves_the_bounds(self):
        merged = self._validate(
            {"max_gpu_per_manifest": 4},
            quota={"container_max_gpu": "2", "max_gpu": "8"},
        )
        assert merged["max_gpu_per_manifest"] == "4"


class TestManifestCapsMatchCdkJson:
    """Regression guard: manifest-cap constants must match cdk.json."""

    def test_cdk_json_mirrors_default_manifest_caps(self):
        from gco.stacks.constants import DEFAULT_MANIFEST_RESOURCE_CAPS

        with CDK_JSON_PATH.open() as f:
            cdk = json.load(f)
        documented = cdk["context"]["job_validation_policy"]["resource_quotas"]
        assert documented == dict(DEFAULT_MANIFEST_RESOURCE_CAPS)

    def test_stack_reads_caps_from_constants(self):
        source = REGIONAL_STACK_PATH.read_text()
        assert "_validated_manifest_caps(" in source
        # The old inline fallback copies are exactly what let the defaults
        # drift apart; the stack must not carry its own values.
        assert '"max_gpu_per_manifest", 4' not in source
        assert '.get("max_cpu_per_manifest", "10")' not in source


# ─── Kueue ClusterQueues cover what GCO's jobs request ───────────────────────

KUEUE_DEFAULT_QUEUES = Path(
    "lambda/kubectl-applier-simple/manifests/post-helm-kueue-default-queues.yaml"
)
KUEUE_HYPERPOD_QUEUE = Path(
    "lambda/kubectl-applier-simple/manifests/post-helm-kueue-hyperpod-queue.yaml"
)
EXAMPLES_DIR = Path("examples")


def _cluster_queue(path: Path, replacements: dict[str, str]) -> dict:
    content = path.read_text(encoding="utf-8")
    for token, value in replacements.items():
        content = content.replace(token, value)
    assert "{{" not in content, "every quota token must be rendered"
    (queue,) = [doc for doc in yaml.safe_load_all(content) if doc and doc["kind"] == "ClusterQueue"]
    return queue


def _default_queue() -> dict:
    from gco.resource_governance import DEFAULT_RESOURCE_QUOTA, kueue_efa_quota

    return _cluster_queue(
        KUEUE_DEFAULT_QUEUES,
        {
            "{{KUEUE_ENABLED}}": "true",
            "{{QUOTA_MAX_CPU}}": DEFAULT_RESOURCE_QUOTA["max_cpu"],
            "{{QUOTA_MAX_MEMORY}}": DEFAULT_RESOURCE_QUOTA["max_memory"],
            "{{QUOTA_MAX_GPU}}": DEFAULT_RESOURCE_QUOTA["max_gpu"],
            "{{QUOTA_MAX_EFA}}": kueue_efa_quota(DEFAULT_RESOURCE_QUOTA["max_gpu"]),
            "{{QUOTA_MAX_PODS}}": DEFAULT_RESOURCE_QUOTA["max_pods"],
        },
    )


def _hyperpod_queue() -> dict:
    from gco.hyperpod_config import compute_hyperpod_replacements, validate_hyperpod_config

    config = validate_hyperpod_config(
        {
            "enabled": True,
            "instance_groups": [{"name": "workers", "instance_type": "ml.p5.48xlarge"}],
        }
    )
    replacements = compute_hyperpod_replacements(config, enabled=True, region="us-east-1")
    return _cluster_queue(KUEUE_HYPERPOD_QUEUE, {"{{KUEUE_ENABLED}}": "true", **replacements})


def _quotas(queue: dict) -> dict[str, str]:
    (group,) = queue["spec"]["resourceGroups"]
    (flavor,) = group["flavors"]
    quotas = {resource["name"]: str(resource["nominalQuota"]) for resource in flavor["resources"]}
    assert set(quotas) == set(group["coveredResources"])
    return quotas


def _example_job_requests() -> list[tuple[str, dict[str, int]]]:
    """``(example, total requests)`` for every batch Job the examples ship.

    Kueue charges a Job its pods' requests (a container's limits stand in for
    requests it omits) times ``parallelism``.
    """
    from gco.k8s_quantity import parse_quantity

    jobs = []
    for path in sorted(EXAMPLES_DIR.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(doc, dict) or doc.get("apiVersion") != "batch/v1":
                continue
            if doc.get("kind") != "Job":
                continue
            pods = int(doc["spec"].get("parallelism") or 1)
            totals: dict[str, int] = {}
            for container in doc["spec"]["template"]["spec"]["containers"]:
                resources = container.get("resources") or {}
                merged = {**(resources.get("limits") or {}), **(resources.get("requests") or {})}
                for name, value in merged.items():
                    totals[name] = totals.get(name, 0) + parse_quantity(value) * pods
            jobs.append((f"{path.name}:{doc['metadata']['name']}", totals))
    return jobs


class TestKueueQueueCoverage:
    """Kueue never admits a workload that requests a resource its ClusterQueue
    does not cover, so a labelled Trainium, Inferentia or EFA Job stayed
    suspended forever on the queues GCO ships."""

    ACCELERATOR_RESOURCES = {"nvidia.com/gpu", "aws.amazon.com/neuron", "vpc.amazonaws.com/efa"}

    def test_every_accelerator_resource_a_nodepool_taints_for_is_covered(self):
        """The NodePools taint their nodes with the resource names they offer.

        The keep-warm pool's second taint names no resource: it keeps pods
        that did not opt in off the pool's nodes (gco.keep_warm).
        """
        from gco.keep_warm import KEEP_WARM_LABEL

        taint_keys = set()
        for path in sorted(
            Path("lambda/kubectl-applier-simple/manifests").glob("4*-nodepool-*.yaml")
        ):
            text = re.sub(r"\{\{[A-Z0-9_]+\}\}", "placeholder", path.read_text(encoding="utf-8"))
            for doc in yaml.safe_load_all(text):
                if isinstance(doc, dict) and doc.get("kind") == "NodePool":
                    taint_keys |= {
                        t["key"] for t in doc["spec"]["template"]["spec"].get("taints", [])
                    }
        assert KEEP_WARM_LABEL in taint_keys
        taint_keys.discard(KEEP_WARM_LABEL)
        assert taint_keys == self.ACCELERATOR_RESOURCES
        for queue in (_default_queue(), _hyperpod_queue()):
            assert taint_keys <= set(_quotas(queue)), queue["metadata"]["name"]

    @pytest.mark.parametrize("queue_factory", [_default_queue, _hyperpod_queue])
    def test_neuron_shares_the_gpu_quota_and_efa_follows_both(self, queue_factory):
        from gco.resource_governance import kueue_efa_quota

        quotas = _quotas(queue_factory())
        assert quotas["aws.amazon.com/neuron"] == quotas["nvidia.com/gpu"]
        assert quotas["vpc.amazonaws.com/efa"] == kueue_efa_quota(quotas["nvidia.com/gpu"])

    @pytest.mark.parametrize("queue_factory", [_default_queue, _hyperpod_queue])
    def test_every_shipped_example_job_is_admissible_once_labelled(self, queue_factory):
        from gco.k8s_quantity import parse_quantity

        quotas = _quotas(queue_factory())
        jobs = _example_job_requests()
        requested = {name for _example, totals in jobs for name in totals}
        assert requested >= self.ACCELERATOR_RESOURCES, "the examples exercise every accelerator"
        for example, totals in jobs:
            for name, total in totals.items():
                assert name in quotas, f"{example}: {name} is not covered"
                assert total <= parse_quantity(quotas[name]), (example, name, total)


class TestKueueEfaQuota:
    def test_four_interfaces_per_gpu_plus_one_per_neuron_device(self):
        from gco.resource_governance import kueue_efa_quota

        # 32 GPUs on p5.48xlarge attach 4 x 32 interfaces; 32 Trainium2
        # devices another 32.
        assert kueue_efa_quota("32") == "160"
        assert kueue_efa_quota(64) == "320"
        assert kueue_efa_quota("0") == "0"

    def test_a_fractional_budget_rounds_up_to_whole_interfaces(self):
        from gco.resource_governance import kueue_efa_quota

        assert kueue_efa_quota("0.5") == "3"

    def test_a_non_quantity_is_rejected(self):
        from gco.resource_governance import kueue_efa_quota

        with pytest.raises(ValueError, match="not a Kubernetes quantity"):
            kueue_efa_quota("lots")
