"""
GCO (Global Capacity Orchestrator on AWS) - Multi-Region EKS Auto Mode Platform for AI/ML Workloads.

GCO is a production-ready AWS infrastructure that deploys EKS Auto Mode
clusters across multiple regions with Global Accelerator for low-latency access.
It's designed for AI/ML workloads requiring GPU compute and provides a REST API
for Kubernetes manifest submission.

Key Features:
- Multi-region deployment with automatic failover
- EKS Auto Mode with GPU support (x86 and ARM)
- IAM-authenticated API Gateway entry point
- Secure request validation with short-lived HMAC envelopes
- CloudWatch monitoring and alerting

Package Structure:
- gco.stacks: CDK stack definitions (global, regional, API gateway, monitoring)
- gco.services: Kubernetes services (health monitor, manifest processor, queue processor, inference monitor)
- gco.models: Pydantic data models for configuration and API
- gco.config: Configuration loader from cdk.json
"""

from gco._version import __version__ as __version__  # noqa: F401

__author__ = "Jacob Mevorach (@Jmevorach)"
__description__ = "Multi-region EKS Auto Mode platform for AI/ML workload orchestration"
