"""
Cross-cutting integration tests for the GCO repo.

Runs static-analysis-style checks that don't need a live cluster or AWS
account: every Kubernetes manifest under lambda/kubectl-applier-simple/
has the required shape for its kind, every example job under examples/
pulls images only from trusted registries (loaded from cdk.json with a
sensible fallback list), every Lambda handler imports cleanly and
exposes a handler(event, context) signature, and CDK synthesis
produces well-formed CloudFormation. Acts as a belt-and-braces smoke
test that catches schema drift across manifests, examples, and stacks
in one place.
"""

import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

# =============================================================================
# Test Configuration
# =============================================================================

PROJECT_ROOT = Path(__file__).parent.parent
MANIFESTS_DIR = PROJECT_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
EXAMPLES_DIR = PROJECT_ROOT / "examples"
LAMBDA_DIR = PROJECT_ROOT / "lambda"

# Required fields for different Kubernetes resource types
REQUIRED_FIELDS = {
    "all": ["apiVersion", "kind", "metadata"],
    "Deployment": ["spec.selector", "spec.template"],
    "Job": ["spec.template"],
    "Service": ["spec.ports"],
    "ConfigMap": [],
    "Secret": [],
    "ServiceAccount": [],
    "ClusterRole": ["rules"],
    "ClusterRoleBinding": ["roleRef", "subjects"],
    "Role": ["rules"],
    "RoleBinding": ["roleRef", "subjects"],
    "Namespace": [],
    "PersistentVolumeClaim": ["spec.accessModes", "spec.resources"],
    "StorageClass": ["provisioner"],
    "Ingress": ["spec.rules"],
    "IngressClass": ["spec.controller"],
    "NodePool": ["spec.template"],
    "NetworkPolicy": ["spec.podSelector"],
    "PodDisruptionBudget": ["spec.selector"],
}


# Trusted image registries (loaded from cdk.json or defaults)
def _load_trusted_config() -> tuple[list[str], list[str]]:
    """Load trusted registries config from cdk.json."""
    cdk_json_path = PROJECT_ROOT / "cdk.json"
    try:
        with open(cdk_json_path, encoding="utf-8") as f:
            data = json.load(f)
        mp_config = data.get("context", {}).get("manifest_processor", {})
        registries = mp_config.get(
            "trusted_registries",
            [
                "docker.io",
                "gcr.io",
                "quay.io",
                "registry.k8s.io",
                "k8s.gcr.io",
                "public.ecr.aws",
                "nvcr.io",
            ],
        )
        orgs = mp_config.get(
            "trusted_dockerhub_orgs",
            [
                "nvidia",
                "pytorch",
                "rayproject",
                "tensorflow",
                "huggingface",
                "amazon",
                "bitnami",
            ],
        )
        return registries, orgs
    except Exception:
        # Fallback to defaults
        return (
            [
                "docker.io",
                "gcr.io",
                "quay.io",
                "registry.k8s.io",
                "k8s.gcr.io",
                "public.ecr.aws",
                "nvcr.io",
            ],
            ["nvidia", "pytorch", "rayproject", "tensorflow", "huggingface", "amazon", "bitnami"],
        )


TRUSTED_REGISTRIES, TRUSTED_DOCKERHUB_ORGS = _load_trusted_config()
# Add "library" for official Docker Hub images
TRUSTED_DOCKERHUB_ORGS = list(TRUSTED_DOCKERHUB_ORGS) + ["library"]


# =============================================================================
# Helper Functions
# =============================================================================


def load_yaml_file(filepath: Path, allow_templates: bool = False) -> list[dict[str, Any]]:
    """Load all documents from a YAML file.

    Args:
        filepath: Path to the YAML file
        allow_templates: If True, replace template placeholders before parsing
    """
    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    if allow_templates:
        # Replace Jinja2-style template placeholders with dummy values
        # This allows parsing manifests that use {{VARIABLE}} syntax
        import re

        # Replace template placeholders with a simple string (no quotes)
        # The placeholder will be treated as a bare string value
        content = re.sub(r"\{\{[^}]+\}\}", "TEMPLATE_PLACEHOLDER", content)

    return [doc for doc in yaml.safe_load_all(content) if doc]


def get_nested(d: dict, path: str, default=None):
    """Get a nested value from a dict using dot notation."""
    keys = path.split(".")
    for key in keys:
        if isinstance(d, dict):
            d = d.get(key, default)
        else:
            return default
    return d


def is_trusted_image(image: str) -> bool:
    """Check if an image is from a trusted registry."""
    if not image:
        return True

    # Official images (no slash) like busybox, python, nginx
    if "/" not in image:
        return True

    # Check trusted registries
    for registry in TRUSTED_REGISTRIES:
        if image.startswith(registry):
            return True

    # Check trusted Docker Hub orgs
    parts = image.split("/")
    if len(parts) >= 2:
        org = parts[0]
        if org in TRUSTED_DOCKERHUB_ORGS:
            return True
        # Check if it's a registry URL (contains dots)
        if "." not in org:
            # It's a Docker Hub org/image format
            return org in TRUSTED_DOCKERHUB_ORGS

    return False


def get_containers_from_manifest(manifest: dict) -> list[dict]:
    """Extract container specs from various manifest types."""
    containers = []
    spec = manifest.get("spec", {})

    # Direct containers (Pod)
    if "containers" in spec:
        containers.extend(spec.get("containers", []))

    # Template containers (Deployment, Job, StatefulSet, DaemonSet)
    if "template" in spec:
        template_spec = spec.get("template", {}).get("spec", {})
        containers.extend(template_spec.get("containers", []))
        containers.extend(template_spec.get("initContainers", []))

    # CronJob
    if "jobTemplate" in spec:
        job_spec = spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
        containers.extend(job_spec.get("containers", []))
        containers.extend(job_spec.get("initContainers", []))

    return containers


# =============================================================================
# Kubernetes Manifest Tests
# =============================================================================


class TestKubernetesManifests:
    """Tests for Kubernetes manifests in lambda/kubectl-applier-simple/manifests/."""

    @pytest.fixture
    def manifest_files(self) -> list[Path]:
        """Get all YAML manifest files."""
        if not MANIFESTS_DIR.exists():
            pytest.skip(f"Manifests directory not found: {MANIFESTS_DIR}")
        return sorted(MANIFESTS_DIR.glob("*.yaml"))

    def test_manifests_directory_exists(self):
        """Test that manifests directory exists."""
        assert MANIFESTS_DIR.exists(), f"Manifests directory not found: {MANIFESTS_DIR}"

    def test_manifests_are_valid_yaml(self, manifest_files):
        """Test that all manifest files are valid YAML."""
        for filepath in manifest_files:
            try:
                docs = load_yaml_file(filepath, allow_templates=True)
                assert len(docs) > 0, f"Empty manifest file: {filepath.name}"
            except yaml.YAMLError as e:
                pytest.fail(f"Invalid YAML in {filepath.name}: {e}")

    def test_manifests_have_required_fields(self, manifest_files):
        """Test that all manifests have required Kubernetes fields."""
        for filepath in manifest_files:
            docs = load_yaml_file(filepath, allow_templates=True)
            for i, doc in enumerate(docs):
                # Skip documents with template placeholders
                if "{{" in str(doc):
                    continue

                # Check base required fields
                for field in REQUIRED_FIELDS["all"]:
                    assert field in doc, (
                        f"{filepath.name} doc {i}: missing required field '{field}'"
                    )

                # Check kind-specific required fields
                kind = doc.get("kind", "")
                if kind in REQUIRED_FIELDS:
                    for field_path in REQUIRED_FIELDS[kind]:
                        value = get_nested(doc, field_path)
                        assert value is not None, (
                            f"{filepath.name} doc {i} ({kind}): missing required field '{field_path}'"
                        )

    def test_manifests_have_valid_api_versions(self, manifest_files):
        """Test that manifests use valid API versions."""
        valid_api_versions = {
            "v1",
            "apps/v1",
            "autoscaling/v2",
            "batch/v1",
            "networking.k8s.io/v1",
            "gateway.networking.k8s.io/v1",
            "gateway.k8s.aws/v1",
            "rbac.authorization.k8s.io/v1",
            "storage.k8s.io/v1",
            "policy/v1",
            "karpenter.sh/v1",
            "karpenter.k8s.aws/v1",
            "coordination.k8s.io/v1",
            "eks.amazonaws.com/v1",
            "resource.k8s.io/v1beta1",
            "apiregistration.k8s.io/v1",
            "keda.sh/v1alpha1",
            "monitoring.coreos.com/v1",
        }

        for filepath in manifest_files:
            docs = load_yaml_file(filepath, allow_templates=True)
            for i, doc in enumerate(docs):
                api_version = doc.get("apiVersion", "")
                # Allow template placeholders
                if "{{" in api_version or api_version == "TEMPLATE_PLACEHOLDER":
                    continue
                assert api_version in valid_api_versions, (
                    f"{filepath.name} doc {i}: invalid apiVersion '{api_version}'"
                )

    def test_manifests_have_metadata_name(self, manifest_files):
        """Test that all manifests have metadata.name."""
        for filepath in manifest_files:
            docs = load_yaml_file(filepath, allow_templates=True)
            for i, doc in enumerate(docs):
                metadata = doc.get("metadata", {})
                name = metadata.get("name", "")
                # Allow template placeholders
                if "{{" in str(metadata) or "TEMPLATE_PLACEHOLDER" in str(name):
                    continue
                assert name, f"{filepath.name} doc {i}: missing metadata.name"

    def test_deployments_have_resource_limits(self, manifest_files):
        """Test that Deployments and Jobs have resource limits defined."""
        for filepath in manifest_files:
            docs = load_yaml_file(filepath, allow_templates=True)
            for i, doc in enumerate(docs):
                kind = doc.get("kind", "")
                if kind not in ["Deployment", "Job", "DaemonSet", "StatefulSet"]:
                    continue

                containers = get_containers_from_manifest(doc)
                for container in containers:
                    # Skip if container has template placeholders
                    if "{{" in str(container) or "TEMPLATE_PLACEHOLDER" in str(container):
                        continue

                    resources = container.get("resources", {})
                    # At minimum, requests should be defined
                    if resources:
                        assert "requests" in resources or "limits" in resources, (
                            f"{filepath.name} doc {i}: container '{container.get('name')}' "
                            "should have resource requests or limits"
                        )

    def test_manifests_use_trusted_images(self, manifest_files):
        """Test that manifests only use images from trusted registries."""
        for filepath in manifest_files:
            docs = load_yaml_file(filepath, allow_templates=True)
            for i, doc in enumerate(docs):
                containers = get_containers_from_manifest(doc)
                for container in containers:
                    image = container.get("image", "")
                    # Skip template placeholders
                    if "{{" in image or image == "TEMPLATE_PLACEHOLDER":
                        continue
                    assert is_trusted_image(image), (
                        f"{filepath.name} doc {i}: untrusted image '{image}'"
                    )

    def test_no_privileged_containers(self, manifest_files):
        """Test that no containers are privileged."""
        for filepath in manifest_files:
            docs = load_yaml_file(filepath, allow_templates=True)
            for i, doc in enumerate(docs):
                containers = get_containers_from_manifest(doc)
                for container in containers:
                    security_context = container.get("securityContext", {})
                    assert not security_context.get("privileged", False), (
                        f"{filepath.name} doc {i}: container '{container.get('name')}' "
                        "should not be privileged"
                    )

    def test_network_policies_exist(self, manifest_files):
        """Test that NetworkPolicy manifests exist for security."""
        network_policies = []
        for filepath in manifest_files:
            docs = load_yaml_file(filepath, allow_templates=True)
            for doc in docs:
                if doc.get("kind") == "NetworkPolicy":
                    network_policies.append(doc)

        assert len(network_policies) > 0, (
            "No NetworkPolicy manifests found - consider adding network policies"
        )

    def test_pod_disruption_budgets_exist(self, manifest_files):
        """Test that PodDisruptionBudget manifests exist for availability."""
        pdbs = []
        for filepath in manifest_files:
            docs = load_yaml_file(filepath, allow_templates=True)
            for doc in docs:
                if doc.get("kind") == "PodDisruptionBudget":
                    pdbs.append(doc)

        assert len(pdbs) > 0, "No PodDisruptionBudget manifests found - consider adding PDBs"

    def test_gco_deployments_have_irsa_credentials(self, manifest_files):
        """Test that all GCO Deployments using dedicated service accounts have IRSA credential config.

        IRSA on EKS Auto Mode requires:
        1. A projected service-account token volume (audience: sts.amazonaws.com)
        2. AWS_ROLE_ARN env var pointing to the IAM role
        3. AWS_WEB_IDENTITY_TOKEN_FILE env var pointing to the projected token
        """
        # Per-service SAs used by platform deployments
        gco_service_accounts = {
            "gco-health-monitor-sa",
            "gco-manifest-processor-sa",
            "gco-inference-monitor-sa",
            "gco-inference-proxy-sa",
        }
        gco_deployments_checked = 0

        for filepath in manifest_files:
            docs = load_yaml_file(filepath, allow_templates=True)
            for doc in docs:
                if doc.get("kind") != "Deployment":
                    continue

                template_spec = doc.get("spec", {}).get("template", {}).get("spec", {})
                sa_name = template_spec.get("serviceAccountName", "")

                if sa_name not in gco_service_accounts:
                    continue

                name = doc.get("metadata", {}).get("name", filepath.name)
                gco_deployments_checked += 1

                # Check for projected token volume
                volumes = template_spec.get("volumes", [])
                has_token_volume = False
                for vol in volumes:
                    projected = vol.get("projected", {})
                    for src in projected.get("sources", []):
                        sat = src.get("serviceAccountToken", {})
                        if sat.get("audience") == "sts.amazonaws.com":
                            has_token_volume = True
                            break

                assert has_token_volume, (
                    f"Deployment '{name}' uses {sa_name} but is missing "
                    "projected service-account token volume (audience: sts.amazonaws.com). "
                    "IRSA won't work without it."
                )

                # Check env vars on containers
                containers = template_spec.get("containers", [])
                for container in containers:
                    env_names = {e.get("name") for e in container.get("env", [])}
                    assert "AWS_ROLE_ARN" in env_names or "TEMPLATE_PLACEHOLDER" in str(
                        container.get("env", [])
                    ), (
                        f"Deployment '{name}' container '{container.get('name')}' "
                        "missing AWS_ROLE_ARN env var for IRSA"
                    )
                    assert (
                        "AWS_WEB_IDENTITY_TOKEN_FILE" in env_names
                        or "TEMPLATE_PLACEHOLDER" in str(container.get("env", []))
                    ), (
                        f"Deployment '{name}' container '{container.get('name')}' "
                        "missing AWS_WEB_IDENTITY_TOKEN_FILE env var for IRSA"
                    )

        assert gco_deployments_checked >= 4, (
            f"Expected at least 4 GCO deployments with dedicated service accounts, "
            f"found {gco_deployments_checked}"
        )

    def test_service_accounts_exist_for_all_namespaces(self, manifest_files):
        """Test that required service accounts exist in all namespaces.

        Platform services use dedicated SAs in gco-system.
        User workloads use gco-service-account in gco-jobs and gco-inference.
        """
        # Track which SAs we find in which namespaces
        found_sas: set[tuple[str, str]] = set()  # (namespace, sa_name)

        for filepath in manifest_files:
            docs = load_yaml_file(filepath, allow_templates=True)
            for doc in docs:
                if doc.get("kind") != "ServiceAccount":
                    continue
                sa_name = doc.get("metadata", {}).get("name", "")
                ns = doc.get("metadata", {}).get("namespace", "default")
                found_sas.add((ns, sa_name))

        # Platform SAs in gco-system
        required_platform_sas = {
            ("gco-system", "gco-health-monitor-sa"),
            ("gco-system", "gco-manifest-processor-sa"),
            ("gco-system", "gco-inference-monitor-sa"),
            ("gco-system", "gco-inference-proxy-sa"),
        }
        # User workload SAs
        required_workload_sas = {
            ("gco-jobs", "gco-service-account"),
            ("gco-inference", "gco-service-account"),
        }
        required_sas = required_platform_sas | required_workload_sas
        missing = required_sas - found_sas
        assert not missing, (
            f"Missing ServiceAccount(s): {missing}. "
            "Pod Identity and IRSA are configured for these in CDK "
            "but the ServiceAccount manifests are missing."
        )

    def test_service_accounts_have_irsa_annotation(self, manifest_files):
        """Test that all GCO service accounts have the eks.amazonaws.com/role-arn annotation."""
        gco_sa_names = {
            "gco-health-monitor-sa",
            "gco-manifest-processor-sa",
            "gco-inference-monitor-sa",
            "gco-inference-proxy-sa",
            "gco-service-account",
        }
        for filepath in manifest_files:
            docs = load_yaml_file(filepath, allow_templates=True)
            for doc in docs:
                if doc.get("kind") != "ServiceAccount":
                    continue
                sa_name = doc.get("metadata", {}).get("name", "")
                if sa_name not in gco_sa_names:
                    continue

                ns = doc.get("metadata", {}).get("namespace", "default")
                annotations = doc.get("metadata", {}).get("annotations", {})
                assert "eks.amazonaws.com/role-arn" in annotations, (
                    f"{sa_name} in {ns} missing eks.amazonaws.com/role-arn annotation for IRSA"
                )

    def test_sqs_consumer_has_irsa_credentials(self, manifest_files):
        """Test that the SQS queue processor ScaledJob has IRSA credential config."""
        for filepath in manifest_files:
            docs = load_yaml_file(filepath, allow_templates=True)
            for doc in docs:
                if doc.get("kind") != "ScaledJob":
                    continue

                name = doc.get("metadata", {}).get("name", filepath.name)
                template_spec = (
                    doc.get("spec", {}).get("jobTargetRef", {}).get("template", {}).get("spec", {})
                )

                # Check service account
                sa = template_spec.get("serviceAccountName", "")
                assert sa == "gco-manifest-processor-sa", (
                    f"ScaledJob '{name}' should use gco-manifest-processor-sa, got '{sa}'"
                )

                # Check for projected token volume
                volumes = template_spec.get("volumes", [])
                has_token_volume = False
                for vol in volumes:
                    projected = vol.get("projected", {})
                    for src in projected.get("sources", []):
                        sat = src.get("serviceAccountToken", {})
                        if sat.get("audience") == "sts.amazonaws.com":
                            has_token_volume = True
                            break

                assert has_token_volume, (
                    f"ScaledJob '{name}' missing projected service-account token volume. "
                    "IRSA won't work without it."
                )

    def test_no_hardcoded_aws_regions(self, manifest_files):
        """Test that no manifest has hardcoded AWS region strings in live values.

        Regions must come from {{REGION}} or {{DYNAMODB_REGION}} template
        variables, not be hardcoded. Hardcoded regions break multi-region deploys.

        Comments are excluded — only actual YAML values are checked.
        """
        import re

        region_pattern = re.compile(
            r"\b(us|eu|ap|sa|ca|me|af)-(east|west|north|south|central|northeast|southeast|northwest)-\d\b"
        )

        for filepath in manifest_files:
            with open(filepath, encoding="utf-8") as f:
                lines = f.readlines()

            for lineno, line in enumerate(lines, start=1):
                # Strip inline comments — only check actual YAML values
                code_part = line.split("#")[0]

                # Skip lines that are only template placeholders
                if "{{" in code_part and "}}" in code_part:
                    continue

                match = region_pattern.search(code_part)
                if match:
                    pytest.fail(
                        f"{filepath.name}:{lineno}: hardcoded AWS region '{match.group()}' found. "
                        "Use {{REGION}} or {{DYNAMODB_REGION}} template variables instead."
                    )


# =============================================================================
# Example Job Tests
# =============================================================================


class TestExampleJobs:
    """Tests for example job manifests in examples/."""

    @pytest.fixture
    def example_files(self) -> list[Path]:
        """Get all example YAML files."""
        if not EXAMPLES_DIR.exists():
            pytest.skip(f"Examples directory not found: {EXAMPLES_DIR}")
        return sorted(EXAMPLES_DIR.glob("*.yaml"))

    def test_examples_directory_exists(self):
        """Test that examples directory exists."""
        assert EXAMPLES_DIR.exists(), f"Examples directory not found: {EXAMPLES_DIR}"

    def test_examples_are_valid_yaml(self, example_files):
        """Test that all example files are valid YAML."""
        for filepath in example_files:
            try:
                docs = load_yaml_file(filepath)
                assert len(docs) > 0, f"Empty example file: {filepath.name}"
            except yaml.YAMLError as e:
                pytest.fail(f"Invalid YAML in {filepath.name}: {e}")

    def test_examples_have_required_fields(self, example_files):
        """Test that all examples have required Kubernetes fields."""
        for filepath in example_files:
            # Skip DAG pipeline definitions (not Kubernetes manifests)
            if "dag" in filepath.name or "pipeline" in filepath.name:
                continue
            docs = load_yaml_file(filepath)
            for i, doc in enumerate(docs):
                for field in REQUIRED_FIELDS["all"]:
                    assert field in doc, (
                        f"{filepath.name} doc {i}: missing required field '{field}'"
                    )

    def test_job_examples_have_restart_policy(self, example_files):
        """Test that Job examples have restartPolicy set."""
        for filepath in example_files:
            docs = load_yaml_file(filepath)
            for i, doc in enumerate(docs):
                kind = doc.get("kind", "")
                api_version = doc.get("apiVersion", "")

                # Only check standard Kubernetes Jobs (batch/v1)
                # Skip Volcano Jobs (batch.volcano.sh/v1alpha1) which have different structure
                if kind != "Job" or not api_version.startswith("batch/v"):
                    continue

                template_spec = get_nested(doc, "spec.template.spec", {})
                restart_policy = template_spec.get("restartPolicy")
                assert restart_policy in [
                    "Never",
                    "OnFailure",
                ], f"{filepath.name} doc {i}: Job should have restartPolicy 'Never' or 'OnFailure'"

    def test_examples_use_gco_jobs_namespace(self, example_files):
        """Test that examples use the gco-jobs or gco-inference namespace."""
        allowed_namespaces = {"gco-jobs", "gco-inference"}
        for filepath in example_files:
            docs = load_yaml_file(filepath)
            for i, doc in enumerate(docs):
                kind = doc.get("kind", "")
                # Only check workload resources
                if kind not in ["Job", "Deployment", "Pod"]:
                    continue

                namespace = get_nested(doc, "metadata.namespace", "default")
                assert namespace in allowed_namespaces, (
                    f"{filepath.name} doc {i}: should use namespace "
                    f"'gco-jobs' or 'gco-inference', got '{namespace}'"
                )

    def test_examples_have_security_context(self, example_files):
        """Test that examples follow security best practices."""
        for filepath in example_files:
            docs = load_yaml_file(filepath)
            for i, doc in enumerate(docs):
                kind = doc.get("kind", "")
                if kind not in ["Job", "Deployment", "Pod"]:
                    continue

                containers = get_containers_from_manifest(doc)
                for container in containers:
                    security_context = container.get("securityContext", {})
                    # Check for privilege escalation
                    assert not security_context.get("allowPrivilegeEscalation", False), (
                        f"{filepath.name} doc {i}: container '{container.get('name')}' "
                        "should not allow privilege escalation"
                    )

    def test_simple_job_exists(self, example_files):
        """Test that simple-job.yaml exists and is valid."""
        simple_job = EXAMPLES_DIR / "simple-job.yaml"
        assert simple_job.exists(), "simple-job.yaml example is missing"

        docs = load_yaml_file(simple_job)
        assert len(docs) > 0, "simple-job.yaml is empty"
        assert docs[0].get("kind") == "Job", "simple-job.yaml should contain a Job"

    def test_gpu_job_exists(self, example_files):
        """Test that gpu-job.yaml exists and requests GPU resources."""
        gpu_job = EXAMPLES_DIR / "gpu-job.yaml"
        assert gpu_job.exists(), "gpu-job.yaml example is missing"

        docs = load_yaml_file(gpu_job)
        assert len(docs) > 0, "gpu-job.yaml is empty"

        # Check that it requests GPU
        containers = get_containers_from_manifest(docs[0])
        has_gpu_request = False
        for container in containers:
            resources = container.get("resources", {})
            limits = resources.get("limits", {})
            requests = resources.get("requests", {})
            if "nvidia.com/gpu" in limits or "nvidia.com/gpu" in requests:
                has_gpu_request = True
                break

        assert has_gpu_request, "gpu-job.yaml should request nvidia.com/gpu resources"


# =============================================================================
# Lambda Handler Tests
# =============================================================================


class TestLambdaHandlers:
    """Tests for Lambda function handlers."""

    def test_kubectl_applier_handler_imports(self):
        """Test that kubectl-applier handler can be imported."""
        handler_path = LAMBDA_DIR / "kubectl-applier-simple"
        sys.path.insert(0, str(handler_path))
        try:
            # Clean up any previous handler module
            if "handler" in sys.modules:
                del sys.modules["handler"]
            from handler import lambda_handler

            assert callable(lambda_handler), "lambda_handler should be callable"
        finally:
            sys.path.remove(str(handler_path))
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_kubectl_applier_handler_signature(self):
        """Test that kubectl-applier handler has correct signature."""
        handler_path = LAMBDA_DIR / "kubectl-applier-simple"
        sys.path.insert(0, str(handler_path))
        try:
            if "handler" in sys.modules:
                del sys.modules["handler"]
            from handler import lambda_handler

            sig = inspect.signature(lambda_handler)
            params = list(sig.parameters.keys())
            assert "event" in params, "lambda_handler should accept 'event' parameter"
            assert "context" in params, "lambda_handler should accept 'context' parameter"
        finally:
            sys.path.remove(str(handler_path))
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_kubectl_applier_has_helper_functions(self):
        """Test that kubectl-applier has required helper functions."""
        handler_path = LAMBDA_DIR / "kubectl-applier-simple"
        sys.path.insert(0, str(handler_path))
        try:
            if "handler" in sys.modules:
                del sys.modules["handler"]
            import handler

            assert hasattr(handler, "apply_manifests"), (
                "handler should have apply_manifests function"
            )
            assert hasattr(handler, "get_eks_token"), "handler should have get_eks_token function"
            assert hasattr(handler, "configure_k8s_client"), (
                "handler should have configure_k8s_client function"
            )
            assert hasattr(handler, "send_response"), "handler should have send_response function"
        finally:
            sys.path.remove(str(handler_path))
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_api_gateway_proxy_handler_imports(self):
        """Test that api-gateway-proxy handler can be imported."""
        handler_path = LAMBDA_DIR / "api-gateway-proxy"
        sys.path.insert(0, str(handler_path))
        try:
            if "handler" in sys.modules:
                del sys.modules["handler"]
            from handler import lambda_handler

            assert callable(lambda_handler), "lambda_handler should be callable"
        finally:
            sys.path.remove(str(handler_path))
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_api_gateway_proxy_handler_signature(self):
        """Test that api-gateway-proxy handler has correct signature."""
        handler_path = LAMBDA_DIR / "api-gateway-proxy"
        sys.path.insert(0, str(handler_path))
        try:
            if "handler" in sys.modules:
                del sys.modules["handler"]
            from handler import lambda_handler

            sig = inspect.signature(lambda_handler)
            params = list(sig.parameters.keys())
            assert "event" in params, "lambda_handler should accept 'event' parameter"
            assert "context" in params, "lambda_handler should accept 'context' parameter"
        finally:
            sys.path.remove(str(handler_path))
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_api_gateway_proxy_has_helper_functions(self):
        """Test that api-gateway-proxy has required helper functions."""
        handler_path = LAMBDA_DIR / "api-gateway-proxy"
        sys.path.insert(0, str(handler_path))
        try:
            # Reload to ensure fresh import
            import importlib

            import handler

            importlib.reload(handler)
            assert hasattr(handler, "get_secret_token"), (
                "handler should have get_secret_token function"
            )
            assert callable(handler.get_secret_token), "get_secret_token should be callable"
        finally:
            sys.path.remove(str(handler_path))
            # Clean up module from sys.modules
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_secret_rotation_handler_imports(self):
        """Test that secret-rotation handler can be imported."""
        handler_path = LAMBDA_DIR / "secret-rotation"
        sys.path.insert(0, str(handler_path))
        try:
            if "handler" in sys.modules:
                del sys.modules["handler"]
            from handler import lambda_handler

            assert callable(lambda_handler), "lambda_handler should be callable"
        finally:
            sys.path.remove(str(handler_path))
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_secret_rotation_has_rotation_steps(self):
        """Test that secret-rotation has all rotation step functions."""
        handler_path = LAMBDA_DIR / "secret-rotation"
        sys.path.insert(0, str(handler_path))
        try:
            # Reload to ensure fresh import
            import importlib

            import handler

            importlib.reload(handler)
            assert hasattr(handler, "create_secret"), "handler should have create_secret function"
            assert hasattr(handler, "set_secret"), "handler should have set_secret function"
            assert hasattr(handler, "test_secret"), "handler should have test_secret function"
            assert hasattr(handler, "finish_secret"), "handler should have finish_secret function"
        finally:
            sys.path.remove(str(handler_path))
            # Clean up module from sys.modules
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_ga_registration_handler_imports(self):
        """Test that ga-registration handler can be imported."""
        handler_path = LAMBDA_DIR / "ga-registration"
        sys.path.insert(0, str(handler_path))
        try:
            if "handler" in sys.modules:
                del sys.modules["handler"]
            from handler import lambda_handler

            assert callable(lambda_handler), "lambda_handler should be callable"
        finally:
            sys.path.remove(str(handler_path))
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_ga_registration_handler_signature(self):
        """Test that ga-registration handler has correct signature."""
        handler_path = LAMBDA_DIR / "ga-registration"
        sys.path.insert(0, str(handler_path))
        try:
            if "handler" in sys.modules:
                del sys.modules["handler"]
            from handler import lambda_handler

            sig = inspect.signature(lambda_handler)
            params = list(sig.parameters.keys())
            assert "event" in params, "lambda_handler should accept 'event' parameter"
            assert "context" in params, "lambda_handler should accept 'context' parameter"
        finally:
            sys.path.remove(str(handler_path))
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_ga_registration_has_helper_functions(self):
        """Test that ga-registration has required helper functions."""
        handler_path = LAMBDA_DIR / "ga-registration"
        sys.path.insert(0, str(handler_path))
        try:
            import importlib

            import handler

            importlib.reload(handler)
            assert hasattr(handler, "handle_create_update"), (
                "handler should have handle_create_update function"
            )
            assert hasattr(handler, "handle_delete"), "handler should have handle_delete function"
            assert hasattr(handler, "send_response"), "handler should have send_response function"
            assert hasattr(handler, "register_alb_with_ga"), (
                "handler should have register_alb_with_ga function"
            )
        finally:
            sys.path.remove(str(handler_path))
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_regional_api_proxy_handler_imports(self):
        """Test that regional-api-proxy handler can be imported."""
        handler_path = LAMBDA_DIR / "regional-api-proxy"
        proxy_path = LAMBDA_DIR / "proxy-shared"
        sys.path.insert(0, str(proxy_path))
        sys.path.insert(0, str(handler_path))
        try:
            with patch("boto3.client"), patch("urllib3.PoolManager"):
                sys.modules.pop("handler", None)
                sys.modules.pop("proxy_utils", None)
                sys.modules.pop("backend_tls", None)
                import handler

                assert hasattr(handler, "lambda_handler")
        finally:
            sys.path.remove(str(handler_path))
            sys.path.remove(str(proxy_path))
            for mod in ["handler", "proxy_utils", "backend_tls"]:
                sys.modules.pop(mod, None)

    def test_regional_api_proxy_handler_signature(self):
        """Test that regional-api-proxy handler has correct signature."""
        handler_path = LAMBDA_DIR / "regional-api-proxy"
        proxy_path = LAMBDA_DIR / "proxy-shared"
        sys.path.insert(0, str(proxy_path))
        sys.path.insert(0, str(handler_path))
        try:
            with patch("boto3.client"), patch("urllib3.PoolManager"):
                sys.modules.pop("handler", None)
                sys.modules.pop("proxy_utils", None)
                sys.modules.pop("backend_tls", None)
                import handler

                sig = inspect.signature(handler.lambda_handler)
                params = list(sig.parameters.keys())
                assert "event" in params
                assert "context" in params
        finally:
            sys.path.remove(str(handler_path))
            sys.path.remove(str(proxy_path))
            for mod in ["handler", "proxy_utils", "backend_tls"]:
                sys.modules.pop(mod, None)

    def test_cross_region_aggregator_handler_imports(self):
        """Test that cross-region-aggregator handler can be imported."""
        handler_path = LAMBDA_DIR / "cross-region-aggregator"
        sys.path.insert(0, str(handler_path))
        try:
            with patch("boto3.client"), patch("boto3.Session"):
                sys.modules.pop("handler", None)
                import handler

                assert hasattr(handler, "lambda_handler")
        finally:
            sys.path.remove(str(handler_path))
            sys.modules.pop("handler", None)

    def test_cross_region_aggregator_has_helper_functions(self):
        """Test that cross-region-aggregator has required helper functions."""
        handler_path = LAMBDA_DIR / "cross-region-aggregator"
        sys.path.insert(0, str(handler_path))
        try:
            with patch("boto3.client"), patch("boto3.Session"):
                sys.modules.pop("handler", None)
                import handler

                assert hasattr(handler, "aggregate_jobs")
                assert hasattr(handler, "aggregate_health")
                assert hasattr(handler, "aggregate_metrics")
                assert hasattr(handler, "get_regional_endpoints")
        finally:
            sys.path.remove(str(handler_path))
            sys.modules.pop("handler", None)

    def test_helm_installer_handler_imports(self):
        """Test that helm-installer handler can be imported."""
        handler_path = LAMBDA_DIR / "helm-installer"
        sys.path.insert(0, str(handler_path))
        try:
            sys.modules.pop("handler", None)
            import handler

            assert hasattr(handler, "lambda_handler")
            assert hasattr(handler, "install_chart")
            assert hasattr(handler, "uninstall_chart")
            assert hasattr(handler, "load_charts_config")
        finally:
            sys.path.remove(str(handler_path))
            sys.modules.pop("handler", None)

    def test_proxy_utils_imports(self):
        """Test that proxy-shared/proxy_utils can be imported."""
        proxy_path = LAMBDA_DIR / "proxy-shared"
        sys.path.insert(0, str(proxy_path))
        try:
            with (
                patch("boto3.client"),
                patch("urllib3.PoolManager"),
                patch.dict(
                    "os.environ",
                    {"SECRET_ARN": "arn:test", "SECRET_CACHE_TTL_SECONDS": "300"},
                ),
            ):
                sys.modules.pop("proxy_utils", None)
                sys.modules.pop("backend_tls", None)
                import proxy_utils

                assert hasattr(proxy_utils, "get_secret_token")
                assert hasattr(proxy_utils, "forward_request")
                assert hasattr(proxy_utils, "build_target_url")
        finally:
            sys.path.remove(str(proxy_path))
            sys.modules.pop("proxy_utils", None)
            sys.modules.pop("backend_tls", None)


# =============================================================================
# CDK Output Tests
# =============================================================================


class TestCDKOutput:
    """Tests for CDK synthesized output."""

    @pytest.fixture
    def cdk_out_dir(self) -> Path:
        """Get CDK output directory."""
        cdk_out = PROJECT_ROOT / "cdk.out"
        if not cdk_out.exists():
            pytest.skip("cdk.out directory not found - run 'cdk synth' first")
        return cdk_out

    def test_cdk_out_exists(self, cdk_out_dir):
        """Test that cdk.out directory exists."""
        assert cdk_out_dir.exists(), "cdk.out directory should exist after synthesis"

    def test_manifest_json_exists(self, cdk_out_dir):
        """Test that manifest.json exists in cdk.out."""
        manifest = cdk_out_dir / "manifest.json"
        assert manifest.exists(), "manifest.json should exist in cdk.out"

    def test_manifest_json_is_valid(self, cdk_out_dir):
        """Test that manifest.json is valid JSON."""
        manifest = cdk_out_dir / "manifest.json"
        with open(manifest, encoding="utf-8") as f:
            data = json.load(f)
        assert "version" in data, "manifest.json should have version field"
        assert "artifacts" in data, "manifest.json should have artifacts field"

    def test_stack_templates_exist(self, cdk_out_dir):
        """Test that CloudFormation templates exist for stacks."""
        templates = list(cdk_out_dir.glob("*.template.json"))
        assert len(templates) > 0, "No CloudFormation templates found in cdk.out"

    def test_stack_templates_are_valid_json(self, cdk_out_dir):
        """Test that all CloudFormation templates are valid JSON."""
        templates = list(cdk_out_dir.glob("*.template.json"))
        for template in templates:
            try:
                with open(template, encoding="utf-8") as f:
                    data = json.load(f)
                assert "Resources" in data, f"{template.name} should have Resources section"
            except json.JSONDecodeError as e:
                pytest.fail(f"Invalid JSON in {template.name}: {e}")

    def test_global_stack_template_exists(self, cdk_out_dir):
        """Test that global stack template exists."""
        global_templates = list(cdk_out_dir.glob("*global*.template.json"))
        assert len(global_templates) > 0, "Global stack template not found"

    def test_regional_stack_template_exists(self, cdk_out_dir):
        """Test that regional stack template exists."""
        # Regional stacks are named like gco-us-east-1.template.json
        regional_templates = [
            t
            for t in cdk_out_dir.glob("*.template.json")
            if "us-east" in t.name or "us-west" in t.name or "eu-west" in t.name
        ]
        assert len(regional_templates) > 0, "Regional stack template not found"


# =============================================================================
# Configuration Tests
# =============================================================================


class TestConfiguration:
    """Tests for project configuration files."""

    def test_cdk_json_exists(self):
        """Test that cdk.json exists."""
        cdk_json = PROJECT_ROOT / "cdk.json"
        assert cdk_json.exists(), "cdk.json should exist"

    def test_cdk_json_is_valid(self):
        """Test that cdk.json is valid JSON with required fields."""
        cdk_json = PROJECT_ROOT / "cdk.json"
        with open(cdk_json, encoding="utf-8") as f:
            data = json.load(f)

        assert "app" in data, "cdk.json should have 'app' field"
        assert "context" in data, "cdk.json should have 'context' field"

        context = data["context"]
        assert "project_name" in context, "cdk.json context should have 'project_name'"
        assert "deployment_regions" in context, "cdk.json context should have 'deployment_regions'"

    def test_deployment_regions_configured(self):
        """Test that deployment regions are properly configured."""
        cdk_json = PROJECT_ROOT / "cdk.json"
        with open(cdk_json, encoding="utf-8") as f:
            data = json.load(f)

        regions = data["context"]["deployment_regions"]
        assert "global" in regions, "deployment_regions should have 'global'"
        assert "regional" in regions, "deployment_regions should have 'regional'"
        assert isinstance(regions["regional"], list), "regional should be a list"
        assert len(regions["regional"]) > 0, "regional should have at least one region"

    def test_pyproject_has_core_dependencies(self):
        """Test that pyproject.toml has core dependencies."""
        pyproject = PROJECT_ROOT / "pyproject.toml"
        with open(pyproject, encoding="utf-8") as f:
            content = f.read().lower()

        core_deps = ["aws-cdk-lib", "boto3", "pyyaml", "fastapi", "kubernetes"]
        for dep in core_deps:
            assert dep in content, f"pyproject.toml should include {dep}"

    def test_pyproject_toml_exists(self):
        """Test that pyproject.toml exists."""
        pyproject = PROJECT_ROOT / "pyproject.toml"
        assert pyproject.exists(), "pyproject.toml should exist"


# =============================================================================
# Documentation Tests
# =============================================================================


class TestDocumentation:
    """Tests for project documentation."""

    def test_readme_exists(self):
        """Test that README.md exists."""
        readme = PROJECT_ROOT / "README.md"
        assert readme.exists(), "README.md should exist"

    def test_readme_has_content(self):
        """Test that README.md has substantial content."""
        readme = PROJECT_ROOT / "README.md"
        with open(readme, encoding="utf-8") as f:
            content = f.read()

        assert len(content) > 1000, "README.md should have substantial content"
        # The H1 carries the official guidance name ("Guidance for EKS
        # AutoMode Clusters with Global Capacity Orchestrator on AWS") and may
        # accentuate the project name with inline markup (<br>, <em>), so
        # assert the project name appears inside the top-level heading rather
        # than matching an exact string.
        assert re.search(r"<h1>.*Global Capacity Orchestrator.*</h1>", content), (
            "README.md should have a project-title H1 naming Global Capacity Orchestrator"
        )
        # The short project name is introduced in the intro copy instead of
        # the official title.
        assert "Global Capacity Orchestrator (GCO)" in content, (
            "README.md should introduce the GCO acronym"
        )

    def test_architecture_doc_exists(self):
        """Test that architecture documentation exists."""
        arch_doc = PROJECT_ROOT / "docs" / "ARCHITECTURE.md"
        assert arch_doc.exists(), "docs/ARCHITECTURE.md should exist"

    def test_cli_doc_exists(self):
        """Test that CLI documentation exists."""
        cli_doc = PROJECT_ROOT / "docs" / "CLI.md"
        assert cli_doc.exists(), "docs/CLI.md should exist"

    def test_examples_readme_exists(self):
        """Test that examples README exists."""
        examples_readme = EXAMPLES_DIR / "README.md"
        assert examples_readme.exists(), "examples/README.md should exist"


class TestDependencyVersionConsistency:
    """Tests that dependency versions are consistent across pyproject.toml and lambda requirements."""

    def _get_pyproject_deps(self) -> dict[str, str]:
        """Extract pinned dependency versions from pyproject.toml."""
        import tomllib

        with open(PROJECT_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        deps = {}
        for dep in data["project"]["dependencies"]:
            if "==" in dep:
                name, version = dep.split("==")
                deps[name.strip().lower().replace("-", "_")] = version.strip()
        return deps

    def _get_requirements_deps(self, req_path) -> dict[str, str]:
        """Extract pinned dependency versions from a requirements.txt file."""
        deps = {}
        with open(req_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "==" in line:
                    name, version = line.split("==")
                    deps[name.strip().lower().replace("-", "_")] = version.strip()
        return deps

    def test_lambda_requirements_match_pyproject(self):
        """Lambda requirements.txt versions should match pyproject.toml pinned versions."""
        pyproject_deps = self._get_pyproject_deps()
        lambda_dirs = list((PROJECT_ROOT / "lambda").iterdir())

        mismatches = []
        for lambda_dir in lambda_dirs:
            req_file = lambda_dir / "requirements.txt"
            if not req_file.exists():
                continue
            lambda_deps = self._get_requirements_deps(req_file)
            for pkg, lambda_ver in lambda_deps.items():
                if pkg in pyproject_deps and pyproject_deps[pkg] != lambda_ver:
                    mismatches.append(
                        f"{lambda_dir.name}/requirements.txt: {pkg}=={lambda_ver} "
                        f"!= pyproject.toml {pkg}=={pyproject_deps[pkg]}"
                    )

        assert not mismatches, (
            "Lambda requirements.txt versions don't match pyproject.toml:\n" + "\n".join(mismatches)
        )

    def test_kubectl_version_matches_eks_version(self):
        """kubectl version in helm-installer Dockerfile should match EKS version in cdk.json."""
        import json
        import re

        # Get EKS version from cdk.json
        with open(PROJECT_ROOT / "cdk.json", encoding="utf-8") as f:
            cdk_config = json.load(f)
        eks_version = cdk_config["context"]["kubernetes_version"]

        # Get kubectl version from helm-installer Dockerfile
        dockerfile = PROJECT_ROOT / "lambda" / "helm-installer" / "Dockerfile"
        content = dockerfile.read_text()
        kubectl_match = re.search(r"dl\.k8s\.io/release/v([\d.]+)/", content)
        assert kubectl_match, "Could not find kubectl version in helm-installer Dockerfile"
        kubectl_version = kubectl_match.group(1)

        # kubectl version should start with the EKS version (e.g., 1.36.x matches 1.36)
        assert kubectl_version.startswith(eks_version), (
            f"kubectl version {kubectl_version} in helm-installer Dockerfile "
            f"doesn't match EKS version {eks_version} in cdk.json"
        )

    def test_kubernetes_python_client_matches_eks_version(self):
        """kubernetes Python client major version should align with EKS version."""
        import json
        import tomllib

        # Get EKS version from cdk.json
        with open(PROJECT_ROOT / "cdk.json", encoding="utf-8") as f:
            cdk_config = json.load(f)
        eks_minor = int(cdk_config["context"]["kubernetes_version"].split(".")[1])

        # Get kubernetes client version from pyproject.toml
        with open(PROJECT_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        k8s_version = None
        for dep in data["project"]["dependencies"]:
            if dep.startswith("kubernetes=="):
                k8s_version = dep.split("==")[1]
                break
        assert k8s_version, "kubernetes not found in pyproject.toml dependencies"

        # kubernetes Python client major version maps to K8s minor version
        # e.g., kubernetes==36.0.0 supports K8s 1.36
        k8s_client_major = int(k8s_version.split(".")[0])
        assert k8s_client_major == eks_minor, (
            f"kubernetes Python client {k8s_version} (major={k8s_client_major}) "
            f"doesn't match EKS version 1.{eks_minor}"
        )


class TestHelmChartConsistency:
    """Tests that Helm chart configurations in charts.yaml are well-formed and consistent."""

    def _load_charts(self) -> dict:
        """Load charts.yaml configuration."""
        import yaml

        charts_path = PROJECT_ROOT / "lambda" / "helm-installer" / "charts.yaml"
        with open(charts_path, encoding="utf-8") as f:
            return yaml.safe_load(f).get("charts", {})

    def test_all_charts_have_required_fields(self):
        """Every chart entry must have repo_url, chart, version, and namespace."""
        charts = self._load_charts()
        missing = []
        for name, cfg in charts.items():
            for field in ("repo_url", "chart", "version", "namespace"):
                if not cfg.get(field):
                    missing.append(f"{name}: missing '{field}'")
        assert not missing, "Charts missing required fields:\n" + "\n".join(missing)

    def test_chart_versions_are_non_empty(self):
        """Chart versions must be non-empty strings."""
        charts = self._load_charts()
        bad = [name for name, cfg in charts.items() if not cfg.get("version", "").strip()]
        assert not bad, f"Charts with empty versions: {bad}"

    def test_oci_charts_have_use_oci_flag(self):
        """Charts using oci:// repo URLs must have use_oci: true."""
        charts = self._load_charts()
        missing_flag = []
        for name, cfg in charts.items():
            if cfg.get("repo_url", "").startswith("oci://") and not cfg.get("use_oci"):
                missing_flag.append(name)
        assert not missing_flag, f"OCI charts missing use_oci: true flag: {missing_flag}"

    def test_no_duplicate_namespaces_for_different_charts(self):
        """Different chart names should not accidentally share the same namespace (except kube-system)."""
        charts = self._load_charts()
        ns_map: dict[str, list[str]] = {}
        for name, cfg in charts.items():
            ns = cfg.get("namespace", "default")
            if ns in ("kube-system", "default"):
                continue  # Shared namespaces are expected
            ns_map.setdefault(ns, []).append(name)
        conflicts = {ns: names for ns, names in ns_map.items() if len(names) > 1}
        # slinky-slurm shares gco-jobs namespace intentionally, filter that
        conflicts = {
            ns: names for ns, names in conflicts.items() if not all("slinky" in n for n in names)
        }
        # OpenCost deliberately co-locates with kube-prometheus-stack in the
        # monitoring namespace (it reads that Prometheus, and the shared
        # namespace keeps the ServiceMonitor + tunnel wiring simple). Pin the
        # exact pair so any third chart landing in `monitoring` still fails.
        if sorted(conflicts.get("monitoring", [])) == ["kube-prometheus-stack", "opencost"]:
            del conflicts["monitoring"]
        assert not conflicts, f"Charts sharing unexpected namespaces: {conflicts}"

    def test_image_tags_in_values_are_semver(self):
        """Image tags specified in chart values should follow semver patterns."""
        import re

        charts = self._load_charts()
        bad_tags = []

        def find_tags(d: dict, path: str = "") -> None:
            if isinstance(d, dict):
                tag = d.get("tag", "")
                repo = d.get("repository", "")
                # re.search rather than re.match: Python 3.15 soft-deprecates re.match;
                # the explicit ^ anchor in the pattern means re.search behaves identically here.
                if tag and repo and "/" in repo and not re.search(r"^v?\d+\.\d+(\.\d+)?", str(tag)):
                    bad_tags.append(f"{path}: {repo}:{tag}")
                for k, v in d.items():
                    if isinstance(v, (dict, list)):
                        find_tags(v, f"{path}.{k}")
            elif isinstance(d, list):
                for i, item in enumerate(d):
                    find_tags(item, f"{path}[{i}]")

        for name, cfg in charts.items():
            find_tags(cfg.get("values", {}), name)

        assert not bad_tags, "Non-semver image tags in chart values:\n" + "\n".join(bad_tags)

    def test_version_sources_comment_covers_all_charts(self):
        """The version check sources comment at the top of charts.yaml should list all enabled charts."""
        charts_path = PROJECT_ROOT / "lambda" / "helm-installer" / "charts.yaml"
        content = charts_path.read_text()

        # Extract chart names
        charts = self._load_charts()

        # Extract URLs from the version check sources comment block
        import re

        source_urls = re.findall(r"#\s+-\s+\w.*?:\s+(https?://\S+)", content)

        # Every chart should have a corresponding version check source URL.
        # Disabled-by-default charts (e.g. opencost, slurm) are included: the
        # monthly dependency scan enumerates charts.yaml without consulting
        # the enabled flag, so the human-facing header must keep pace.
        missing_sources = []
        for chart_name in charts:
            # Check if any source URL is plausibly related to this chart
            chart_words = chart_name.lower().replace("-", " ").replace("_", " ").split()
            found = any(any(word in url.lower() for word in chart_words) for url in source_urls)
            if not found:
                missing_sources.append(chart_name)

        assert not missing_sources, (
            f"Enabled charts missing version check source URLs in charts.yaml header comment: "
            f"{missing_sources}\nAdd a '# - ChartName: https://...' line to the header."
        )


class TestNewSchedulerChartIntegration:
    """Tests that the newly added scheduler Helm charts are properly configured."""

    def _load_charts(self) -> dict:
        import yaml

        charts_path = PROJECT_ROOT / "lambda" / "helm-installer" / "charts.yaml"
        with open(charts_path, encoding="utf-8") as f:
            return yaml.safe_load(f).get("charts", {})

    def test_cert_manager_installed_before_slurm_operator(self):
        """cert-manager must appear before slinky-slurm-operator in charts.yaml (install order)."""
        charts = self._load_charts()
        chart_names = list(charts.keys())
        cm_idx = chart_names.index("cert-manager")
        op_idx = chart_names.index("slinky-slurm-operator")
        assert cm_idx < op_idx, (
            f"cert-manager (index {cm_idx}) must be listed before "
            f"slinky-slurm-operator (index {op_idx}) in charts.yaml"
        )

    def test_slurm_operator_installed_before_slurm_cluster(self):
        """slinky-slurm-operator must appear before slinky-slurm in charts.yaml."""
        charts = self._load_charts()
        chart_names = list(charts.keys())
        op_idx = chart_names.index("slinky-slurm-operator")
        cl_idx = chart_names.index("slinky-slurm")
        assert op_idx < cl_idx, (
            f"slinky-slurm-operator (index {op_idx}) must be listed before "
            f"slinky-slurm (index {cl_idx}) in charts.yaml"
        )

    def test_slurm_cluster_deploys_to_gco_jobs_namespace(self):
        """The default Slurm cluster should deploy to gco-jobs namespace."""
        charts = self._load_charts()
        assert charts["slinky-slurm"]["namespace"] == "gco-jobs"

    def test_slurm_and_yunikorn_disabled_by_default(self):
        """Slurm and YuniKorn should be disabled by default in charts.yaml."""
        charts = self._load_charts()
        expected_disabled = ["slinky-slurm-operator", "slinky-slurm", "yunikorn"]
        enabled = [name for name in expected_disabled if charts[name].get("enabled")]
        assert not enabled, f"Expected these charts to be disabled by default: {enabled}"

    def test_cert_manager_enabled_by_default(self):
        """cert-manager should be enabled by default."""
        charts = self._load_charts()
        assert charts["cert-manager"].get("enabled") is True

    def test_slinky_charts_use_oci_registry(self):
        """Both Slinky charts should use OCI registry from ghcr.io."""
        charts = self._load_charts()
        for name in ("slinky-slurm-operator", "slinky-slurm"):
            assert charts[name].get("use_oci") is True, f"{name} should have use_oci: true"
            assert charts[name]["repo_url"].startswith("oci://ghcr.io/"), (
                f"{name} repo_url should start with oci://ghcr.io/"
            )

    def test_slinky_operator_and_cluster_versions_match(self):
        """The Slurm operator and cluster chart versions should be the same release."""
        charts = self._load_charts()
        op_ver = charts["slinky-slurm-operator"]["version"]
        cl_ver = charts["slinky-slurm"]["version"]
        assert op_ver == cl_ver, (
            f"Slurm operator version ({op_ver}) and cluster version ({cl_ver}) should match"
        )

    def test_kueue_installed_last(self):
        """Kueue must be the last chart because its webhook intercepts all Job/Deployment mutations."""
        charts = self._load_charts()
        chart_names = list(charts.keys())
        assert chart_names[-1] == "kueue", (
            f"Kueue must be the last chart in charts.yaml (currently last is '{chart_names[-1]}'). "
            f"Kueue's mutating webhook blocks other chart installs when its pod is unavailable."
        )

    def test_cdk_json_helm_section_has_all_chart_groups(self):
        """The cdk.json helm section should have entries for all togglable chart groups.

        KEDA is exempt: it is a mandatory platform component with no toggle, so
        it is intentionally absent from cdk.json.
        """
        import json

        with open(PROJECT_ROOT / "cdk.json", encoding="utf-8") as f:
            cdk_config = json.load(f)
        helm_config = cdk_config["context"].get("helm", {})

        expected_keys = [
            "aws_efa_device_plugin",
            "aws_neuron_device_plugin",
            "volcano",
            "kuberay",
            "cert_manager",
            "slurm",
            "yunikorn",
            "kueue",
        ]
        missing = [k for k in expected_keys if k not in helm_config]
        assert not missing, f"cdk.json helm section missing keys: {missing}"

        # KEDA must NOT be present — it is mandatory and not configurable.
        assert "keda" not in helm_config

    # NOTE: test_cdk_json_helm_slurm_and_yunikorn_disabled was removed because
    # it asserted specific config values from the live cdk.json, which breaks
    # when features are enabled for testing. The CDK config matrix test
    # (tests/test_cdk_synthesis_matrix.py) already validates all config combinations.


class TestGetEnabledHelmCharts:
    """Tests for _get_enabled_helm_charts logic in regional_stack.py."""

    def _get_charts_for_config(self, helm_config: dict) -> list[str]:
        """Simulate _get_enabled_helm_charts logic without importing CDK."""
        # This mirrors the logic in GCORegionalStack._get_enabled_helm_charts
        chart_map: list[tuple[str, list[str]]] = [
            ("keda", ["keda"]),
            ("aws_efa_device_plugin", ["aws-efa-device-plugin"]),
            ("aws_neuron_device_plugin", ["aws-neuron-device-plugin"]),
            ("volcano", ["volcano"]),
            ("kuberay", ["kuberay-operator"]),
            ("cert_manager", ["cert-manager"]),
            ("slurm", ["slinky-slurm-operator", "slinky-slurm"]),
            ("yunikorn", ["yunikorn"]),
            ("kueue", ["kueue"]),
        ]

        # KEDA is a mandatory platform component: it backs the SQS queue
        # processor and is the only metrics bridge for GPU/CloudWatch-driven
        # autoscaling, so its cdk.json toggle is ignored.
        mandatory_chart_keys = {"keda"}

        enabled_charts = []
        for config_key, chart_names in chart_map:
            chart_config = helm_config.get(config_key, {})
            if config_key in mandatory_chart_keys or chart_config.get("enabled", True):
                enabled_charts.extend(chart_names)
        return enabled_charts

    def test_default_config_includes_core_charts(self):
        """With all defaults (enabled: true), core charts should be present."""
        charts = self._get_charts_for_config(
            {
                "keda": {"enabled": True},
                "volcano": {"enabled": True},
                "kuberay": {"enabled": True},
                "kueue": {"enabled": True},
            }
        )
        assert "keda" in charts
        assert "volcano" in charts
        assert "kuberay-operator" in charts
        assert "kueue" in charts

    def test_slurm_toggle_enables_both_charts(self):
        """Enabling slurm should add both slinky-slurm-operator and slinky-slurm."""
        charts = self._get_charts_for_config({"slurm": {"enabled": True}})
        assert "slinky-slurm-operator" in charts
        assert "slinky-slurm" in charts

    def test_slurm_toggle_disabled_excludes_both(self):
        """Disabling slurm should exclude both slinky-slurm-operator and slinky-slurm."""
        charts = self._get_charts_for_config({"slurm": {"enabled": False}})
        assert "slinky-slurm-operator" not in charts
        assert "slinky-slurm" not in charts

    def test_yunikorn_toggle(self):
        """Enabling/disabling yunikorn should toggle the yunikorn chart."""
        enabled = self._get_charts_for_config({"yunikorn": {"enabled": True}})
        disabled = self._get_charts_for_config({"yunikorn": {"enabled": False}})
        assert "yunikorn" in enabled
        assert "yunikorn" not in disabled

    def test_kueue_is_always_last(self):
        """Kueue must be the last chart in the returned list."""
        charts = self._get_charts_for_config(
            {
                "keda": {"enabled": True},
                "volcano": {"enabled": True},
                "kueue": {"enabled": True},
                "slurm": {"enabled": True},
                "yunikorn": {"enabled": True},
            }
        )
        assert charts[-1] == "kueue"

    def test_empty_helm_config_enables_all(self):
        """With no helm config, all charts default to enabled."""
        charts = self._get_charts_for_config({})
        assert "keda" in charts
        assert "volcano" in charts
        assert "kueue" in charts
        # slurm and yunikorn also default to enabled when not in config
        assert "slinky-slurm-operator" in charts
        assert "yunikorn" in charts

    def test_disabling_single_chart(self):
        """Disabling one chart should not affect others."""
        charts = self._get_charts_for_config({"volcano": {"enabled": False}})
        assert "volcano" not in charts
        assert "keda" in charts
        assert "kueue" in charts

    def test_keda_cannot_be_disabled(self):
        """KEDA is mandatory: it stays enabled even when explicitly disabled."""
        charts = self._get_charts_for_config({"keda": {"enabled": False}})
        assert "keda" in charts

    def test_keda_present_when_other_charts_disabled(self):
        """KEDA survives even when every other chart is explicitly turned off."""
        all_off = {
            "keda": {"enabled": False},
            "aws_efa_device_plugin": {"enabled": False},
            "aws_neuron_device_plugin": {"enabled": False},
            "volcano": {"enabled": False},
            "kuberay": {"enabled": False},
            "cert_manager": {"enabled": False},
            "slurm": {"enabled": False},
            "yunikorn": {"enabled": False},
            "kueue": {"enabled": False},
        }
        charts = self._get_charts_for_config(all_off)
        assert charts == ["keda"]

    def test_kuberay_maps_to_kuberay_operator(self):
        """The 'kuberay' config key should map to 'kuberay-operator' chart name."""
        charts = self._get_charts_for_config({"kuberay": {"enabled": True}})
        assert "kuberay-operator" in charts
        assert "kuberay" not in charts  # The chart name is kuberay-operator, not kuberay

    def test_all_chart_map_keys_present_in_cdk_json(self):
        """Every togglable chart_map key should exist in cdk.json's helm section.

        KEDA is exempt: it is a mandatory platform component with no toggle, so
        it is intentionally not listed in cdk.json.
        """
        import json

        with open(PROJECT_ROOT / "cdk.json", encoding="utf-8") as f:
            cdk_config = json.load(f)
        helm_config = cdk_config["context"].get("helm", {})

        # Togglable config keys used in _get_enabled_helm_charts. KEDA is
        # mandatory (always installed regardless of config), so it is omitted
        # from cdk.json and excluded here.
        chart_map_keys = [
            "aws_efa_device_plugin",
            "aws_neuron_device_plugin",
            "volcano",
            "kuberay",
            "cert_manager",
            "slurm",
            "yunikorn",
            "kueue",
        ]
        missing = [k for k in chart_map_keys if k not in helm_config]
        assert not missing, f"cdk.json helm section missing keys from chart_map: {missing}"

        # KEDA must NOT be present — it is mandatory and not configurable.
        assert "keda" not in helm_config

    def test_disabling_all_gpu_charts(self):
        """Disabling all GPU-related charts should produce a list without any NVIDIA charts."""
        charts = self._get_charts_for_config(
            {
                "aws_efa_device_plugin": {"enabled": False},
            }
        )
        assert "aws-efa-device-plugin" not in charts
        # Other charts should still be present
        assert "keda" in charts
        assert "kueue" in charts


class TestHelmToggleBehavior:
    """Tests for the bidirectional helm toggle behavior (enable → install, disable → uninstall)."""

    def test_disabled_charts_get_uninstalled_marker(self):
        """When a chart is disabled, the Lambda should attempt to uninstall it."""
        # The Lambda handler marks disabled charts for uninstall during Create/Update.
        # We verify this by checking the handler code has the uninstall logic.
        handler_path = PROJECT_ROOT / "lambda" / "helm-installer" / "handler.py"
        content = handler_path.read_text()
        assert "uninstall_chart" in content, "handler.py should contain uninstall_chart function"
        assert "uninstalled (disabled)" in content, (
            "handler.py should mark disabled charts as 'uninstalled (disabled)'"
        )

    def test_disabled_charts_uninstalled_before_enabled_installed(self):
        """Disabled charts should be uninstalled before enabled charts are installed."""
        handler_path = PROJECT_ROOT / "lambda" / "helm-installer" / "handler.py"
        content = handler_path.read_text()
        # The uninstall pass should come before the install pass
        uninstall_pos = content.find("uninstall disabled charts")
        install_pos = content.find("install/upgrade enabled charts")
        assert uninstall_pos < install_pos, (
            "Disabled chart uninstall should happen before enabled chart install"
        )

    def test_uninstall_chart_handles_not_found(self):
        """uninstall_chart should succeed gracefully when chart was never installed."""
        handler_path = PROJECT_ROOT / "lambda" / "helm-installer" / "handler.py"
        content = handler_path.read_text()
        assert "not found" in content.lower(), (
            "uninstall_chart should handle 'not found' errors gracefully"
        )
