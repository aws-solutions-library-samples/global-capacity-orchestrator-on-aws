"""
Tests for the NetworkPolicy manifest at
lambda/kubectl-applier-simple/manifests/03-network-policies.yaml.

Asserts the gco-jobs CIDR-restricted egress posture (port 443 traffic
gated by allow-vpc-endpoint-egress rather than a wildcard
allow-https-egress), DNS egress still permitted on UDP/TCP 53,
default-deny-ingress present in both gco-system and gco-jobs, and no
stray unrestricted HTTPS egress policy. Replays the CDK-side
{{VPC_ENDPOINT_CIDR_BLOCKS}} substitution locally via
_render_cidr_block_substitution so the tests exercise the same YAML
the regional stack would emit at deploy time.
"""

from pathlib import Path

import pytest
import yaml

NETPOL_MANIFEST_PATH = Path("lambda/kubectl-applier-simple/manifests/03-network-policies.yaml")

# Placeholder as used by kubectl-applier Lambda substitution (see regional_stack.py).
CIDR_PLACEHOLDER = "{{VPC_ENDPOINT_CIDR_BLOCKS}}"


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


def _get_port_protocols(egress_rule: dict) -> set[tuple[str, int]]:
    """Return the set of (protocol, port) tuples referenced by an egress rule."""
    result = set()
    for port in egress_rule.get("ports", []):
        proto = port.get("protocol", "TCP")
        port_num = port.get("port")
        if port_num is not None:
            result.add((proto, port_num))
    return result


# ─── allow-vpc-endpoint-egress in gco-jobs ──────────────────────────


class TestVpcEndpointEgressPolicy:
    """CIDR-restricted HTTPS egress in gco-jobs."""

    def test_allow_vpc_endpoint_egress_exists(self, netpol_docs):
        policy = _find_netpol(netpol_docs, "allow-vpc-endpoint-egress", "gco-jobs")
        assert policy is not None, "gco-jobs must have an allow-vpc-endpoint-egress NetworkPolicy"

    def test_old_unrestricted_https_egress_removed(self, netpol_docs):
        """The old unrestricted allow-https-egress policy must not exist in gco-jobs."""
        policy = _find_netpol(netpol_docs, "allow-https-egress", "gco-jobs")
        assert policy is None, (
            "Old unrestricted allow-https-egress policy must be removed from gco-jobs"
        )

    def test_vpc_endpoint_egress_has_egress_policy_type(self, netpol_docs):
        policy = _find_netpol(netpol_docs, "allow-vpc-endpoint-egress", "gco-jobs")
        assert "Egress" in policy["spec"]["policyTypes"]

    def test_vpc_endpoint_egress_restricts_to_cidr_blocks(self, netpol_docs):
        """The egress rule must reference ipBlock entries (not be unrestricted)."""
        policy = _find_netpol(netpol_docs, "allow-vpc-endpoint-egress", "gco-jobs")
        egress_rules = policy["spec"].get("egress", [])
        assert len(egress_rules) >= 1, "Must have at least one egress rule"

        # At least one rule must have `to` peers that are all ipBlock entries
        found_ip_block_rule = False
        for rule in egress_rules:
            to_peers = rule.get("to", [])
            if to_peers and all("ipBlock" in peer for peer in to_peers):
                found_ip_block_rule = True
                break
        assert found_ip_block_rule, (
            "allow-vpc-endpoint-egress must restrict egress via ipBlock peers"
        )

    def test_vpc_endpoint_egress_not_open_to_world(self, netpol_docs):
        """No CIDR should be 0.0.0.0/0 or ::/0 — that would be unrestricted."""
        policy = _find_netpol(netpol_docs, "allow-vpc-endpoint-egress", "gco-jobs")
        for rule in policy["spec"].get("egress", []):
            for peer in rule.get("to", []):
                ip_block = peer.get("ipBlock")
                if ip_block is not None:
                    cidr = ip_block.get("cidr", "")
                    assert cidr not in (
                        "0.0.0.0/0",
                        "::/0",
                    ), f"allow-vpc-endpoint-egress must not allow world-wide CIDR {cidr}"

    def test_vpc_endpoint_egress_restricts_port_443(self, netpol_docs):
        """The CIDR-restricted egress rule must be scoped to port 443 TCP."""
        policy = _find_netpol(netpol_docs, "allow-vpc-endpoint-egress", "gco-jobs")
        for rule in policy["spec"].get("egress", []):
            to_peers = rule.get("to", [])
            if to_peers and all("ipBlock" in peer for peer in to_peers):
                ports = _get_port_protocols(rule)
                assert ("TCP", 443) in ports, "CIDR-restricted egress rule must allow TCP/443"


# ─── DNS Egress in gco-jobs ─────────────────────────────────────────


class TestDnsEgressGcoJobs:
    """Validates: DNS egress (port 53 UDP/TCP) is still allowed in gco-jobs."""

    def test_allow_dns_policy_exists(self, netpol_docs):
        policy = _find_netpol(netpol_docs, "allow-dns", "gco-jobs")
        assert policy is not None, "gco-jobs must have an allow-dns NetworkPolicy"

    def test_dns_policy_allows_udp_and_tcp_53(self, netpol_docs):
        policy = _find_netpol(netpol_docs, "allow-dns", "gco-jobs")
        all_ports: set[tuple[str, int]] = set()
        for rule in policy["spec"].get("egress", []):
            all_ports.update(_get_port_protocols(rule))
        assert ("UDP", 53) in all_ports, "DNS policy must allow UDP port 53"
        assert ("TCP", 53) in all_ports, "DNS policy must allow TCP port 53"

    def test_dns_policy_targets_kube_system(self, netpol_docs):
        """DNS egress should target the kube-system namespace (kube-dns)."""
        policy = _find_netpol(netpol_docs, "allow-dns", "gco-jobs")
        found_kube_system = False
        for rule in policy["spec"].get("egress", []):
            for peer in rule.get("to", []):
                ns_selector = peer.get("namespaceSelector", {})
                labels = ns_selector.get("matchLabels", {})
                if labels.get("kubernetes.io/metadata.name") == "kube-system":
                    found_kube_system = True
        assert found_kube_system, "DNS egress should target kube-system namespace"


# ─── No Unrestricted HTTPS Egress in gco-jobs ──────────────────────


class TestNoUnrestrictedHttpsEgress:
    """no unrestricted egress on port 443 in gco-jobs."""

    def test_no_gco_jobs_egress_policy_allows_443_without_cidr(self, netpol_docs):
        """
        Any egress policy in gco-jobs that opens port 443 must restrict the peer
        set via ipBlock. A rule with port 443 and no `to` peers (empty list)
        means "any destination" — that is the threat we are guarding against.
        """
        gco_jobs_policies = [
            d
            for d in netpol_docs
            if d.get("kind") == "NetworkPolicy"
            and d["metadata"]["namespace"] == "gco-jobs"
            and "Egress" in d["spec"].get("policyTypes", [])
        ]
        for policy in gco_jobs_policies:
            for rule in policy["spec"].get("egress", []):
                ports = _get_port_protocols(rule)
                if ("TCP", 443) in ports:
                    to_peers = rule.get("to", [])
                    assert to_peers, (
                        f"Policy '{policy['metadata']['name']}' has unrestricted "
                        f"TCP/443 egress (empty `to` peers)"
                    )
                    for peer in to_peers:
                        assert "ipBlock" in peer, (
                            f"Policy '{policy['metadata']['name']}' TCP/443 egress "
                            f"peer is not an ipBlock: {peer}"
                        )


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


# ─── Dedicated Inference Proxy Boundaries ───────────────────────────


class TestInferenceProxyPolicies:
    """Only the dedicated proxy may bridge ALB traffic to model pods."""

    def test_alb_ingress_targets_only_inference_proxy(self, netpol_docs):
        policy = _find_netpol(netpol_docs, "allow-alb-to-inference-proxy", "gco-system")
        assert policy is not None
        assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "inference-proxy"}}
        assert policy["spec"]["podSelector"] != {"matchLabels": {"app": "manifest-processor"}}

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
        assert all(
            peer.get("podSelector", {}).get("matchLabels", {}).get("app") != "manifest-processor"
            for peer in peers
        )


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
        policy = _find_netpol(docs, "allow-vpc-endpoint-egress", "gco-jobs")
        assert policy is not None
        rendered_cidrs: set[str] = set()
        for rule in policy["spec"].get("egress", []):
            for peer in rule.get("to", []):
                ip_block = peer.get("ipBlock")
                if ip_block is not None:
                    rendered_cidrs.add(ip_block["cidr"])
        for cidr in cidrs:
            assert cidr in rendered_cidrs, (
                f"CIDR {cidr} missing from rendered allow-vpc-endpoint-egress policy "
                f"(rendered: {rendered_cidrs})"
            )

    def test_substitution_single_cidr_matches_default(self, raw_manifest):
        """
        The default fallback in regional_stack.py is ["10.0.0.0/16"]. Verify
        that specific substitution renders exactly one ipBlock.
        """
        docs = _substitute_and_load(raw_manifest, ["10.0.0.0/16"])
        policy = _find_netpol(docs, "allow-vpc-endpoint-egress", "gco-jobs")
        assert policy is not None
        ip_blocks = []
        for rule in policy["spec"].get("egress", []):
            for peer in rule.get("to", []):
                if "ipBlock" in peer:
                    ip_blocks.append(peer["ipBlock"])
        assert len(ip_blocks) == 1
        assert ip_blocks[0]["cidr"] == "10.0.0.0/16"
