"""
Consistency tests between Gateway API target-group health-check paths and the
auth middleware allowlist.

Parses every Kubernetes TargetGroupConfiguration manifest under
lambda/kubectl-applier-simple/manifests/ after rendering unresolved template
variables to inert scalar placeholders, pulls each
spec.defaultConfiguration.healthCheckConfig.healthCheckPath value, and asserts
that it appears in gco.services.auth_middleware.UNAUTHENTICATED_PATHS. Also
checks that the GA health check path from cdk.json is allowlisted. Prevents the
regression where a target-group health check path is introduced that the
middleware rejects with 403, silently taking the region out of GA rotation.
"""

import json
import re
from pathlib import Path

import yaml

from gco.services.auth_middleware import UNAUTHENTICATED_PATHS

PROJECT_ROOT = Path(__file__).parent.parent


class TestHealthCheckPathCoverage:
    """Verify every ALB health check path is in UNAUTHENTICATED_PATHS."""

    def _get_target_group_health_paths(self) -> dict[str, str]:
        """Extract health paths from all TargetGroupConfiguration manifests.

        Returns:
            Dict mapping manifest filename and resource name to health check path.
        """
        manifests_dir = PROJECT_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
        paths: dict[str, str] = {}

        for manifest_file in sorted(manifests_dir.glob("*.yaml")):
            with open(manifest_file, encoding="utf-8") as f:
                content = f.read()
            # Render deployment-time tokens as inert YAML scalars so templated
            # multi-document Gateway manifests remain part of this coverage.
            rendered = re.sub(r"\{\{[^{}]+\}\}", "placeholder", content)
            for doc in yaml.safe_load_all(rendered):
                if not isinstance(doc, dict):
                    continue
                if doc.get("kind") != "TargetGroupConfiguration":
                    continue
                health_path = (
                    doc.get("spec", {})
                    .get("defaultConfiguration", {})
                    .get("healthCheckConfig", {})
                    .get("healthCheckPath")
                )
                if health_path:
                    name = doc["metadata"]["name"]
                    paths[f"{manifest_file.name}:{name}"] = health_path

        return paths

    def test_all_target_group_health_paths_are_unauthenticated(self):
        """Every ALB target-group health path must be in UNAUTHENTICATED_PATHS.

        If this test fails, a TargetGroupConfiguration has a health check path
        that the auth middleware will reject with 403. Global Accelerator uses
        ALB target group health to determine whether a region is healthy. A 403
        on the health path makes GA think the entire region is down.

        Fix: Add the health check path to UNAUTHENTICATED_PATHS in
        gco/services/auth_middleware.py.
        """
        target_group_paths = self._get_target_group_health_paths()
        assert target_group_paths, (
            "No TargetGroupConfiguration health check paths found — test setup error"
        )

        missing = []
        for source, path in target_group_paths.items():
            if path not in UNAUTHENTICATED_PATHS:
                missing.append(f"  {source}: {path}")

        if missing:
            paths_list = "\n".join(missing)
            raise AssertionError(
                f"The following target-group health check paths are NOT in "
                f"UNAUTHENTICATED_PATHS and will be rejected by the auth "
                f"middleware (breaking GA health checks):\n{paths_list}\n\n"
                f"Fix: Add them to UNAUTHENTICATED_PATHS in "
                f"gco/services/auth_middleware.py"
            )

    def test_ga_health_check_path_is_unauthenticated(self):
        """The GA health check path from cdk.json must be in UNAUTHENTICATED_PATHS.

        Global Accelerator's HTTP health check hits this path directly on
        the ALB. If the auth middleware rejects it, GA marks the ALB as
        unhealthy and stops routing traffic to the region.
        """
        cdk_json = PROJECT_ROOT / "cdk.json"
        with open(cdk_json, encoding="utf-8") as f:
            config = json.load(f)

        ga_health_path = (
            config.get("context", {})
            .get("global_accelerator", {})
            .get("health_check_path", "/api/v1/health")
        )

        assert ga_health_path in UNAUTHENTICATED_PATHS, (
            f"GA health check path '{ga_health_path}' from cdk.json is NOT in "
            f"UNAUTHENTICATED_PATHS. This will cause GA to mark the ALB as "
            f"unhealthy. Add it to UNAUTHENTICATED_PATHS in "
            f"gco/services/auth_middleware.py"
        )

    def test_unauthenticated_paths_includes_standard_probes(self):
        """Standard Kubernetes probe paths must always be unauthenticated."""
        for path in ["/healthz", "/readyz"]:
            assert path in UNAUTHENTICATED_PATHS, (
                f"Standard probe path '{path}' missing from UNAUTHENTICATED_PATHS"
            )
