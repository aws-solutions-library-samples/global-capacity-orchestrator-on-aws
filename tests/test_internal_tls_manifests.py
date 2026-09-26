"""GCO's internal PKI and verified in-cluster HTTPS, pinned to the manifests.

Every in-cluster hop GCO owns is HTTPS verified against one private CA:

* ``post-helm-api-workload-certificates.yaml`` holds the chain (a selfSigned
  bootstrap ClusterIssuer, the ``gco-internal-ca`` CA Certificate in
  cert-manager's cluster resource namespace, the ``gco-internal-ca``
  ClusterIssuer) and the always-on leaves; the leaves of optional features sit
  in files gated like the feature, so the applier's unresolved-placeholder
  rule skips (and prunes) them together with it.
* Servers terminate TLS in a sidecar that mounts the leaf; clients project only
  the ``ca.crt`` key of a Secret in their own namespace and never see a key.
* Prometheus, the cost monitor, the manifest processor, the inference proxy
  and the Grafana rotator all name the Service host their peer's leaf carries.

* ``07-internal-ca-issuance.yaml`` fences who may get the CA to sign: its
  ValidatingAdmissionPolicy's own CEL runs through a small interpreter here, over
  every shipped Certificate and over the tenant objects it must refuse.

The four traced services also carry the tracing switches, and nothing else
does. These tests parse the shipped manifests the way the applier would.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFESTS_DIR = REPO_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
HANDLER_DIR = MANIFESTS_DIR.parent
CHARTS_FILE = REPO_ROOT / "lambda" / "helm-installer" / "charts.yaml"
INTEGRATION_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "integration-tests.yml"

CORE_PKI = "post-helm-api-workload-certificates.yaml"
FENCE = "07-internal-ca-issuance.yaml"
COST_TLS = "post-helm-cost-monitoring-tls.yaml"
MONITORING = "post-helm-monitoring-servicemonitors.yaml"
MONITORING_TLS = "post-helm-monitoring-tls.yaml"
ROTATION = "post-helm-grafana-credential-rotation.yaml"

COST_GATE = "{{COST_MONITORING_ENABLED}}"
OBSERVABILITY_GATE = "{{CLUSTER_OBSERVABILITY_ENABLED}}"
CA_ISSUER_REF = {"name": "gco-internal-ca", "kind": "ClusterIssuer", "group": "cert-manager.io"}
CA_BUNDLE = "/var/run/gco/ca/ca.crt"
TLS_PROXY_COMMAND = ["python", "-m", "gco.services.tls_proxy"]

#: Typed stubs, like the regional stack renders them (integers and quantities
#: where Kubernetes wants them); every other token becomes a string.
_TYPED_TOKENS = {
    "{{MP_REPLICAS}}": "3",
    "{{INFERENCE_PROXY_MIN_REPLICAS}}": "3",
    "{{INFERENCE_PROXY_MAX_REPLICAS}}": "10",
    "{{INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION}}": "70",
    "{{INFERENCE_PROXY_TLS_CPU_REQUEST}}": "100m",
    "{{MP_CPU_LIMIT}}": "1000m",
    "{{MP_MEMORY_LIMIT}}": "2Gi",
    "{{VPC_ENDPOINT_CIDR_BLOCKS}}": '- ipBlock:\n            cidr: "10.0.0.0/16"',
}


def _service_names(service: str, namespace: str) -> list[str]:
    return [
        service,
        f"{service}.{namespace}",
        f"{service}.{namespace}.svc",
        f"{service}.{namespace}.svc.cluster.local",
    ]


#: Every leaf the internal CA signs: name -> (manifest, namespace, dnsNames).
LEAVES: dict[str, tuple[str, str, list[str]]] = {
    "health-monitor-tls": (CORE_PKI, "gco-system", _service_names("health-monitor", "gco-system")),
    "manifest-processor-tls": (
        CORE_PKI,
        "gco-system",
        _service_names("manifest-processor", "gco-system"),
    ),
    "inference-proxy-tls": (
        CORE_PKI,
        "gco-system",
        _service_names("inference-proxy", "gco-system"),
    ),
    "inference-monitor-tls": (
        CORE_PKI,
        "gco-system",
        _service_names("inference-monitor", "gco-system"),
    ),
    "gco-inference-tls": (
        CORE_PKI,
        "gco-inference",
        ["*.gco-inference.svc", "*.gco-inference.svc.cluster.local"],
    ),
    "cost-monitor-tls": (COST_TLS, "gco-system", _service_names("cost-monitor", "gco-system")),
    "opencost-tls": (COST_TLS, "monitoring", _service_names("opencost-tls", "monitoring")),
    "grafana-tls": (MONITORING_TLS, "monitoring", _service_names("grafana-tls", "monitoring")),
    "gco-monitoring-trust": (
        MONITORING_TLS,
        "monitoring",
        _service_names("gco-monitoring-trust", "monitoring"),
    ),
}

#: The traced services' application containers (never their sidecars).
TRACED_CONTAINERS = {
    "30-health-monitor.yaml": "health-monitor",
    "31-manifest-processor.yaml": "manifest-processor",
    "33-inference-proxy.yaml": "inference-proxy",
    "34-cost-monitor.yaml": "cost-monitor",
}


def _raw(name: str) -> str:
    return (MANIFESTS_DIR / name).read_text(encoding="utf-8")


def _documents(name: str) -> list[dict[str, Any]]:
    text = _raw(name)
    for token, value in _TYPED_TOKENS.items():
        text = text.replace(token, value)
    text = re.sub(r"\{\{[A-Z0-9_]+\}\}", "placeholder", text)
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def _all_documents() -> list[tuple[str, dict[str, Any]]]:
    return [
        (path.name, doc)
        for path in sorted(MANIFESTS_DIR.glob("*.yaml"))
        for doc in _documents(path.name)
    ]


def _find(name: str, kind: str, object_name: str) -> dict[str, Any]:
    (doc,) = [
        doc
        for doc in _documents(name)
        if doc["kind"] == kind and doc["metadata"]["name"] == object_name
    ]
    return doc


def _deployment(name: str) -> dict[str, Any]:
    (doc,) = [doc for doc in _documents(name) if doc["kind"] == "Deployment"]
    return doc


def _containers(workload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        container["name"]: container
        for container in workload["spec"]["template"]["spec"]["containers"]
    }


def _env(container: dict[str, Any]) -> dict[str, Any]:
    return {item["name"]: item.get("value", item.get("valueFrom")) for item in container["env"]}


def _pod_specs() -> list[tuple[str, str, str, dict[str, Any]]]:
    """(file, kind, name, pod spec) for every workload with a pod template."""
    found = []
    for filename, doc in _all_documents():
        kind = doc["kind"]
        if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
            spec = doc["spec"]["template"]["spec"]
        elif kind == "CronJob":
            spec = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        elif kind == "ScaledJob":
            spec = doc["spec"]["jobTargetRef"]["template"]["spec"]
        else:
            continue
        found.append((filename, kind, doc["metadata"]["name"], spec))
    return found


@pytest.fixture(scope="module")
def handler_module():
    sys.path.insert(0, str(HANDLER_DIR))
    try:
        sys.modules.pop("handler", None)
        import handler

        yield handler
    finally:
        sys.path.pop(0)
        sys.modules.pop("handler", None)


# ─── The chain ─────────────────────────────────────────────────────


class TestCertificateAuthority:
    def test_the_bootstrap_issuer_is_cluster_scoped_and_self_signed(self) -> None:
        issuer = _find(CORE_PKI, "ClusterIssuer", "gco-internal-ca-bootstrap")
        assert "namespace" not in issuer["metadata"]
        assert issuer["spec"] == {"selfSigned": {}}

    def test_the_ca_certificate_is_a_long_lived_p256_ca_that_keeps_its_key(self) -> None:
        ca = _find(CORE_PKI, "Certificate", "gco-internal-ca")
        assert ca["metadata"]["namespace"] == "cert-manager"
        spec = ca["spec"]
        assert spec["isCA"] is True
        assert spec["commonName"] == "GCO internal CA"
        assert spec["secretName"] == "gco-internal-ca"
        assert spec["duration"] == "87600h"
        # Renewal keeps the key, so a renewed CA still verifies every leaf
        # its predecessor signed; the overlap outlives every 90-day leaf.
        assert spec["privateKey"] == {
            "algorithm": "ECDSA",
            "encoding": "PKCS8",
            "size": 256,
            "rotationPolicy": "Never",
        }
        assert int(spec["renewBefore"].removesuffix("h")) > 2160
        assert "cert sign" in spec["usages"]
        assert spec["issuerRef"] == {
            "name": "gco-internal-ca-bootstrap",
            "kind": "ClusterIssuer",
            "group": "cert-manager.io",
        }

    def test_the_ca_issuer_signs_from_the_ca_secret(self) -> None:
        issuer = _find(CORE_PKI, "ClusterIssuer", "gco-internal-ca")
        assert "namespace" not in issuer["metadata"]
        assert issuer["spec"] == {"ca": {"secretName": "gco-internal-ca"}}

    def test_the_ca_secret_lives_in_cert_managers_cluster_resource_namespace(self) -> None:
        """A ClusterIssuer reads its CA Secret from exactly one namespace.

        That is cert-manager's --cluster-resource-namespace, which defaults to
        the namespace the chart runs in unless the values override it.
        """
        chart = yaml.safe_load(CHARTS_FILE.read_text(encoding="utf-8"))["charts"]["cert-manager"]
        assert chart["enabled"] is True
        assert "clusterResourceNamespace" not in chart.get("values", {})
        ca = _find(CORE_PKI, "Certificate", "gco-internal-ca")
        assert ca["metadata"]["namespace"] == chart["namespace"] == "cert-manager"

    def test_only_the_ca_is_signed_by_the_bootstrap_issuer(self) -> None:
        by_bootstrap = [
            doc["metadata"]["name"]
            for _file, doc in _all_documents()
            if doc["kind"] == "Certificate"
            and doc["spec"]["issuerRef"]["name"] == "gco-internal-ca-bootstrap"
        ]
        assert by_bootstrap == ["gco-internal-ca"]

    def test_gco_ships_exactly_these_issuers(self) -> None:
        issuers = {
            (doc["kind"], doc["metadata"]["name"])
            for _file, doc in _all_documents()
            if doc["kind"] in {"Issuer", "ClusterIssuer"}
        }
        assert issuers == {
            ("ClusterIssuer", "gco-internal-ca-bootstrap"),
            ("ClusterIssuer", "gco-internal-ca"),
        }

    def test_the_core_pki_file_is_never_gated(self) -> None:
        """No feature placeholder: the chain and the platform leaves always apply."""
        assert not re.search(r"\{\{[A-Z0-9_]+\}\}", _raw(CORE_PKI))


# ─── The leaves ────────────────────────────────────────────────────


class TestLeaves:
    def test_the_leaf_inventory_is_complete(self) -> None:
        shipped = {
            doc["metadata"]["name"]: filename
            for filename, doc in _all_documents()
            if doc["kind"] == "Certificate" and not doc["spec"].get("isCA")
        }
        assert shipped == {name: leaf[0] for name, leaf in LEAVES.items()}

    @pytest.mark.parametrize("name", sorted(LEAVES))
    def test_every_leaf_is_a_rotating_p256_server_leaf_from_the_internal_ca(
        self, name: str
    ) -> None:
        filename, namespace, dns_names = LEAVES[name]
        leaf = _find(filename, "Certificate", name)
        assert leaf["metadata"]["namespace"] == namespace
        spec = leaf["spec"]
        assert spec["secretName"] == name
        assert spec["issuerRef"] == CA_ISSUER_REF
        assert spec["dnsNames"] == dns_names
        assert spec["privateKey"] == {
            "algorithm": "ECDSA",
            "encoding": "PKCS8",
            "size": 256,
            "rotationPolicy": "Always",
        }
        assert (spec["duration"], spec["renewBefore"]) == ("2160h", "720h")
        assert spec["usages"] == ["digital signature", "server auth"]
        assert spec["revisionHistoryLimit"] == 1
        assert "isCA" not in spec and "commonName" not in spec

    def test_the_model_endpoint_leaf_is_one_wildcard_label(self) -> None:
        _file, _namespace, dns_names = LEAVES["gco-inference-tls"]
        for name in dns_names:
            assert name.startswith("*.") and name.count("*") == 1

    @pytest.mark.parametrize(
        ("filename", "gate", "annotation"),
        [
            (COST_TLS, COST_GATE, "gco.aws/feature-gate"),
            (MONITORING_TLS, OBSERVABILITY_GATE, "gco.io/cluster-observability-enabled"),
            # The monitors that consume gco-monitoring-trust share its gate.
            (MONITORING, OBSERVABILITY_GATE, "gco.io/cluster-observability-enabled"),
        ],
    )
    def test_gated_leaves_sit_in_files_gated_like_their_feature(
        self, filename: str, gate: str, annotation: str
    ) -> None:
        raw = _raw(filename)
        assert set(re.findall(r"\{\{[A-Z0-9_]+\}\}", raw)) == {gate}
        for doc in yaml.safe_load_all(raw):
            if doc:
                assert doc["metadata"]["annotations"][annotation] == gate, doc["metadata"]

    def test_certificates_are_post_helm(self) -> None:
        """cert-manager's CRDs come from its chart: a base-pass Certificate cannot apply."""
        for filename, doc in _all_documents():
            if doc["apiVersion"].startswith("cert-manager.io/"):
                assert filename.startswith("post-helm-"), filename


# ─── Servers: sidecars hold the keys ───────────────────────────────


def _tls_sidecars() -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    """(file, workload, pod spec, container) for every TLS proxy sidecar GCO ships."""
    found = []
    for filename, _kind, workload, spec in _pod_specs():
        for container in spec["containers"]:
            if container.get("command") == TLS_PROXY_COMMAND:
                found.append((filename, workload, spec, container))
    return found


class TestServers:
    def test_every_platform_tls_listener_is_accounted_for(self) -> None:
        assert {
            (workload, container["name"]) for _f, workload, _s, container in _tls_sidecars()
        } == {
            ("health-monitor", "api-tls-proxy"),
            ("manifest-processor", "api-tls-proxy"),
            ("inference-proxy", "api-tls-proxy"),
            ("cost-monitor", "api-tls-proxy"),
            ("inference-monitor", "metrics-tls-proxy"),
        }

    @pytest.mark.parametrize(
        ("filename", "workload", "secret"),
        [
            ("30-health-monitor.yaml", "health-monitor", "health-monitor-tls"),
            ("31-manifest-processor.yaml", "manifest-processor", "manifest-processor-tls"),
            ("32-inference-monitor.yaml", "inference-monitor", "inference-monitor-tls"),
            ("33-inference-proxy.yaml", "inference-proxy", "inference-proxy-tls"),
            ("34-cost-monitor.yaml", "cost-monitor", "cost-monitor-tls"),
        ],
    )
    def test_each_sidecar_alone_mounts_its_leaf(
        self, filename: str, workload: str, secret: str
    ) -> None:
        deployment = _deployment(filename)
        spec = deployment["spec"]["template"]["spec"]
        volumes = {volume["name"]: volume for volume in spec["volumes"]}
        (sidecar,) = [c for c in spec["containers"] if c.get("command") == TLS_PROXY_COMMAND]
        (mount,) = sidecar["volumeMounts"]
        assert mount == {"name": mount["name"], "mountPath": "/var/run/gco/tls", "readOnly": True}
        assert volumes[mount["name"]]["secret"] == {"secretName": secret, "defaultMode": 0o440}
        assert LEAVES[secret][1] == deployment["metadata"]["namespace"]
        for container in spec["containers"]:
            if container is not sidecar:
                names = {m["name"] for m in container.get("volumeMounts", [])}
                assert mount["name"] not in names, container["name"]
        # The sidecar runs from the application's own image and is skipped by
        # the Pod Identity webhook.
        (application,) = [c for c in spec["containers"] if c is not sidecar]
        assert sidecar["image"] == application["image"]
        annotations = deployment["spec"]["template"]["metadata"]["annotations"]
        assert annotations["eks.amazonaws.com/skip-containers"] == sidecar["name"]
        assert spec["securityContext"]["fsGroup"] == 1000
        env = _env(sidecar)
        assert env["GCO_TLS_CERT_FILE"] == "/var/run/gco/tls/tls.crt"
        assert env["GCO_TLS_KEY_FILE"] == "/var/run/gco/tls/tls.key"
        # GCO pods mount their Secret non-optionally, so the proxy keeps the
        # fail-closed start (no keypair wait).
        assert "TLS_PROXY_KEYPAIR_WAIT_SECONDS" not in env


# ─── Clients: only ca.crt, only from their own namespace ───────────


class TestClients:
    def test_no_client_container_mounts_a_private_key(self) -> None:
        """Outside the TLS sidecars, a leaf Secret is only ever projected as ca.crt."""
        checked = 0
        for filename, _kind, workload, spec in _pod_specs():
            volumes = {volume["name"]: volume for volume in spec.get("volumes", [])}
            for container in spec["containers"]:
                if container.get("command") == TLS_PROXY_COMMAND:
                    continue
                for mount in container.get("volumeMounts", []):
                    secret = volumes.get(mount["name"], {}).get("secret")
                    if not secret or secret["secretName"] not in LEAVES:
                        continue
                    checked += 1
                    where = f"{filename}:{workload}/{container['name']}"
                    assert secret["items"] == [{"key": "ca.crt", "path": "ca.crt"}], where
                    assert mount == {
                        "name": mount["name"],
                        "mountPath": "/var/run/gco/ca",
                        "readOnly": True,
                    }, where
                    assert _env(container)["GCO_INTERNAL_CA_FILE"] == CA_BUNDLE, where
        # manifest processor, inference proxy, cost monitor, Grafana rotator.
        assert checked == 4

    @pytest.mark.parametrize(
        ("filename", "container_name", "secret"),
        [
            ("31-manifest-processor.yaml", "manifest-processor", "manifest-processor-tls"),
            ("33-inference-proxy.yaml", "inference-proxy", "inference-proxy-tls"),
            ("34-cost-monitor.yaml", "cost-monitor", "cost-monitor-tls"),
        ],
    )
    def test_platform_clients_trust_their_own_leafs_ca(
        self, filename: str, container_name: str, secret: str
    ) -> None:
        deployment = _deployment(filename)
        spec = deployment["spec"]["template"]["spec"]
        volumes = {volume["name"]: volume for volume in spec["volumes"]}
        assert volumes["internal-ca"]["secret"] == {
            "secretName": secret,
            "items": [{"key": "ca.crt", "path": "ca.crt"}],
            "defaultMode": 0o440,
        }
        mounts = {m["name"] for m in _containers(deployment)[container_name]["volumeMounts"]}
        assert "internal-ca" in mounts
        assert LEAVES[secret][1] == deployment["metadata"]["namespace"]

    def test_the_rotator_trusts_the_monitoring_namespace_copy_of_the_ca(self) -> None:
        cronjob = _find(ROTATION, "CronJob", "gco-grafana-admin-password-rotation")
        spec = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        (volume,) = spec["volumes"]
        assert volume["secret"]["secretName"] == "gco-monitoring-trust"
        assert LEAVES["gco-monitoring-trust"][1] == cronjob["metadata"]["namespace"]


# ─── Hops: every client names the Service its peer's leaf carries ──


def _https_endpoint(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    assert parts.scheme == "https", url
    assert parts.hostname is not None and parts.port is not None, url
    return parts.hostname, parts.port


class TestHops:
    def test_manifest_processor_to_cost_monitor(self) -> None:
        from gco.services.api_routes import cost

        host, port = _https_endpoint(cost._DEFAULT_COST_MONITOR_URL)
        assert host in LEAVES["cost-monitor-tls"][2]
        service = _find("34-cost-monitor.yaml", "Service", "cost-monitor")
        assert host == "cost-monitor.gco-system.svc.cluster.local"
        assert service["spec"]["ports"] == [
            {"port": port, "targetPort": "https", "protocol": "TCP", "name": "https"}
        ]
        # 31 relies on the code default rather than restating it.
        env = _env(_containers(_deployment("31-manifest-processor.yaml"))["manifest-processor"])
        assert "COST_MONITOR_URL" not in env

    def test_cost_monitor_to_opencost(self) -> None:
        from gco.services import cost_monitor

        container = _containers(_deployment("34-cost-monitor.yaml"))["cost-monitor"]
        url = _env(container)["OPENCOST_BASE_URL"]
        assert url == cost_monitor.DEFAULT_OPENCOST_BASE_URL
        host, port = _https_endpoint(url)
        assert host in LEAVES["opencost-tls"][2]
        service = _find(COST_TLS, "Service", "opencost-tls")
        assert service["metadata"]["namespace"] == "monitoring"
        assert service["spec"]["ports"] == [
            {"port": port, "targetPort": "https", "protocol": "TCP", "name": "https"}
        ]
        # The chart's own selector labels: release name == charts.yaml key.
        charts = yaml.safe_load(CHARTS_FILE.read_text(encoding="utf-8"))["charts"]
        assert charts["opencost"]["namespace"] == "monitoring"
        assert service["spec"]["selector"] == {
            "app.kubernetes.io/name": "opencost",
            "app.kubernetes.io/instance": "opencost",
        }

    def test_rotator_to_grafana(self) -> None:
        from gco.services import grafana_rotator

        cronjob = _find(ROTATION, "CronJob", "gco-grafana-admin-password-rotation")
        (container,) = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
        url = _env(container)["GRAFANA_SERVICE_URL"]
        assert url == grafana_rotator.DEFAULT_SERVICE_URL
        assert _env(container)["GCO_INTERNAL_CA_FILE"] == CA_BUNDLE
        host, port = _https_endpoint(url)
        assert host in LEAVES["grafana-tls"][2]
        service = _find(MONITORING_TLS, "Service", "grafana-tls")
        assert service["spec"]["ports"] == [
            {"port": port, "targetPort": "https", "protocol": "TCP", "name": "https"}
        ]
        charts = yaml.safe_load(CHARTS_FILE.read_text(encoding="utf-8"))["charts"]
        assert charts["kube-prometheus-stack"]["namespace"] == "monitoring"
        assert service["spec"]["selector"] == {
            "app.kubernetes.io/name": "grafana",
            "app.kubernetes.io/instance": "kube-prometheus-stack",
        }

    def test_prometheus_verifies_every_gco_scrape(self) -> None:
        monitors = [
            doc
            for doc in _documents(MONITORING)
            if doc["kind"] == "PodMonitor" and doc["metadata"]["name"].startswith("gco-")
        ]
        assert len(monitors) == 4
        for monitor in monitors:
            app = monitor["spec"]["selector"]["matchLabels"]["app"]
            (endpoint,) = monitor["spec"]["podMetricsEndpoints"]
            assert endpoint["scheme"] == "https"
            tls = endpoint["tlsConfig"]
            assert "insecureSkipVerify" not in tls
            assert tls["ca"] == {"secret": {"name": "gco-monitoring-trust", "key": "ca.crt"}}
            assert tls["serverName"] in LEAVES[f"{app}-tls"][2]
            assert monitor["metadata"]["namespace"] == LEAVES["gco-monitoring-trust"][1]

    def test_only_kueues_own_certificate_goes_unverified(self) -> None:
        unverified = [
            doc["metadata"]["name"]
            for filename, doc in _all_documents()
            if "insecureSkipVerify" in yaml.safe_dump(doc)
        ]
        assert unverified == ["gco-kueue"]


# ─── The cost monitor pod ──────────────────────────────────────────


class TestCostMonitorPod:
    @pytest.fixture(scope="class")
    @staticmethod
    def pod() -> dict[str, dict[str, Any]]:
        return _containers(_deployment("34-cost-monitor.yaml"))

    def test_the_application_listens_on_loopback_behind_the_sidecar(self, pod) -> None:
        assert set(pod) == {"cost-monitor", "api-tls-proxy"}
        app_env, proxy_env = _env(pod["cost-monitor"]), _env(pod["api-tls-proxy"])
        assert (app_env["HOST"], app_env["PORT"]) == ("127.0.0.1", "8080")
        assert pod["cost-monitor"]["ports"] == [
            {"name": "app-http", "containerPort": 8080, "protocol": "TCP"}
        ]
        assert proxy_env["TLS_PROXY_UPSTREAM_PORT"] == app_env["PORT"]
        assert pod["api-tls-proxy"]["ports"] == [
            {"name": "https", "containerPort": 8443, "protocol": "TCP"}
        ]
        drain = "GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS"
        assert proxy_env[drain] == app_env[drain]
        assert pod["api-tls-proxy"]["lifecycle"] == pod["cost-monitor"]["lifecycle"]

    def test_the_sidecar_is_probed_over_loopback_because_8443_admits_one_client(self, pod) -> None:
        sidecar = pod["api-tls-proxy"]
        for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
            source = sidecar[probe]["exec"]["command"][-1]
            assert "socket.create_connection(('127.0.0.1',8443),3)" in source
        readiness = sidecar["readinessProbe"]["exec"]["command"][-1]
        assert "wrap_socket" in readiness and "GET /readyz " in readiness

    def test_the_network_policies_carry_only_tls_ports(self) -> None:
        ingress = _find(
            "34-cost-monitor.yaml",
            "NetworkPolicy",
            "allow-manifest-processor-to-cost-monitor-ingress",
        )
        assert ingress["spec"]["ingress"] == [
            {
                "from": [{"podSelector": {"matchLabels": {"app": "manifest-processor"}}}],
                "ports": [{"protocol": "TCP", "port": 8443}],
            }
        ]
        egress = _find(
            "34-cost-monitor.yaml",
            "NetworkPolicy",
            "allow-manifest-processor-to-cost-monitor-egress",
        )
        assert egress["spec"]["egress"] == [
            {
                "to": [{"podSelector": {"matchLabels": {"app": "cost-monitor"}}}],
                "ports": [{"protocol": "TCP", "port": 8443}],
            }
        ]

    def test_the_opencost_rule_selects_exactly_the_tls_front_door(self) -> None:
        """Peer labels == the opencost-tls Service selector, port == its port.

        The VPC CNI admits a Service ClusterIP for an egress rule only when the
        Service selector matches the rule's podSelector, and evaluates the
        rule before kube-proxy's DNAT.
        """
        rule = _find("34-cost-monitor.yaml", "NetworkPolicy", "allow-cost-monitor-to-opencost")
        (egress,) = rule["spec"]["egress"]
        (peer,) = egress["to"]
        service = _find(COST_TLS, "Service", "opencost-tls")
        assert peer["namespaceSelector"] == {
            "matchLabels": {"kubernetes.io/metadata.name": "monitoring"}
        }
        assert peer["podSelector"]["matchLabels"] == service["spec"]["selector"]
        assert egress["ports"] == [{"protocol": "TCP", "port": service["spec"]["ports"][0]["port"]}]


# ─── The inference monitor pod ─────────────────────────────────────


class TestInferenceMonitorPod:
    def test_metrics_are_served_only_through_the_sidecar(self) -> None:
        deployment = _deployment("32-inference-monitor.yaml")
        pod = _containers(deployment)
        monitor_env, sidecar_env = _env(pod["inference-monitor"]), _env(pod["metrics-tls-proxy"])
        assert monitor_env["METRICS_HOST"] == "127.0.0.1"
        assert sidecar_env["TLS_PROXY_UPSTREAM_PORT"] == monitor_env["METRICS_PORT"] == "9090"
        assert sidecar_env["TLS_PROXY_PORT"] == "9443"
        assert pod["metrics-tls-proxy"]["ports"] == [
            {"name": "https-metrics", "containerPort": 9443, "protocol": "TCP"}
        ]
        assert "AWS_ROLE_ARN" not in sidecar_env
        assert "aws-iam-token" not in {
            mount["name"] for mount in pod["metrics-tls-proxy"]["volumeMounts"]
        }


# ─── Tracing switches ──────────────────────────────────────────────


class TestTracingEnvironment:
    def test_env_names_match_the_tracing_module(self) -> None:
        from gco.services import tracing

        assert tracing.TRACING_ENABLED_ENV == "GCO_TRACING_ENABLED"
        assert tracing.TRACING_SAMPLE_RATIO_ENV == "GCO_TRACING_SAMPLE_RATIO"

    @pytest.mark.parametrize(("filename", "container_name"), sorted(TRACED_CONTAINERS.items()))
    def test_traced_application_containers_carry_the_switches(
        self, filename: str, container_name: str
    ) -> None:
        raw = _raw(filename)
        # Quoted, so YAML keeps them strings, and exactly these two tokens:
        # the regional stack always substitutes both.
        assert raw.count('value: "{{TRACING_ENABLED}}"') == 1
        assert raw.count('value: "{{TRACING_SAMPLE_RATIO}}"') == 1
        env = _env(_containers(_deployment(filename))[container_name])
        assert env["GCO_TRACING_ENABLED"] == "placeholder"
        assert env["GCO_TRACING_SAMPLE_RATIO"] == "placeholder"
        assert env["POD_NAME"] == {"fieldRef": {"fieldPath": "metadata.name"}}
        assert env["POD_NAMESPACE"] == {"fieldRef": {"fieldPath": "metadata.namespace"}}
        # The resource attributes the tracing module reads.
        assert env["REGION"] == "placeholder"
        assert env["CLUSTER_NAME"] == "placeholder"

    def test_no_other_container_carries_the_switches(self) -> None:
        carriers = set()
        for filename, _kind, _workload, spec in _pod_specs():
            for container in spec["containers"]:
                names = {item["name"] for item in container.get("env", [])}
                if names & {"GCO_TRACING_ENABLED", "GCO_TRACING_SAMPLE_RATIO"}:
                    carriers.add((filename, container["name"]))
        assert carriers == set(TRACED_CONTAINERS.items())

    def test_no_other_manifest_uses_the_tracing_tokens(self) -> None:
        users = {
            path.name
            for path in MANIFESTS_DIR.glob("*.yaml")
            if "{{TRACING_" in path.read_text(encoding="utf-8")
        }
        assert users == set(TRACED_CONTAINERS)
        tokens = set()
        for filename in TRACED_CONTAINERS:
            tokens |= set(re.findall(r"\{\{TRACING_[A-Z0-9_]+\}\}", _raw(filename)))
        assert tokens == {"{{TRACING_ENABLED}}", "{{TRACING_SAMPLE_RATIO}}"}


# ─── The issuance fence ────────────────────────────────────────────
#
# No CEL evaluator ships with the repository, so the policy's own expressions
# run through the interpreter below. It knows exactly the CEL the policy uses
# (string, int, bool and null literals, lists, maps, field and index
# selection, has(), all(), !, &&, ||, ==, !=, in, + and ?:), with CEL's
# semantics where they decide an admission: selecting an absent field or key
# is an error, && and || absorb an error when the other side settles the
# result, and an error would deny the request (failurePolicy: Fail), so any
# error fails the test. Anything else is a parse error, so a construct the
# interpreter does not know fails these tests instead of passing unevaluated.
# integration:kind:examples-smoke runs the same policy in a real API server
# against the pinned cert-manager (TestIssuanceFenceInKind pins its probes).

POLICY = "gco-internal-ca-issuance"
CERT_MANAGER_USER = "system:serviceaccount:cert-manager:cert-manager"
APPLIER_USER = "kubernetes-admin"
GCO_NAME = "cost-monitor.gco-system.svc.cluster.local"
BOOTSTRAP_REF = {"name": "gco-internal-ca-bootstrap", "kind": "ClusterIssuer"}
SIGNS_ONLY_THE_LEAVES = "the ClusterIssuer gco-internal-ca signs only the GCO platform leaves"
ONLY_ITS_OWN_NAMES = "may carry only its own DNS names to use the ClusterIssuer gco-internal-ca"
ONLY_FROM_CERT_MANAGER = "accepts requests only from cert-manager"
NEVER_A_CA = "the ClusterIssuer gco-internal-ca never signs a CA"

CelNode = tuple[Any, ...]


class CelError(Exception):
    """A CEL evaluation error; under failurePolicy Fail it denies the request."""


_CEL_TOKEN = re.compile(
    r"""\s*(?:
        (?P<string>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
      | (?P<int>\d+)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<op>&&|\|\||==|!=|[!?:.,()\[\]{}+])
    )""",
    re.VERBOSE,
)
_CEL_ESCAPES = {"\\": "\\", "'": "'", '"': '"'}
_CEL_LITERALS = {"true": True, "false": False, "null": None}


def _cel_unquote(literal: str) -> str:
    def escape(match: re.Match[str]) -> str:
        if match.group(1) not in _CEL_ESCAPES:
            raise ValueError(f"unsupported CEL escape {match.group(0)!r}")
        return _CEL_ESCAPES[match.group(1)]

    return re.sub(r"\\(.)", escape, literal[1:-1])


class _CelParser:
    """Recursive descent over CEL's precedence levels, loosest first."""

    def __init__(self, source: str) -> None:
        self._tokens: list[tuple[str, str]] = []
        position = 0
        while source[position:].strip():
            match = _CEL_TOKEN.match(source, position)
            if match is None or match.lastgroup is None:
                raise ValueError(f"unsupported CEL at {source[position : position + 30]!r}")
            self._tokens.append((match.lastgroup, match.group(match.lastgroup)))
            position = match.end()
        self._position = 0

    def parse(self) -> CelNode:
        node = self._conditional()
        if self._position != len(self._tokens):
            raise ValueError(f"unexpected CEL token {self._tokens[self._position][1]!r}")
        return node

    def _peek(self) -> str | None:
        return self._tokens[self._position][1] if self._position < len(self._tokens) else None

    def _accept(self, text: str) -> bool:
        if self._peek() != text:
            return False
        self._position += 1
        return True

    def _expect(self, text: str) -> None:
        if not self._accept(text):
            raise ValueError(f"expected {text!r} in CEL, found {self._peek()!r}")

    def _next(self) -> tuple[str, str]:
        if self._position == len(self._tokens):
            raise ValueError("unexpected end of CEL")
        self._position += 1
        return self._tokens[self._position - 1]

    def _identifier(self) -> str:
        kind, text = self._next()
        if kind != "ident":
            raise ValueError(f"expected an identifier in CEL, found {text!r}")
        return text

    def _conditional(self) -> CelNode:
        condition = self._or()
        if not self._accept("?"):
            return condition
        then = self._or()
        self._expect(":")
        return ("?:", condition, then, self._conditional())

    def _or(self) -> CelNode:
        node = self._and()
        while self._accept("||"):
            node = ("||", node, self._and())
        return node

    def _and(self) -> CelNode:
        node = self._relation()
        while self._accept("&&"):
            node = ("&&", node, self._relation())
        return node

    def _relation(self) -> CelNode:
        node = self._addition()
        while (operator := self._peek()) in ("==", "!=", "in"):
            self._position += 1
            node = (operator, node, self._addition())
        return node

    def _addition(self) -> CelNode:
        node = self._unary()
        while self._accept("+"):
            node = ("+", node, self._unary())
        return node

    def _unary(self) -> CelNode:
        if self._accept("!"):
            return ("!", self._unary())
        return self._member()

    def _member(self) -> CelNode:
        node = self._primary()
        while True:
            if self._accept("."):
                field = self._identifier()
                if not self._accept("("):
                    node = ("select", node, field)
                    continue
                if field != "all":
                    raise ValueError(f"unsupported CEL macro .{field}()")
                variable = self._identifier()
                self._expect(",")
                predicate = self._conditional()
                self._expect(")")
                node = ("all", node, variable, predicate)
            elif self._accept("["):
                node = ("index", node, self._conditional())
                self._expect("]")
            else:
                return node

    def _primary(self) -> CelNode:
        kind, text = self._next()
        if kind == "string":
            return ("literal", _cel_unquote(text))
        if kind == "int":
            return ("literal", int(text))
        if kind == "ident" and text in _CEL_LITERALS:
            return ("literal", _CEL_LITERALS[text])
        if kind == "ident" and self._accept("("):
            argument = self._conditional()
            self._expect(")")
            if text != "has" or argument[0] != "select":
                raise ValueError(f"unsupported CEL call {text}()")
            return ("has", argument[1], argument[2])
        if kind == "ident":
            return ("ident", text)
        if text == "(":
            node = self._conditional()
            self._expect(")")
            return node
        if text == "[":
            items: list[CelNode] = []
            while not self._accept("]"):
                if items:
                    self._expect(",")
                items.append(self._conditional())
            return ("list", tuple(items))
        if text == "{":
            entries: list[tuple[CelNode, CelNode]] = []
            while not self._accept("}"):
                if entries:
                    self._expect(",")
                key = self._conditional()
                self._expect(":")
                entries.append((key, self._conditional()))
            return ("map", tuple(entries))
        raise ValueError(f"unsupported CEL token {text!r}")


class _CelVariables:
    """The policy's variables, each evaluated on first use and then reused."""

    def __init__(self, definitions: list[dict[str, str]], activation: dict[str, Any]) -> None:
        self._expressions = {
            item["name"]: _CelParser(item["expression"]).parse() for item in definitions
        }
        self._activation = activation
        self._results: dict[str, Any] = {}

    def get(self, name: str) -> Any:
        if name not in self._expressions:
            raise CelError(f"undefined variable {name!r}")
        if name not in self._results:
            try:
                self._results[name] = _cel_eval(self._expressions[name], self._activation)
            except CelError as error:
                self._results[name] = error
        result = self._results[name]
        if isinstance(result, CelError):
            raise result
        return result


def _cel_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise CelError(f"no such overload: expected a bool, got {value!r}")
    return value


def _cel_equal(left: Any, right: Any) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(_cel_equal, left, right, strict=True))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_cel_equal(left[k], right[k]) for k in left)
    # Values of different types are unequal: in CEL a bool is not an int.
    return type(left) is type(right) and left == right


def _cel_eval(node: CelNode, activation: dict[str, Any]) -> Any:
    operator = node[0]
    if operator == "literal":
        return node[1]
    if operator == "ident":
        if node[1] not in activation:
            raise CelError(f"undeclared reference to {node[1]!r}")
        return activation[node[1]]
    if operator in ("select", "has"):
        target = _cel_eval(node[1], activation)
        if operator == "select" and isinstance(target, _CelVariables):
            return target.get(node[2])
        if not isinstance(target, dict):
            raise CelError(f"{operator} {node[2]!r} on {target!r}")
        if operator == "has":
            return node[2] in target
        if node[2] not in target:
            raise CelError(f"no such key: {node[2]}")
        return target[node[2]]
    if operator == "index":
        target, key = _cel_eval(node[1], activation), _cel_eval(node[2], activation)
        if not isinstance(target, dict) or not isinstance(key, str) or key not in target:
            raise CelError(f"no such key: {key!r}")
        return target[key]
    if operator == "list":
        return [_cel_eval(item, activation) for item in node[1]]
    if operator == "map":
        return {_cel_eval(key, activation): _cel_eval(value, activation) for key, value in node[1]}
    if operator == "!":
        return not _cel_bool(_cel_eval(node[1], activation))
    if operator in ("&&", "||"):
        # Commutative: a side that settles the result wins over an error.
        decisive = operator == "||"
        logic_error: CelError | None = None
        for side in node[1:]:
            try:
                if _cel_bool(_cel_eval(side, activation)) is decisive:
                    return decisive
            except CelError as error:
                logic_error = error
        if logic_error is not None:
            raise logic_error
        return not decisive
    if operator == "?:":
        branch = node[2] if _cel_bool(_cel_eval(node[1], activation)) else node[3]
        return _cel_eval(branch, activation)
    if operator == "all":
        target = _cel_eval(node[1], activation)
        if not isinstance(target, (list, dict)):
            raise CelError(f"all() over {target!r}")
        item_error: CelError | None = None
        for item in target:
            try:
                if not _cel_bool(_cel_eval(node[3], {**activation, node[2]: item})):
                    return False
            except CelError as error:
                item_error = error
        if item_error is not None:
            raise item_error
        return True
    left, right = _cel_eval(node[1], activation), _cel_eval(node[2], activation)
    if operator == "==":
        return _cel_equal(left, right)
    if operator == "!=":
        return not _cel_equal(left, right)
    if operator == "in":
        if not isinstance(right, (list, dict)):
            raise CelError(f"no such overload: in {right!r}")
        return any(_cel_equal(left, item) for item in right)
    if type(left) is type(right) and isinstance(left, (str, list)):
        return left + right
    raise CelError(f"no such overload: {left!r} {operator} {right!r}")


def _variable_references(source: str) -> list[str]:
    """Every ``variables.<name>`` an expression selects."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, tuple):
            if node[:2] == ("select", ("ident", "variables")):
                found.append(node[2])
            for child in node[1:]:
                walk(child)

    walk(_CelParser(source).parse())
    return found


def _fence_policy() -> dict[str, Any]:
    return _find(FENCE, "ValidatingAdmissionPolicy", POLICY)


def _activation(
    obj: dict[str, Any],
    *,
    user: str = APPLIER_USER,
    operation: str = "CREATE",
    subresource: str = "",
    old: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The variables the API server binds for one admission request."""
    metadata = obj.get("metadata", {})
    resource = {"Certificate": "certificates", "CertificateRequest": "certificaterequests"}
    activation: dict[str, Any] = {
        "object": obj,
        "oldObject": old,
        "request": {
            "operation": operation,
            "resource": {
                "group": "cert-manager.io",
                "version": "v1",
                "resource": resource[obj["kind"]],
            },
            "subResource": subresource,
            "namespace": metadata.get("namespace", ""),
            "name": metadata.get("name", ""),
            "userInfo": {"username": user, "groups": ["system:authenticated"]},
        },
    }
    activation["variables"] = _CelVariables(_fence_policy()["spec"]["variables"], activation)
    return activation


def _variable(obj: dict[str, Any], name: str, **request: Any) -> Any:
    return _activation(obj, **request)["variables"].get(name)


def _review(obj: dict[str, Any], **request: Any) -> list[str] | None:
    """Run the policy as the API server does for one request.

    None when its matchConstraints do not select the request; otherwise the
    messages of the validations that failed, in order ([] admits it; the API
    server returns the first).
    """
    activation = _activation(obj, **request)
    admission = activation["request"]
    requested = admission["resource"]["resource"]
    if admission["subResource"]:
        requested += "/" + admission["subResource"]
    if not any(
        {"cert-manager.io", "*"} & set(rule["apiGroups"])
        and {"v1", "*"} & set(rule["apiVersions"])
        and {admission["operation"], "*"} & set(rule["operations"])
        and requested in rule["resources"]
        for rule in _fence_policy()["spec"]["matchConstraints"]["resourceRules"]
    ):
        return None
    failures = []
    for validation in _fence_policy()["spec"]["validations"]:
        try:
            if _cel_bool(_cel_eval(_CelParser(validation["expression"]).parse(), activation)):
                continue
            message = _cel_eval(_CelParser(validation["messageExpression"]).parse(), activation)
        except CelError as error:
            pytest.fail(f"CEL error in {validation['expression']!r}: {error}")
        assert isinstance(message, str) and message.strip() and "\n" not in message
        failures.append(message)
    return failures


def _certificate(
    namespace: str,
    name: str,
    issuer_ref: dict[str, str],
    *dns_names: str,
    **spec: Any,
) -> dict[str, Any]:
    return {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "secretName": name,
            "dnsNames": list(dns_names or (GCO_NAME,)),
            "issuerRef": dict(issuer_ref),
            **spec,
        },
    }


def _request_for(certificate: dict[str, Any]) -> dict[str, Any]:
    """The CertificateRequest cert-manager's request manager files for a Certificate.

    As cert-manager v1.21 builds it: the Certificate's namespace, issuerRef,
    usages and (only when true) isCA, and the cert-manager.io/certificate-name
    annotation naming the Certificate.
    """
    spec = certificate["spec"]
    name = certificate["metadata"]["name"]
    request_spec: dict[str, Any] = {"request": "LS0tLS1CRUdJTg==", "issuerRef": spec["issuerRef"]}
    if "usages" in spec:
        request_spec["usages"] = spec["usages"]
    if spec.get("isCA"):
        request_spec["isCA"] = True
    return {
        "apiVersion": "cert-manager.io/v1",
        "kind": "CertificateRequest",
        "metadata": {
            "name": f"{name}-1",
            "namespace": certificate["metadata"]["namespace"],
            "annotations": {
                "cert-manager.io/certificate-name": name,
                "cert-manager.io/certificate-revision": "1",
                "cert-manager.io/private-key-secret-name": f"{name}-x7k2p",
            },
        },
        "spec": request_spec,
    }


def _shipped_certificates() -> list[dict[str, Any]]:
    return [doc for _file, doc in _all_documents() if doc["kind"] == "Certificate"]


def _key(obj: dict[str, Any]) -> str:
    return f"{obj['metadata']['namespace']}/{obj['metadata']['name']}"


class TestCelInterpreter:
    """The interpreter behaves like CEL for every construct the policy relies on."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("has(o.a)", True),
            ("has(o.missing)", False),
            ("true || o.missing", True),
            ("o.missing || true", True),
            ("false && o.missing", False),
            ("o.missing && false", False),
            ("o.a == 1 ? 'one' : o.missing", "one"),
            ("'x' in ['y', 'x']", True),
            ("'b' in {'a': 1, 'b': 2}", True),
            ("o.m['k'] + '/' + o.m['k']", "v/v"),
            ("['a'] + ['b'] == ['a', 'b']", True),
            ("1 == true", False),
            ("[].all(n, n == 1)", True),
            ("['a', 'b'].all(n, n in ['a', 'b', 'c'])", True),
            ("['a', 'z'].all(n, n in ['a', 'b'])", False),
            # A false element settles all() over another element's error.
            ("[1, 'z'].all(n, n + 'x' == 'ax')", False),
            ("!(o.a != 1)", True),
            ("\"double\" == 'double'", True),
            (r"'it\'s' == " + '"it\'s"', True),
        ],
    )
    def test_values(self, source: str, expected: Any) -> None:
        activation = {"o": {"a": 1, "m": {"k": "v"}}}
        assert _cel_eval(_CelParser(source).parse(), activation) == expected

    @pytest.mark.parametrize(
        "source",
        [
            "o.missing",
            "has(o.missing.deeper)",
            "o.missing || false",
            "true && o.missing",
            "o.m['absent']",
            "!o.a",
            "o.a ? 1 : 2",
            "1 + 'a'",
            "'a' in 'abc'",
            "[o.missing].all(n, n == 'a')",
            "['a', 1].all(n, n + 'x' == 'ax')",
            "unknown",
        ],
    )
    def test_errors(self, source: str) -> None:
        with pytest.raises(CelError):
            _cel_eval(_CelParser(source).parse(), {"o": {"a": 1, "m": {"k": "v"}}})

    @pytest.mark.parametrize(
        "source",
        [
            "size(o)",
            "o.exists(n, n)",
            "o.?a",
            "o.a < 2",
            "has(o)",
            "[1, 2",
            "o.a o.b",
            "'\\n'",
        ],
    )
    def test_constructs_it_does_not_model_are_parse_errors(self, source: str) -> None:
        with pytest.raises(ValueError, match=r"CEL"):
            _CelParser(source).parse()


class TestIssuanceFencePolicy:
    """The admission objects, and the shape of the CEL they carry."""

    def test_the_fence_is_an_ungated_base_pass_file_of_its_own(self) -> None:
        assert [(doc["kind"], doc["metadata"]["name"]) for doc in _documents(FENCE)] == [
            ("ValidatingAdmissionPolicy", POLICY),
            ("ValidatingAdmissionPolicyBinding", POLICY),
        ]
        for doc in _documents(FENCE):
            assert doc["apiVersion"] == "admissionregistration.k8s.io/v1"
            assert "namespace" not in doc["metadata"]
        assert not re.search(r"\{\{[A-Za-z0-9_]+\}\}", _raw(FENCE))
        # The base pass runs before Helm installs cert-manager, so the fence is
        # enforced before any Certificate can exist and minutes before the
        # post-Helm pass creates the CA; it also sorts before the kro grant.
        assert not FENCE.startswith("post-helm-") and CORE_PKI.startswith("post-helm-")
        assert FENCE < "07-kro-tenant-access.yaml"

    def test_the_policy_fails_closed_and_its_binding_denies_everywhere(self) -> None:
        spec = _fence_policy()["spec"]
        assert spec["failurePolicy"] == "Fail"
        assert "paramKind" not in spec and "matchConditions" not in spec
        binding = _find(FENCE, "ValidatingAdmissionPolicyBinding", POLICY)
        assert binding["spec"] == {"policyName": POLICY, "validationActions": ["Deny"]}

    def test_it_matches_certificate_writes_and_request_filing_and_completion(self) -> None:
        def rule(operations: list[str], resource: str) -> dict[str, list[str]]:
            return {
                "apiGroups": ["cert-manager.io"],
                "apiVersions": ["*"],
                "operations": operations,
                "resources": [resource],
            }

        assert _fence_policy()["spec"]["matchConstraints"] == {
            "resourceRules": [
                rule(["CREATE", "UPDATE"], "certificates"),
                rule(["CREATE"], "certificaterequests"),
                rule(["UPDATE"], "certificaterequests/status"),
            ]
        }

    @pytest.mark.parametrize(
        ("kind", "operation", "subresource"),
        [
            ("Certificate", "DELETE", ""),
            ("Certificate", "UPDATE", "status"),
            ("CertificateRequest", "UPDATE", ""),
            ("CertificateRequest", "DELETE", ""),
        ],
    )
    def test_other_requests_are_not_evaluated(
        self, kind: str, operation: str, subresource: str
    ) -> None:
        certificate = _certificate("gco-jobs", "tenant", CA_ISSUER_REF)
        obj = certificate if kind == "Certificate" else _request_for(certificate)
        assert _review(obj, operation=operation, subresource=subresource) is None

    def test_every_rule_is_forbidden_with_a_one_line_message(self) -> None:
        validations = _fence_policy()["spec"]["validations"]
        assert len(validations) == 4
        for validation in validations:
            assert validation["reason"] == "Forbidden"
            assert validation["message"].strip() and "\n" not in validation["message"]
            assert validation["messageExpression"].strip()

    def test_expressions_parse_and_use_only_variables_defined_before_them(self) -> None:
        spec = _fence_policy()["spec"]
        defined: list[str] = []
        for variable in spec["variables"]:
            assert set(_variable_references(variable["expression"])) <= set(defined), variable
            defined.append(variable["name"])
        assert len(defined) == len(set(defined))
        for validation in spec["validations"]:
            for field in ("expression", "messageExpression"):
                assert set(_variable_references(validation[field])) <= set(defined), validation

    def test_the_allowlist_is_exactly_the_shipped_leaves_and_their_names(self) -> None:
        """Every Certificate that names gco-internal-ca, parsed from every manifest."""
        shipped = {
            _key(doc): doc["spec"]["dnsNames"]
            for doc in _shipped_certificates()
            if _variable(doc, "gcoIssuer") == "gco-internal-ca"
        }
        allowlist = _variable(_shipped_certificates()[0], "internalLeaves")
        assert allowlist == shipped
        assert allowlist == {f"{leaf[1]}/{name}": leaf[2] for name, leaf in LEAVES.items()}

    def test_cert_manager_is_the_charts_default_service_account(self) -> None:
        """The installer names the release after the charts.yaml key, and the chart
        runs its controller as a ServiceAccount named after the release."""
        release = "cert-manager"
        chart = yaml.safe_load(CHARTS_FILE.read_text(encoding="utf-8"))["charts"][release]
        values = chart.get("values", {})
        # cert-manager.fullname is the release name when it contains the chart
        # name, and the controller's ServiceAccount defaults to the fullname.
        assert chart["chart"] in release
        assert not {"fullnameOverride", "nameOverride"} & set(values)
        assert values.get("serviceAccount", {}).get("create", True) is True
        assert "name" not in values.get("serviceAccount", {})
        derived = f"system:serviceaccount:{chart['namespace']}:{release}"
        assert derived == CERT_MANAGER_USER
        request = _request_for(_find(CORE_PKI, "Certificate", "gco-internal-ca"))
        assert _variable(request, "fromCertManager", user=CERT_MANAGER_USER) is True
        for user in (
            f"{CERT_MANAGER_USER}-webhook",
            f"{CERT_MANAGER_USER}-cainjector",
            "system:serviceaccount:kube-system:cert-manager",
            "cert-manager",
        ):
            assert _variable(request, "fromCertManager", user=user) is False


def _shipped_certificate_id(certificate: dict[str, Any]) -> str:
    return _key(certificate)


class TestIssuanceFenceRules:
    """The shipped policy, evaluated: what it admits and what it refuses."""

    def test_the_certificates_that_name_a_gco_issuer_are_the_chain(self) -> None:
        named = {
            (_variable(doc, "gcoIssuer"), _key(doc))
            for doc in _shipped_certificates()
            if _variable(doc, "gcoIssuer")
        }
        assert named == {("gco-internal-ca-bootstrap", "cert-manager/gco-internal-ca")} | {
            ("gco-internal-ca", f"{leaf[1]}/{name}") for name, leaf in LEAVES.items()
        }

    @pytest.mark.parametrize("certificate", _shipped_certificates(), ids=_shipped_certificate_id)
    def test_every_shipped_certificate_and_its_requests_are_admitted(
        self, certificate: dict[str, Any]
    ) -> None:
        """A new Certificate for either GCO issuer fails here until it is listed."""
        assert _review(certificate) == []
        assert _review(certificate, operation="UPDATE", old=certificate) == []
        request = _request_for(certificate)
        assert _review(request, user=CERT_MANAGER_USER) == []
        assert (
            _review(
                request,
                user=CERT_MANAGER_USER,
                operation="UPDATE",
                subresource="status",
                old=request,
            )
            == []
        )

    @pytest.mark.parametrize(
        ("certificate", "message"),
        [
            (
                _certificate("gco-jobs", "cost-monitor-tls", CA_ISSUER_REF),
                f"Certificate gco-jobs/cost-monitor-tls: {SIGNS_ONLY_THE_LEAVES}; "
                "use an Issuer in your own namespace",
            ),
            (
                _certificate(
                    "gco-jobs", "no-group", {"name": "gco-internal-ca", "kind": "ClusterIssuer"}
                ),
                f"Certificate gco-jobs/no-group: {SIGNS_ONLY_THE_LEAVES}",
            ),
            (
                _certificate(
                    "gco-jobs",
                    "empty-group",
                    {"name": "gco-internal-ca", "kind": "ClusterIssuer", "group": ""},
                ),
                f"Certificate gco-jobs/empty-group: {SIGNS_ONLY_THE_LEAVES}",
            ),
            (
                _certificate(
                    "gco-inference", "tenant-endpoint-tls", CA_ISSUER_REF, "*.gco-inference.svc"
                ),
                f"Certificate gco-inference/tenant-endpoint-tls: {SIGNS_ONLY_THE_LEAVES}",
            ),
            (
                _certificate(
                    "gco-system",
                    "gco-system-wildcard",
                    CA_ISSUER_REF,
                    "*.gco-system.svc",
                    "*.gco-system.svc.cluster.local",
                ),
                f"Certificate gco-system/gco-system-wildcard: {SIGNS_ONLY_THE_LEAVES}",
            ),
            (
                _certificate("cert-manager", "gco-internal-ca", CA_ISSUER_REF, isCA=True),
                f"Certificate cert-manager/gco-internal-ca: {SIGNS_ONLY_THE_LEAVES}",
            ),
            (
                _certificate("gco-jobs", "gco-internal-ca", BOOTSTRAP_REF),
                "Certificate gco-jobs/gco-internal-ca: the ClusterIssuer "
                "gco-internal-ca-bootstrap signs only cert-manager/gco-internal-ca",
            ),
            (
                _certificate("gco-system", "health-monitor-tls", BOOTSTRAP_REF),
                "Certificate gco-system/health-monitor-tls: the ClusterIssuer "
                "gco-internal-ca-bootstrap signs only cert-manager/gco-internal-ca",
            ),
        ],
        ids=[
            "gco-jobs-leaf-for-a-gco-name",
            "issuerRef-without-group",
            "issuerRef-with-empty-group",
            "gco-inference-other-name",
            "gco-system-wildcard",
            "the-ca-from-the-ca",
            "bootstrap-from-a-tenant",
            "bootstrap-for-a-listed-leaf",
        ],
    )
    def test_certificates_off_the_allowlist_are_refused(
        self, certificate: dict[str, Any], message: str
    ) -> None:
        for request in ({}, {"user": CERT_MANAGER_USER}, {"operation": "UPDATE"}):
            failures = _review(certificate, **request)
            assert failures, request
            assert failures[0].startswith(message), failures

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"dnsNames": ["*.gco-inference.svc", GCO_NAME]}, ONLY_ITS_OWN_NAMES),
            ({"dnsNames": ["*.gco-system.svc"]}, ONLY_ITS_OWN_NAMES),
            ({"commonName": GCO_NAME}, ONLY_ITS_OWN_NAMES),
            ({"literalSubject": f"CN={GCO_NAME}"}, ONLY_ITS_OWN_NAMES),
            ({"subject": {"organizations": ["system:masters"]}}, ONLY_ITS_OWN_NAMES),
            ({"ipAddresses": ["10.0.0.1"]}, ONLY_ITS_OWN_NAMES),
            ({"uris": ["spiffe://cluster.local/ns/gco-system/sa/x"]}, ONLY_ITS_OWN_NAMES),
            ({"emailAddresses": ["tenant@example.com"]}, ONLY_ITS_OWN_NAMES),
            (
                {"otherNames": [{"oid": "1.3.6.1.4.1.311.20.2.3", "utf8Value": "x"}]},
                ONLY_ITS_OWN_NAMES,
            ),
            ({"isCA": True, "usages": ["cert sign", "digital signature"]}, NEVER_A_CA),
        ],
        ids=[
            "add-a-gco-system-name",
            "swap-in-another-wildcard",
            "commonName",
            "literalSubject",
            "subject",
            "ipAddresses",
            "uris",
            "emailAddresses",
            "otherNames",
            "isCA",
        ],
    )
    def test_a_listed_leaf_in_a_tenant_namespace_keeps_its_identity(
        self, change: dict[str, Any], message: str
    ) -> None:
        """gco-inference-tls lives where tenants may patch Certificates."""
        shipped = _find(CORE_PKI, "Certificate", "gco-inference-tls")
        changed = copy.deepcopy(shipped)
        changed["spec"].update(change)
        failures = _review(changed, operation="UPDATE", old=shipped)
        assert failures and message in failures[0], failures
        # CEL cannot read the CSR inside a request, so names are fenced on the
        # Certificate (above); the request rule still refuses a CA.
        assert _review(_request_for(changed), user=CERT_MANAGER_USER) == (
            [f"CertificateRequest gco-inference/gco-inference-tls-1: {NEVER_A_CA}"]
            if change.get("isCA")
            else []
        )

    @pytest.mark.parametrize(
        "change",
        [
            {"metadata": {"labels": {"tenant": "team-a"}}},
            {"spec": {"dnsNames": ["*.gco-inference.svc.cluster.local"]}},
            {"spec": {"issuerRef": {"name": "tenant-ca", "kind": "Issuer"}}},
        ],
        ids=["a-label", "one-of-its-own-names", "leaving-the-gco-ca"],
    )
    def test_changes_that_keep_a_leafs_identity_are_admitted(self, change: dict[str, Any]) -> None:
        shipped = _find(CORE_PKI, "Certificate", "gco-inference-tls")
        changed = copy.deepcopy(shipped)
        for section, fields in change.items():
            changed[section].update(fields)
        assert _review(changed, operation="UPDATE", old=shipped) == []

    @pytest.mark.parametrize(
        "issuer_ref",
        [
            {"name": "gco-internal-ca"},
            {"name": "gco-internal-ca", "kind": ""},
            {"name": "gco-internal-ca", "kind": "Issuer"},
            {"name": "tenant-ca", "kind": "Issuer", "group": "cert-manager.io"},
            {"name": "gco-internal-ca", "kind": "ClusterIssuer", "group": "awspca.cert-manager.io"},
            {"name": "letsencrypt", "kind": "ClusterIssuer"},
        ],
        ids=[
            "own-issuer-named-like-the-ca",
            "empty-kind-is-an-issuer",
            "kind-issuer",
            "explicit-group",
            "another-api-group",
            "another-cluster-issuer",
        ],
    )
    def test_a_tenants_own_issuer_is_untouched(self, issuer_ref: dict[str, str]) -> None:
        certificate = _certificate("gco-jobs", "tenant-tls", issuer_ref, GCO_NAME, isCA=True)
        assert _variable(certificate, "gcoIssuer") == ""
        assert _review(certificate) == []
        assert _review(certificate, operation="UPDATE", old=certificate) == []
        request = _request_for(certificate)
        assert _review(request, user="system:serviceaccount:gco-jobs:default") == []

    @pytest.mark.parametrize(
        "user",
        [
            APPLIER_USER,
            "system:serviceaccount:gco-jobs:default",
            "system:serviceaccount:argocd:argocd-application-controller",
            "system:serviceaccount:crossplane-system:crossplane",
            "arn:aws:sts::123456789012:assumed-role/example-kro-role/KRO",
            f"{CERT_MANAGER_USER}-webhook",
            "system:serviceaccount:kube-system:cert-manager",
        ],
    )
    @pytest.mark.parametrize(
        "certificate",
        [
            _find(CORE_PKI, "Certificate", "health-monitor-tls"),
            _find(CORE_PKI, "Certificate", "gco-inference-tls"),
            _find(CORE_PKI, "Certificate", "gco-internal-ca"),
        ],
        ids=_shipped_certificate_id,
    )
    def test_only_cert_manager_files_or_completes_requests_to_the_gco_issuers(
        self, certificate: dict[str, Any], user: str
    ) -> None:
        request = _request_for(certificate)
        issuer = certificate["spec"]["issuerRef"]["name"]
        expected = (
            f"CertificateRequest {_key(request)}: the ClusterIssuer {issuer} {ONLY_FROM_CERT_MANAGER} "
            f"({CERT_MANAGER_USER}), not {user}; create a Certificate instead"
        )
        assert _review(request, user=user) == [expected]
        status = {"operation": "UPDATE", "subresource": "status", "old": request}
        assert _review(request, user=user, **status) == [expected]

    def test_cert_manager_cannot_file_or_complete_a_request_for_an_unlisted_certificate(
        self,
    ) -> None:
        """A request filed before the fence existed is refused its signed certificate."""
        request = _request_for(_certificate("gco-jobs", "evil", CA_ISSUER_REF))
        expected = [
            "CertificateRequest gco-jobs/evil-1 (for Certificate gco-jobs/evil): "
            f"{SIGNS_ONLY_THE_LEAVES}; use an Issuer in your own namespace"
        ]
        assert _review(request, user=CERT_MANAGER_USER) == expected
        status = {"operation": "UPDATE", "subresource": "status", "old": request}
        assert _review(request, user=CERT_MANAGER_USER, **status) == expected

    def test_a_request_without_its_certificate_annotation_is_refused(self) -> None:
        request = _request_for(_find(CORE_PKI, "Certificate", "health-monitor-tls"))
        del request["metadata"]["annotations"]
        failures = _review(request, user=CERT_MANAGER_USER)
        assert failures == [
            "CertificateRequest gco-system/health-monitor-tls-1 (for Certificate gco-system/): "
            f"{SIGNS_ONLY_THE_LEAVES}; use an Issuer in your own namespace"
        ]

    def test_cert_manager_cannot_ask_the_internal_ca_for_a_ca(self) -> None:
        request = _request_for(_find(CORE_PKI, "Certificate", "health-monitor-tls"))
        request["spec"]["isCA"] = True
        assert _review(request, user=CERT_MANAGER_USER) == [
            f"CertificateRequest gco-system/health-monitor-tls-1: {NEVER_A_CA}"
        ]

    @pytest.mark.parametrize(
        ("obj", "admitted"),
        [
            ({"kind": "Certificate", "metadata": {"name": "bare", "namespace": "gco-jobs"}}, True),
            ({"kind": "Certificate", "metadata": {"namespace": "gco-jobs"}, "spec": {}}, True),
            (
                {
                    "kind": "Certificate",
                    "metadata": {"namespace": "gco-jobs"},
                    "spec": {"issuerRef": {}},
                },
                True,
            ),
            (
                {
                    "kind": "Certificate",
                    "metadata": {"namespace": "gco-system"},
                    "spec": {"issuerRef": CA_ISSUER_REF},
                },
                False,
            ),
            (
                {
                    "kind": "Certificate",
                    "metadata": {"name": "health-monitor-tls", "namespace": "gco-system"},
                    "spec": {"issuerRef": CA_ISSUER_REF},
                },
                True,
            ),
            (
                {
                    "kind": "CertificateRequest",
                    "metadata": {"namespace": "gco-jobs"},
                    "spec": {"issuerRef": CA_ISSUER_REF},
                },
                False,
            ),
            ({"kind": "CertificateRequest", "metadata": {"namespace": "gco-jobs"}}, True),
        ],
        ids=[
            "no-spec",
            "no-name-no-issuerRef",
            "empty-issuerRef",
            "no-name-for-the-ca",
            "listed-leaf-without-dnsNames",
            "request-without-name-or-annotations",
            "request-without-spec",
        ],
    )
    def test_absent_optional_fields_never_raise(self, obj: dict[str, Any], admitted: bool) -> None:
        """Every field is guarded: an absent one is a decision, never a CEL error.

        Each variable is evaluated on its own too, because && and || would
        absorb an unguarded variable's error into a correct-looking result.
        """
        for user in (APPLIER_USER, CERT_MANAGER_USER):
            failures = _review(obj, user=user)
            assert (failures == []) is admitted, failures
        for variable in _fence_policy()["spec"]["variables"]:
            try:
                _variable(obj, variable["name"])
            except CelError as error:
                pytest.fail(f"variables.{variable['name']} raised {error}")


class TestIssuanceFenceInKind:
    """integration:kind:examples-smoke's probes meet the rule they claim to meet."""

    @staticmethod
    def _step() -> str:
        workflow = yaml.safe_load(INTEGRATION_WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["integration-kind-examples-smoke"]["steps"]
        name = "Issue the shipped internal PKI with the pinned cert-manager"
        return str(next(step["run"] for step in steps if step.get("name") == name))

    @classmethod
    def _heredoc(cls, filename: str) -> list[dict[str, Any]]:
        pattern = rf"cat > \"\$\{{fence\}}/{re.escape(filename)}\" <<'?EOF'?\n(.*?)\nEOF\n"
        match = re.search(pattern, cls._step(), re.DOTALL)
        assert match, filename
        text = match.group(1).replace("${csr}", "LS0tLS1CRUdJTg==")
        return [doc for doc in yaml.safe_load_all(text) if doc]

    def test_the_tenant_leaf_is_refused_with_the_message_the_step_expects(self) -> None:
        run = self._step()
        (leaf,) = self._heredoc("tenant-leaf.yaml")
        denied = re.search(r'^denied="([^"]+)"$', run, re.MULTILINE)
        assert denied and denied.group(1) == SIGNS_ONLY_THE_LEAVES
        assert leaf["metadata"]["namespace"] == "gco-jobs"
        assert leaf["spec"]["issuerRef"] == CA_ISSUER_REF
        failures = _review(leaf)
        assert failures and denied.group(1) in failures[0]

    def test_widening_the_model_leaf_is_refused_with_the_message_the_step_expects(self) -> None:
        run = self._step()
        patch_ops = re.search(
            r"patch certificate gco-inference-tls .*?-p '(\[.*?\])'", run, re.DOTALL
        )
        assert patch_ops
        shipped = _find(CORE_PKI, "Certificate", "gco-inference-tls")
        widened = copy.deepcopy(shipped)
        for op in json.loads(patch_ops.group(1)):
            assert (op["op"], op["path"]) == ("add", "/spec/dnsNames/-")
            widened["spec"]["dnsNames"].append(op["value"])
        failures = _review(widened, operation="UPDATE", old=shipped)
        assert failures and "may carry only its own DNS names" in failures[0]
        assert 'grep -qF "may carry only its own DNS names"' in run

    def test_the_tenant_request_is_refused_with_the_message_the_step_expects(self) -> None:
        run = self._step()
        (request,) = self._heredoc("tenant-request.yaml")
        user = re.search(r"kubectl create --as=(\S+) --as-group=system:masters", run)
        assert user
        failures = _review(request, user=user.group(1))
        assert failures and ONLY_FROM_CERT_MANAGER in failures[0]
        assert f'grep -qF "{ONLY_FROM_CERT_MANAGER}"' in run

    def test_the_tenants_own_issuer_is_admitted(self) -> None:
        issuer, certificate = self._heredoc("tenant-own-issuer.yaml")
        assert issuer["kind"] == "Issuer" and issuer["spec"] == {"selfSigned": {}}
        assert issuer["metadata"] == {"name": "gco-internal-ca", "namespace": "gco-jobs"}
        assert certificate["spec"]["issuerRef"] == {"name": issuer["metadata"]["name"]}
        assert _review(certificate) == []


# ─── The applier ───────────────────────────────────────────────────


class TestApplier:
    def test_cert_manager_kinds_match_the_applier_map(self, handler_module) -> None:
        mapping = handler_module._CERT_MANAGER_CUSTOM_OBJECTS
        shipped = {
            doc["kind"]
            for _file, doc in _all_documents()
            if doc["apiVersion"].startswith("cert-manager.io/")
        }
        assert shipped == {"ClusterIssuer", "Certificate"}
        for kind in shipped:
            group, version, _plural, cluster_scoped = mapping[kind]
            assert f"{group}/{version}" == "cert-manager.io/v1"
            assert cluster_scoped is (kind in handler_module._CLUSTER_SCOPED_KINDS)
            assert kind in handler_module._SUPPORTED_MANIFEST_KINDS
        assert mapping["ClusterIssuer"] == ("cert-manager.io", "v1", "clusterissuers", True)

    def test_the_fence_plans_in_the_base_pass_ahead_of_the_ca(
        self, handler_module, tmp_path
    ) -> None:
        for name in (FENCE, CORE_PKI):
            (tmp_path / name).write_text(_raw(name), encoding="utf-8")
        plan = handler_module.plan_manifests(str(tmp_path), {})
        assert [
            (item["kind"], item["namespace"], item["name"]) for item in plan["phases"]["base"]
        ] == [
            ("ValidatingAdmissionPolicy", "<cluster>", POLICY),
            ("ValidatingAdmissionPolicyBinding", "<cluster>", POLICY),
        ]
        assert {item["sourceFile"] for item in plan["phases"]["post-helm"]} == {CORE_PKI}
        for kind in ("ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"):
            assert kind in handler_module._SUPPORTED_MANIFEST_KINDS
            assert kind in handler_module._CLUSTER_SCOPED_KINDS

    def test_the_core_pki_plans_cluster_scoped_issuers(self, handler_module, tmp_path) -> None:
        (tmp_path / CORE_PKI).write_text(_raw(CORE_PKI), encoding="utf-8")
        plan = handler_module.plan_manifests(str(tmp_path), {})
        planned = [
            (item["kind"], item["namespace"], item["name"]) for item in plan["phases"]["post-helm"]
        ]
        assert planned[:3] == [
            ("ClusterIssuer", "<cluster>", "gco-internal-ca-bootstrap"),
            ("Certificate", "cert-manager", "gco-internal-ca"),
            ("ClusterIssuer", "<cluster>", "gco-internal-ca"),
        ]
        assert plan["phases"]["base"] == []

    @pytest.mark.parametrize(
        ("filename", "gate"), [(COST_TLS, COST_GATE), (MONITORING_TLS, OBSERVABILITY_GATE)]
    )
    def test_gated_tls_objects_are_pruned_certificate_before_secret(
        self, handler_module, filename: str, gate: str
    ) -> None:
        inventory = list(handler_module._FEATURE_RESOURCE_INVENTORY[(gate, True)])
        tls_objects = [
            doc
            for doc in _documents(filename)
            if doc["apiVersion"].startswith("cert-manager.io/")
            or (doc["kind"] == "Service" and doc["metadata"]["name"].endswith("-tls"))
        ]
        assert tls_objects
        for doc in tls_objects:
            identity = (
                doc["apiVersion"],
                doc["kind"],
                doc["metadata"]["namespace"],
                doc["metadata"]["name"],
            )
            assert identity in inventory, identity
            if doc["kind"] == "Certificate":
                # cert-manager leaves the Secret behind; the prune removes it,
                # after the Certificate so nothing re-issues it.
                secret = ("v1", "Secret", doc["metadata"]["namespace"], doc["spec"]["secretName"])
                assert inventory.index(identity) < inventory.index(secret)

    def test_every_cost_tls_object_is_in_the_post_helm_cost_inventory(self, handler_module) -> None:
        inventory = set(handler_module._FEATURE_RESOURCE_INVENTORY[(COST_GATE, True)])
        for doc in _documents(COST_TLS):
            identity = (
                doc["apiVersion"],
                doc["kind"],
                doc["metadata"]["namespace"],
                doc["metadata"]["name"],
            )
            assert identity in inventory
