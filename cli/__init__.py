"""
GCO CLI - Command-line interface for managing GCO clusters and jobs.

This package provides a comprehensive CLI for:
- Submitting jobs to GCO clusters
- Querying job status across regions
- Checking capacity availability (spot/on-demand)
- Managing file system data (EFS/FSx)
- Deploying and managing CDK stacks
- Auto-discovering regional stacks

Usage:
    gco jobs submit job.yaml --region us-east-1
    gco jobs list --all-regions
    gco capacity check --instance-type g4dn.xlarge --region us-east-1
    gco stacks deploy gco-us-east-1
    gco stacks deploy --all -y
    gco files list --region us-east-1
"""

# Try to import version from gco package, fall back to local version
try:
    from gco._version import __version__
except ImportError:
    __version__ = "7.1.0"

from .analytics_user_mgmt import (
    discover_api_endpoint,
    discover_cognito_client_id,
    discover_cognito_pool_id,
    srp_authenticate,
)
from .aws_client import GCOAWSClient, get_aws_client
from .capacity import CapacityChecker, CapacityEstimate, get_capacity_checker
from .config import GCOConfig, get_config
from .costs import CostTracker, get_cost_tracker
from .files import FileSystemClient, FileSystemInfo, get_file_system_client
from .jobs import JobInfo, JobManager, get_job_manager
from .kubectl_helpers import update_kubeconfig
from .output import OutputFormatter, get_output_formatter
from .stacks import StackInfo, StackManager, get_stack_manager

__all__ = [
    "CapacityChecker",
    "CapacityEstimate",
    "CostTracker",
    "FileSystemClient",
    "FileSystemInfo",
    "GCOAWSClient",
    "GCOConfig",
    "JobInfo",
    "JobManager",
    "OutputFormatter",
    "StackInfo",
    "StackManager",
    "__version__",
    "discover_api_endpoint",
    "discover_cognito_client_id",
    "discover_cognito_pool_id",
    "get_aws_client",
    "get_capacity_checker",
    "get_config",
    "get_cost_tracker",
    "get_file_system_client",
    "get_job_manager",
    "get_output_formatter",
    "get_stack_manager",
    "srp_authenticate",
    "update_kubeconfig",
]
