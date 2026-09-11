"""
The network posture GCO ships, pinned to the manifests that implement it.

``lambda/kubectl-applier-simple/manifests/03-network-policies.yaml`` is the
single source of truth for three namespaces:

- ``gco-system``: default-deny ingress; every platform Deployment is admitted
  on exactly the port it serves (8443 for the TLS proxy sidecars the ALB
  targets, 9090 for the inference monitor's metrics endpoint), egress is DNS +
  HTTPS plus the proxy's path into ``gco-inference``.
- ``gco-jobs``: default-deny ingress from other namespaces, everything allowed
  between job pods, egress DNS + HTTPS anywhere + the in-VPC ranges from
  ``vpc_endpoint_cidrs`` on any port.
- ``gco-inference``: model pods reachable only from the authenticated proxy and
  each other; egress DNS + HTTPS.

The rules that make it real on EKS Auto Mode live in
``06-network-policy-controller.yaml`` (the enforcement ConfigMap, rendered from
``eks_cluster.network_policy_enforcement``). These tests replay the
``{{VPC_ENDPOINT_CIDR_BLOCKS}}`` substitution the regional stack performs so
they parse the same YAML the applier would.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

MANIFESTS_DIR = Path("lambda/kubectl-applier-simple/manifests")
NETPOL_MANIFEST_PATH = MANIFESTS_DIR / "03-network-policies.yaml"
CONTROLLER_MANIFEST_PATH = MANIFESTS_DIR / "06-network-policy-controller.yaml"

# Placeholder as used by kubectl-applier Lambda substitution (see regional_stack.py).
CIDR_PLACEHOLDER = "{{VPC_ENDPOINT_CIDR_BLOCKS}}"

#: gco-system Deployments and the ports the kubelet probes / other pods call.
#: The ScaledJob (queue-processor) has no listener and needs no ingress rule.
PLATFORM_INGRESS_PORTS = {
    "health-monitor": {8443},
    "manifest-processor": {8443},
    "inference-proxy": {8443},
    "inference-monitor": {9090},
}


def _render_cidr_block_substitution(cidrs: list[str]) -> str:
    """
    Replicates the substitution logic from gco/stacks/regional_stack.py so the
    test exercises the same string transformation performed at deploy time.

    The placeholder sits at 8-space indentation in the manifest, so the first
    entry needs no leading indent (the manifest provides it) and subsequent
    entries are indented 8 spaces to align.
    """
    lines = []
    for i, cidr in enumerate(cidrs):
        prefix = "" if i == 0 else "        "
        lines.append(f'{prefix}- ipBlock:\n            cidr: "{cidr}"')
    return "\n".join(lines)


def _substitute_and_load(raw: str, cidrs: list[str]) -> list[dict]:
    """Substitute the CIDR placeholder and parse the resulting YAML."""
    substituted = raw.replace(CIDR_PLACEHOLDER, _render_cidr_block_substitution(cidrs))
    docs = list(yaml.safe_load_all(substituted))
    return [d for d in docs if d is not None]


def _stubbed_documents(path: Path) -> list[dict]:
    """Parse any manifest with every remaining placeholder replaced by a literal."""
    text = re.sub(r"\{\{[A-Z0-9_]+\}\}", "placeholder", path.read_text(encoding="utf-8"))
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


@pytest.fixture(scope="module")
def raw_manifest() -> str:
    """Load the raw manifest (with the unsubstituted placeholder)."""
    return NETPOL_MANIFEST_PATH.read_text()


@pytest.fixture(scope="module")
def netpol_docs(raw_manifest) -> list[dict]:
    """Parse the manifest after substituting a sample CIDR value."""
    return _substitute_and_load(raw_manifest, ["10.0.0.0/16"])


def _find_netpol(docs, name: str, namespace: str):
    """Find a NetworkPolicy document by name and namespace."""
    for d in docs:
        if (
            d.get("kind") == "NetworkPolicy"
            and d["metadata"]["name"] == name
            and d["metadata"]["namespace"] == namespace
        ):
            return d
    return None


def _policies_in(docs, namespace: str) -> list[dict]:
    return [
        d
        for d in docs
        if d.get("kind") == "NetworkPolicy" and d["metadata"]["namespace"] == namespace
    ]


def _get_port_protocols(rule: dict) -> set[tuple[str, int]]:
    """Return the set of (protocol, port) tuples referenced by a rule."""
    result = set()
    for port in rule.get("ports", []):
        proto = port.get("protocol", "TCP")
        port_num = port.get("port")
        if port_num is not None:
            result.add((proto, port_num))
    return result


def _selects(policy: dict, labels: dict[str, str]) -> bool:
    """Whether ``policy.spec.podSelector`` matches a pod carrying ``labels``."""
    selector = policy["spec"].get("podSelector") or {}
    wanted = selector.get("matchLabels") or {}
    return all(labels.get(key) == value for key, value in wanted.items())


def _ingress_rules_selecting(docs, namespace: str, labels: dict[str, str]) -> list[dict]:
    rules: list[dict] = []
    for policy in _policies_in(docs, namespace):
        if "Ingress" not in policy["spec"].get("policyTypes", []):
            continue
        if _selects(policy, labels):
            rules.extend(policy["spec"].get("ingress") or [])
    return rules


# ─── Default Deny Ingress ──────────────────────────────────────────


class TestDefaultDenyIngress:
    """Verify default-deny-ingress is present for gco-system and gco-jobs."""

    @pytest.mark.parametrize("namespace", ["gco-system", "gco-jobs"])
    def test_default_deny_ingress_exists(self, netpol_docs, namespace):
        policy = _find_netpol(netpol_docs, "default-deny-ingress", namespace)
        assert policy is not None, f"default-deny-ingress NetworkPolicy must exist in {namespace}"

    @pytest.mark.parametrize("namespace", ["gco-system", "gco-jobs"])
    def test_default_deny_ingress_selects_all_pods(self, netpol_docs, namespace):
        """An empty podSelector `{}` selects every pod in the namespace."""
        policy = _find_netpol(netpol_docs, "default-deny-ingress", namespace)
        assert policy["spec"].get("podSelector") == {}

    @pytest.mark.parametrize("namespace", ["gco-system", "gco-jobs"])
    def test_default_deny_ingress_has_ingress_type_and_no_rules(self, netpol_docs, namespace):
        """No ingress rules + policyTypes containing Ingress == default deny."""
        policy = _find_netpol(netpol_docs, "default-deny-ingress", namespace)
        assert "Ingress" in policy["spec"]["policyTypes"]
        # Default-deny = no `ingress:` key at all, or empty list
        assert not policy["spec"].get("ingress")


# ─── gco-system: every platform workload is reachable on its port ──


class TestPlatformIngress:
    """Each gco-system Deployment is admitted on exactly the port it serves."""

    @pytest.mark.parametrize("app,ports", sorted(PLATFORM_INGRESS_PORTS.items()))
    def test_each_platform_workload_has_a_port_scoped_ingress_allow(self, netpol_docs, app, ports):
        rules = _ingress_rules_selecting(netpol_docs, "gco-system", {"app": app, "project": "gco"})
        assert rules, f"{app}: default-deny ingress with no allow rule — unreachable"
        allowed = set()
        for rule in rules:
            allowed |= {port for _proto, port in _get_port_protocols(rule)}
        assert allowed == ports, f"{app}: allowed ports {allowed} != served ports {ports}"

    @pytest.mark.parametrize("app,ports", sorted(PLATFORM_INGRESS_PORTS.items()))
    def test_probed_port_rules_admit_any_source(self, netpol_docs, app, ports):
        """Port only, no ``from``: the ALB is not a pod and neither is the kubelet.

        The kubelet's probes arrive from the node's host network, which no
        pod or namespace selector can name (post-helm-mlflow-network.yaml
        documents the live incident behind this).
        """
        for rule in _ingress_rules_selecting(netpol_docs, "gco-system", {"app": app}):
            assert "from" not in rule, f"{app}: rule {rule} names sources on a probed port"

    def test_no_platform_workload_admits_an_unrelated_port(self, netpol_docs):
        """No gco-system ingress rule opens a port nobody serves."""
        served = set().union(*PLATFORM_INGRESS_PORTS.values())
        for policy in _policies_in(netpol_docs, "gco-system"):
            for rule in policy["spec"].get("ingress") or []:
                for _proto, port in _get_port_protocols(rule):
                    assert port in served, f"{policy['metadata']['name']} opens unserved {port}"

    def test_inference_monitor_metrics_port_matches_the_scrape_and_probes(self):
        """The 9090 rule covers the PodMonitor scrape and the manifest's probes."""
        deployment = next(
            doc
            for doc in _stubbed_documents(MANIFESTS_DIR / "32-inference-monitor.yaml")
            if doc["kind"] == "Deployment"
        )
        (container,) = deployment["spec"]["template"]["spec"]["containers"]
        ports = {port["name"]: port["containerPort"] for port in container["ports"]}
        assert ports == {"metrics": 9090}
        for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
            assert container[probe]["httpGet"]["port"] == "metrics"
        monitors = [
            doc
            for doc in _stubbed_documents(
                MANIFESTS_DIR / "post-helm-monitoring-servicemonitors.yaml"
            )
            if doc["kind"] == "PodMonitor"
            and doc["spec"]["selector"]["matchLabels"] == {"app": "inference-monitor"}
        ]
        assert [monitor["spec"]["podMetricsEndpoints"][0]["port"] for monitor in monitors] == [
            "metrics"
        ]
        assert PLATFORM_INGRESS_PORTS["inference-monitor"] == {ports["metrics"]}

    def test_scraped_tls_ports_are_the_alb_ports(self):
        """The three HTTPS PodMonitors scrape the same 8443 the ALB rules open."""
        monitors = [
            doc
            for doc in _stubbed_documents(
                MANIFESTS_DIR / "post-helm-monitoring-servicemonitors.yaml"
            )
            if doc["kind"] == "PodMonitor"
            and doc["spec"]["podMetricsEndpoints"][0]["port"] == "https"
        ]
        scraped = {monitor["spec"]["selector"]["matchLabels"]["app"] for monitor in monitors}
        assert scraped == {"health-monitor", "manifest-processor", "inference-proxy"}
        for app in scraped:
            assert PLATFORM_INGRESS_PORTS[app] == {8443}


# ─── gco-system egress ─────────────────────────────────────────────


class TestPlatformEgress:
    def test_platform_pods_may_reach_https(self, netpol_docs):
        policy = _find_netpol(netpol_docs, "allow-aws-api-egress", "gco-system")
        assert policy is not None
        assert policy["spec"]["podSelector"] == {"matchLabels": {"project": "gco"}}
        assert policy["spec"]["policyTypes"] == ["Egress"]
        assert policy["spec"]["egress"] == [{"ports": [{"protocol": "TCP", "port": 443}]}]

    def test_every_platform_pod_template_carries_the_egress_label(self):
        """A platform pod without project=gco would be cut off from AWS."""
        for filename in (
            "30-health-monitor.yaml",
            "31-manifest-processor.yaml",
            "32-inference-monitor.yaml",
            "33-inference-proxy.yaml",
            "34-cost-monitor.yaml",
            "post-helm-sqs-consumer.yaml",
        ):
            for doc in _stubbed_documents(MANIFESTS_DIR / filename):
                if doc["kind"] == "Deployment":
                    labels = doc["spec"]["template"]["metadata"]["labels"]
                elif doc["kind"] == "ScaledJob":
                    labels = doc["spec"]["jobTargetRef"]["template"]["metadata"]["labels"]
                else:
                    continue
                assert labels.get("project") == "gco", f"{filename}: {doc['metadata']['name']}"


# ─── DNS everywhere ────────────────────────────────────────────────


class TestDnsEgress:
    """DNS on UDP/TCP 53 to any resolver in every GCO namespace.

    EKS Auto Mode answers cluster DNS from a per-node system service, not from
    CoreDNS pods in kube-system, so a namespace-selected rule would match no
    destination there and break name resolution the moment enforcement is on.
    """

    @pytest.mark.parametrize("namespace", ["gco-system", "gco-jobs", "gco-inference"])
    def test_allow_dns_policy_exists_and_selects_all_pods(self, netpol_docs, namespace):
        policy = _find_netpol(netpol_docs, "allow-dns", namespace)
        assert policy is not None, f"{namespace} must have an allow-dns NetworkPolicy"
        assert policy["spec"]["podSelector"] == {}
        assert policy["spec"]["policyTypes"] == ["Egress"]

    @pytest.mark.parametrize("namespace", ["gco-system", "gco-jobs", "gco-inference"])
    def test_dns_policy_allows_udp_and_tcp_53_to_any_destination(self, netpol_docs, namespace):
        policy = _find_netpol(netpol_docs, "allow-dns", namespace)
        (rule,) = policy["spec"]["egress"]
        assert "to" not in rule, f"{namespace}: DNS rule must not be bound to a resolver location"
        assert _get_port_protocols(rule) == {("UDP", 53), ("TCP", 53)}


# ─── gco-jobs ──────────────────────────────────────────────────────


class TestJobNamespacePosture:
    """default-deny from outside, everything inside, DNS + HTTPS + in-VPC out."""

    #: Every Egress-type policy allowed to select ALL gco-jobs pods. Any other
    #: all-pods Egress policy would silently narrow what jobs may reach (the
    #: rules union, but every selected pod is isolated to that union).
    EXPECTED_ALL_PODS_EGRESS = {
        "allow-same-namespace",
        "allow-dns",
        "allow-https-egress",
        "allow-vpc-egress",
    }

    def test_same_namespace_traffic_is_allowed_both_ways(self, netpol_docs):
        policy = _find_netpol(netpol_docs, "allow-same-namespace", "gco-jobs")
        assert policy is not None
        assert policy["spec"]["podSelector"] == {}
        assert sorted(policy["spec"]["policyTypes"]) == ["Egress", "Ingress"]
        # A bare podSelector peer is "pods in this namespace"; no ports means
        # every port and protocol (frameworks pick theirs dynamically).
        assert policy["spec"]["ingress"] == [{"from": [{"podSelector": {}}]}]
        assert policy["spec"]["egress"] == [{"to": [{"podSelector": {}}]}]

    def test_https_egress_is_allowed_to_any_destination(self, netpol_docs):
        """S3, DynamoDB, ECR, CloudWatch, Bedrock, hubs and package indexes: 443."""
        policy = _find_netpol(netpol_docs, "allow-https-egress", "gco-jobs")
        assert policy is not None
        assert policy["spec"]["podSelector"] == {}
        assert policy["spec"]["egress"] == [{"ports": [{"protocol": "TCP", "port": 443}]}]

    def test_in_vpc_egress_is_allowed_on_any_port(self, netpol_docs):
        """Valkey, Aurora, EFS/FSx targets and VPC endpoints sit in the VPC ranges."""
        policy = _find_netpol(netpol_docs, "allow-vpc-egress", "gco-jobs")
        assert policy is not None
        assert policy["spec"]["podSelector"] == {}
        (rule,) = policy["spec"]["egress"]
        assert "ports" not in rule
        assert rule["to"] == [{"ipBlock": {"cidr": "10.0.0.0/16"}}]

    def test_no_other_policy_isolates_every_job_pod_on_egress(self, netpol_docs):
        all_pods_egress = {
            policy["metadata"]["name"]
            for policy in _policies_in(netpol_docs, "gco-jobs")
            if "Egress" in policy["spec"].get("policyTypes", [])
            and policy["spec"].get("podSelector") == {}
        }
        assert all_pods_egress == self.EXPECTED_ALL_PODS_EGRESS

    def test_no_job_ingress_rule_admits_other_namespaces(self, netpol_docs):
        """Cross-namespace sources need an operator rule; none ships here."""
        for policy in _policies_in(netpol_docs, "gco-jobs"):
            for rule in policy["spec"].get("ingress") or []:
                for peer in rule.get("from", []):
                    assert "namespaceSelector" not in peer, policy["metadata"]["name"]
                    assert "ipBlock" not in peer, policy["metadata"]["name"]

    @pytest.mark.parametrize("name", ["allow-vpc-endpoint-egress", "allow-ray-cluster-internal"])
    def test_retired_job_policies_are_gone_and_swept(self, netpol_docs, name):
        """The VPC-CIDR-only HTTPS rule and the Ray-only rule are subsumed.

        Neither is shipped any more, and the applier's legacy sweep removes
        them from upgraded clusters so the live set equals the shipped one.
        """
        assert _find_netpol(netpol_docs, name, "gco-jobs") is None
        handler = (MANIFESTS_DIR.parent / "handler.py").read_text(encoding="utf-8")
        assert f'("networking.k8s.io/v1", "NetworkPolicy", "gco-jobs", "{name}")' in handler


# ─── gco-inference ─────────────────────────────────────────────────


class TestInferenceNamespacePosture:
    def test_model_pods_may_reach_https(self, netpol_docs):
        policy = _find_netpol(netpol_docs, "allow-https-egress", "gco-inference")
        assert policy is not None
        assert policy["spec"]["podSelector"] == {}
        assert policy["spec"]["egress"] == [{"ports": [{"protocol": "TCP", "port": 443}]}]

    def test_alb_ingress_targets_only_inference_proxy(self, netpol_docs):
        policy = _find_netpol(netpol_docs, "allow-alb-to-inference-proxy", "gco-system")
        assert policy is not None
        assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "inference-proxy"}}

    def test_cross_namespace_egress_selects_only_inference_proxy(self, netpol_docs):
        policy = _find_netpol(netpol_docs, "allow-inference-proxy-to-inference", "gco-system")
        assert policy is not None
        assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "inference-proxy"}}
        peers = policy["spec"]["egress"][0]["to"]
        assert peers == [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "gco-inference"}
                },
                "podSelector": {"matchLabels": {"gco.io/type": "inference"}},
            }
        ]

    def test_model_ingress_accepts_only_inference_proxy(self, netpol_docs):
        policy = _find_netpol(netpol_docs, "allow-inference-proxy-ingress", "gco-inference")
        assert policy is not None
        assert policy["spec"]["podSelector"] == {"matchLabels": {"gco.io/type": "inference"}}
        peers = policy["spec"]["ingress"][0]["from"]
        assert peers == [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "gco-system"}},
                "podSelector": {"matchLabels": {"app": "inference-proxy"}},
            }
        ]


# ─── Enforcement switch ────────────────────────────────────────────


class TestEnforcementSwitch:
    """06-network-policy-controller.yaml turns the Auto Mode controller on."""

    def test_configmap_carries_both_documented_keys(self):
        (config_map,) = [
            doc for doc in yaml.safe_load_all(CONTROLLER_MANIFEST_PATH.read_text()) if doc
        ]
        assert config_map["kind"] == "ConfigMap"
        assert config_map["metadata"] == {
            "name": "amazon-vpc-cni",
            "namespace": "kube-system",
            "labels": {"project": "gco"},
        }
        assert config_map["data"] == {
            "enable-network-policy-controller": "{{NETWORK_POLICY_ENFORCEMENT}}",
            "enable-network-policy": "{{NETWORK_POLICY_ENFORCEMENT}}",
        }

    def test_regional_stack_renders_the_switch_from_config(self):
        source = (Path("gco/stacks/regional_stack.py")).read_text(encoding="utf-8")
        assert '"{{NETWORK_POLICY_ENFORCEMENT}}"' in source
        # Read like every other eks_cluster key: with the loader's default, so a
        # partial block (test fakes, hand-written context) still renders "true".
        assert 'get_eks_cluster_config().get("network_policy_enforcement", True)' in source

    def test_kind_ci_applies_the_switch_and_the_policies(self):
        workflow = Path(".github/workflows/integration-tests.yml").read_text(encoding="utf-8")
        assert "06-network-policy-controller.yaml" in workflow
        assert "s|{{NETWORK_POLICY_ENFORCEMENT}}|true|g" in workflow
        assert "03-network-policies.yaml" in workflow


# ─── CDK Substitution Logic ─────────────────────────────────────────


class TestCdkSubstitutionLogic:
    """
    Verify the `{{VPC_ENDPOINT_CIDR_BLOCKS}}` substitution logic from
    regional_stack.py produces valid YAML when applied to the placeholder.
    """

    def test_raw_manifest_contains_placeholder(self, raw_manifest):
        """Sanity check — the raw manifest should still have the placeholder."""
        assert CIDR_PLACEHOLDER in raw_manifest, (
            "Raw manifest must contain {{VPC_ENDPOINT_CIDR_BLOCKS}} placeholder"
        )

    def test_raw_manifest_is_not_valid_yaml(self, raw_manifest):
        """
        Before substitution, the placeholder sits where a list entry should be,
        so yaml.safe_load_all should either raise or produce a document where
        the placeholder survived as a string — confirming substitution is
        required before parsing.
        """
        try:
            docs = list(yaml.safe_load_all(raw_manifest))
        except yaml.YAMLError:
            return  # Acceptable — YAML rejects the placeholder form.
        # If parse succeeded, the placeholder should appear as a raw string
        # somewhere — it should NOT parse as a structured list entry.
        flat = yaml.dump(docs)
        assert CIDR_PLACEHOLDER in flat or "VPC_ENDPOINT_CIDR_BLOCKS" in flat

    @pytest.mark.parametrize(
        "cidrs",
        [
            ["10.0.0.0/16"],
            ["10.0.0.0/16", "10.1.0.0/16"],
            ["10.0.0.0/16", "172.16.0.0/12", "192.168.0.0/16"],
        ],
    )
    def test_substitution_produces_valid_yaml(self, raw_manifest, cidrs):
        """Substituted manifest must parse cleanly as YAML."""
        docs = _substitute_and_load(raw_manifest, cidrs)
        # Sanity: we should have at least a handful of documents
        assert len(docs) > 0
        # Verify every document has a kind (no parse anomalies)
        for d in docs:
            assert "kind" in d, f"Parsed doc missing kind: {d}"

    @pytest.mark.parametrize(
        "cidrs",
        [
            ["10.0.0.0/16"],
            ["10.0.0.0/16", "172.16.0.0/12"],
            ["10.0.0.0/16", "172.16.0.0/12", "192.168.0.0/16"],
        ],
    )
    def test_substitution_injects_all_cidrs(self, raw_manifest, cidrs):
        """Every CIDR passed to the substitution must appear in the rendered policy."""
        docs = _substitute_and_load(raw_manifest, cidrs)
        policy = _find_netpol(docs, "allow-vpc-egress", "gco-jobs")
        assert policy is not None
        rendered_cidrs: set[str] = set()
        for rule in policy["spec"].get("egress", []):
            for peer in rule.get("to", []):
                ip_block = peer.get("ipBlock")
                if ip_block is not None:
                    rendered_cidrs.add(ip_block["cidr"])
        for cidr in cidrs:
            assert cidr in rendered_cidrs, (
                f"CIDR {cidr} missing from rendered allow-vpc-egress policy "
                f"(rendered: {rendered_cidrs})"
            )

    def test_substitution_single_cidr_matches_default(self, raw_manifest):
        """
        The default fallback in regional_stack.py is ["10.0.0.0/16"]. Verify
        that specific substitution renders exactly one ipBlock.
        """
        docs = _substitute_and_load(raw_manifest, ["10.0.0.0/16"])
        policy = _find_netpol(docs, "allow-vpc-egress", "gco-jobs")
        assert policy is not None
        ip_blocks = []
        for rule in policy["spec"].get("egress", []):
            for peer in rule.get("to", []):
                if "ipBlock" in peer:
                    ip_blocks.append(peer["ipBlock"])
        assert len(ip_blocks) == 1
        assert ip_blocks[0]["cidr"] == "10.0.0.0/16"
