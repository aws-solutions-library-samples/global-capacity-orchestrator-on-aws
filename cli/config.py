"""
CLI Configuration management for GCO.

Handles configuration loading, caching, and validation for the CLI.
Supports both file-based configuration and environment variables.

Configuration is loaded in this order (later sources override earlier):
1. Default values
2. cdk.json (if present in current directory)
3. ~/.gco/config.yaml or config.json
4. Environment variables (GCO_*)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def _load_cdk_json() -> dict[str, Any]:
    """Load deployment_regions from cdk.json if present."""
    cdk_json_path = Path.cwd() / "cdk.json"
    if cdk_json_path.exists():
        try:
            with open(cdk_json_path, encoding="utf-8") as f:
                data = json.load(f)
                result = data.get("context", {}).get("deployment_regions", {})
                if isinstance(result, dict):
                    return result
        except Exception as e:
            logger.debug("Failed to load cdk.json: %s", e)
    return {}


def _load_cdk_project_name() -> str | None:
    """Load ``context.project_name`` from cdk.json if present (#139).

    The CDK reads the deployment's identity from ``context.project_name``
    (see ``gco/config/config_loader.ConfigLoader.get_project_name``). The CLI
    must resolve the same value so it addresses the right project-scoped
    resources — otherwise a non-``gco`` deployment's stacks, EKS clusters, and
    DynamoDB tables are unreachable from the CLI.
    """
    cdk_json_path = Path.cwd() / "cdk.json"
    if cdk_json_path.exists():
        try:
            with open(cdk_json_path, encoding="utf-8") as f:
                data = json.load(f)
                value = data.get("context", {}).get("project_name")
                if isinstance(value, str) and value:
                    return value
        except Exception as e:
            logger.debug("Failed to load project_name from cdk.json: %s", e)
    return None


@dataclass
class GCOConfig:
    """Configuration for GCO CLI."""

    # Project settings
    project_name: str = "gco"

    # AWS settings - defaults can be overridden by cdk.json or env vars
    default_region: str = "us-east-1"
    api_gateway_region: str = "us-east-2"
    global_region: str = "us-east-2"
    monitoring_region: str = "us-east-2"

    # Stack naming
    global_stack_name: str = "gco-global"
    api_gateway_stack_name: str = "gco-api-gateway"
    regional_stack_prefix: str = "gco"

    # Default namespace for namespaced workload resources
    default_namespace: str = "gco-jobs"

    # Capacity checking
    spot_price_history_days: int = 7
    capacity_check_timeout: int = 30

    # File system settings
    efs_mount_path: str = "/mnt/gco"
    fsx_mount_path: str = "/mnt/fsx"

    # Output settings
    output_format: str = "table"  # table, json, yaml
    verbose: bool = False

    # Cache settings
    cache_dir: str = field(default_factory=lambda: str(Path.home() / ".gco" / "cache"))
    cache_ttl_seconds: int = 300  # 5 minutes

    # API access mode
    use_regional_api: bool = False  # Use regional APIs for private access

    # Tracks fields explicitly supplied by a file/environment source so a
    # value equal to the dataclass default can still override an earlier source.
    _specified_fields: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        # The stack names and regional prefix always derive from
        # project_name (#139) so a non-"gco" deployment addresses its own
        # stacks/clusters. get_config() re-applies this after merging the
        # cdk.json / file / env overrides.
        self._apply_project_scoped_names()

    def _apply_project_scoped_names(self) -> None:
        """Derive project-scoped stack names from ``project_name`` (#139).

        The global/api-gateway stack names and the regional-stack prefix are
        not independent knobs — they are always ``<project_name>-global``,
        ``<project_name>-api-gateway``, and ``<project_name>`` respectively, to
        match what the CDK deploys. For the default ``gco`` this yields the
        identical ``gco-*`` names.
        """
        self.global_stack_name = f"{self.project_name}-global"
        self.api_gateway_stack_name = f"{self.project_name}-api-gateway"
        self.regional_stack_prefix = self.project_name

    @classmethod
    def from_file(cls, config_path: str | None = None) -> GCOConfig:
        """Load configuration from a file, or defaults if no default file exists."""
        explicit_path = config_path is not None
        if config_path is None:
            default_paths = [
                Path.cwd() / ".gco.yaml",
                Path.cwd() / ".gco.json",
                Path.home() / ".gco" / "config.yaml",
                Path.home() / ".gco" / "config.json",
            ]
            config_path = next((str(path) for path in default_paths if path.exists()), None)

        if config_path is None:
            return cls()

        path = Path(config_path).expanduser()
        if not path.exists():
            if explicit_path:
                raise FileNotFoundError(f"Configuration file not found: {path}")
            return cls()

        with open(path, encoding="utf-8") as f:
            data = json.load(f) if path.suffix.lower() == ".json" else yaml.safe_load(f)

        # ``yaml.safe_load`` returns None for an empty document.
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"Configuration file must contain a mapping: {path}")

        valid_fields = {
            item.name
            for item in dataclass_fields(cls)
            if item.init and not item.name.startswith("_")
        }
        values = {key: value for key, value in data.items() if key in valid_fields}
        config = cls(**values)
        config._specified_fields = set(values)
        return config

    @classmethod
    def from_env(cls) -> GCOConfig:
        """Load configuration from environment variables."""
        config = cls()

        env_mappings = {
            "GCO_PROJECT_NAME": "project_name",
            "GCO_DEFAULT_REGION": "default_region",
            "GCO_API_GATEWAY_REGION": "api_gateway_region",
            "GCO_GLOBAL_REGION": "global_region",
            "GCO_MONITORING_REGION": "monitoring_region",
            "GCO_DEFAULT_NAMESPACE": "default_namespace",
            "GCO_OUTPUT_FORMAT": "output_format",
            "GCO_VERBOSE": "verbose",
            "GCO_CACHE_DIR": "cache_dir",
            "GCO_REGIONAL_API": "use_regional_api",
        }

        for env_var, attr in env_mappings.items():
            value: Any = os.environ.get(env_var)
            if value is not None:
                if attr in {"verbose", "use_regional_api"}:
                    setattr(config, attr, value.lower() in ("true", "1", "yes"))
                else:
                    setattr(config, attr, value)
                config._specified_fields.add(attr)

        return config

    def to_dict(self) -> dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "project_name": self.project_name,
            "default_region": self.default_region,
            "api_gateway_region": self.api_gateway_region,
            "global_region": self.global_region,
            "monitoring_region": self.monitoring_region,
            "global_stack_name": self.global_stack_name,
            "api_gateway_stack_name": self.api_gateway_stack_name,
            "regional_stack_prefix": self.regional_stack_prefix,
            "default_namespace": self.default_namespace,
            "spot_price_history_days": self.spot_price_history_days,
            "capacity_check_timeout": self.capacity_check_timeout,
            "efs_mount_path": self.efs_mount_path,
            "fsx_mount_path": self.fsx_mount_path,
            "output_format": self.output_format,
            "verbose": self.verbose,
            "cache_dir": self.cache_dir,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "use_regional_api": self.use_regional_api,
        }

    def save(self, config_path: str | None = None) -> None:
        """Save configuration to file."""
        if config_path is None:
            config_dir = Path.home() / ".gco"
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path = str(config_dir / "config.yaml")

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(self.to_dict(), f, default_flow_style=False)


def get_config(config_path: str | None = None) -> GCOConfig:
    """Get merged configuration from cdk.json, file, and environment.

    Configuration is loaded in this order (later sources override earlier):
    1. Default values
    2. cdk.json deployment regions and project name (if present)
    3. ``config_path`` when supplied, otherwise the first default config file
    4. Environment variables (GCO_*)
    """
    # Start with defaults
    config = GCOConfig()

    # cdk.json is the CDK's source of truth for project_name (#139). Load it
    # first so file/env can still override per the documented precedence.
    cdk_project = _load_cdk_project_name()
    if cdk_project:
        config.project_name = cdk_project

    # Load from cdk.json if present
    cdk_regions = _load_cdk_json()
    if cdk_regions:
        if "api_gateway" in cdk_regions:
            config.api_gateway_region = cdk_regions["api_gateway"]
        if "global" in cdk_regions:
            config.global_region = cdk_regions["global"]
        if "monitoring" in cdk_regions:
            config.monitoring_region = cdk_regions["monitoring"]
        if cdk_regions.get("regional"):
            config.default_region = cdk_regions["regional"][0]

    # Merge only fields explicitly supplied by real file/env loaders. For
    # callers/tests that construct a GCOConfig directly, retain the historical
    # non-default inference as a compatibility fallback.
    derived_fields = {"global_stack_name", "api_gateway_stack_name", "regional_stack_prefix"}
    mergeable_fields = [
        item.name
        for item in dataclass_fields(GCOConfig)
        if item.init and item.name not in derived_fields and not item.name.startswith("_")
    ]
    defaults = GCOConfig()

    def apply_overrides(source: GCOConfig) -> None:
        specified = set(source._specified_fields)
        if not specified:
            specified = {
                attr
                for attr in mergeable_fields
                if getattr(source, attr) != getattr(defaults, attr)
            }
        for attr in mergeable_fields:
            if attr in specified:
                setattr(config, attr, getattr(source, attr))

    apply_overrides(GCOConfig.from_file(config_path))
    apply_overrides(GCOConfig.from_env())

    # Stack names / regional prefix always track the final project_name (#139),
    # regardless of which source set it.
    config._apply_project_scoped_names()

    return config
