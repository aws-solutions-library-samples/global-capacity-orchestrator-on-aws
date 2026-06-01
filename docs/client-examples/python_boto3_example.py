#!/usr/bin/env python3
"""
Example: Submit Kubernetes manifests to GCO API Gateway using Python boto3

This example demonstrates how to authenticate with AWS IAM and submit manifests
to the GCO API Gateway using AWS SigV4 request signing.

Requirements:
    pip install boto3 requests aws-requests-auth

Usage:
    python python_boto3_example.py
"""

import json

import boto3
import requests
from aws_requests_auth.aws_auth import AWSRequestsAuth


def get_api_endpoint(region: str | None = None) -> str:
    """
    Get the API Gateway endpoint URL from CloudFormation stack outputs.

    Args:
        region: AWS region where API Gateway stack is deployed.
                If not provided, reads from cdk.json or defaults to us-east-2.

    Returns:
        API Gateway invoke URL
    """
    cfn = boto3.client("cloudformation", region_name=region)

    response = cfn.describe_stacks(StackName="gco-api-gateway")
    outputs = response["Stacks"][0]["Outputs"]

    for output in outputs:
        if output["OutputKey"] == "ApiEndpoint":
            # Remove trailing slash if present
            return output["OutputValue"].rstrip("/")

    raise ValueError("ApiEndpoint not found in stack gco-api-gateway")


def create_aws_auth(api_host: str, region: str) -> AWSRequestsAuth:
    """
    Create AWS SigV4 authentication for API Gateway requests.

    Args:
        api_host: API Gateway host (e.g., 'abc123.execute-api.us-east-2.amazonaws.com')
        region: AWS region

    Returns:
        AWSRequestsAuth object for request signing
    """
    session = boto3.Session()
    credentials = session.get_credentials()

    return AWSRequestsAuth(
        aws_access_key=credentials.access_key,
        aws_secret_access_key=credentials.secret_key,
        aws_token=credentials.token,  # For temporary credentials (STS, IAM roles)
        aws_host=api_host,
        aws_region=region,
        aws_service="execute-api",
    )


def submit_manifests(
    api_endpoint: str,
    auth: AWSRequestsAuth,
    manifests: list,
    namespace: str = None,
    dry_run: bool = False,
) -> dict:
    """
    Submit Kubernetes manifests to the API Gateway.

    Args:
        api_endpoint: API Gateway base URL
        auth: AWS authentication object
        manifests: List of Kubernetes manifests as dictionaries
        namespace: Default namespace for manifests without one specified
        dry_run: If True, validate without applying

    Returns:
        API response as dictionary
    """
    url = f"{api_endpoint}/api/v1/manifests"

    payload = {"manifests": manifests, "dry_run": dry_run}

    if namespace:
        payload["namespace"] = namespace

    response = requests.post(
        url, json=payload, auth=auth, headers={"Content-Type": "application/json"}, timeout=30
    )

    response.raise_for_status()
    return response.json()


def get_health(api_endpoint: str, auth: AWSRequestsAuth) -> dict:
    """
    Get cluster health status.

    Args:
        api_endpoint: API Gateway base URL
        auth: AWS authentication object

    Returns:
        API response as dictionary
    """
    url = f"{api_endpoint}/api/v1/health"

    response = requests.get(url, auth=auth, timeout=30)
    response.raise_for_status()
    return response.json()


def get_api_gateway_region() -> str:
    """Get API Gateway region from cdk.json or default to us-east-2."""
    import json
    from pathlib import Path

    # Try to read from cdk.json
    cdk_json_path = Path(__file__).parent.parent.parent / "cdk.json"
    if cdk_json_path.exists():
        try:
            with open(cdk_json_path, encoding="utf-8") as f:
                data = json.load(f)
                deployment_regions = data.get("context", {}).get("deployment_regions", {})
                if "api_gateway" in deployment_regions:
                    return deployment_regions["api_gateway"]
        except Exception:
            pass

    # Default to us-east-2
    return "us-east-2"


def main():
    # Configuration - reads from cdk.json or defaults to us-east-2
    API_REGION = get_api_gateway_region()
    print(f"Using API Gateway region: {API_REGION}")

    # Get API Gateway endpoint from CloudFormation
    print("Getting API Gateway endpoint from stack gco-api-gateway...")
    api_endpoint = get_api_endpoint(API_REGION)
    print(f"API Endpoint: {api_endpoint}")

    # Extract host from endpoint URL
    api_host = api_endpoint.replace("https://", "").replace("http://", "").split("/")[0]

    # Create AWS authentication
    print("Creating AWS SigV4 authentication...")
    auth = create_aws_auth(api_host, API_REGION)

    # Example 1: Simple Kubernetes Job
    print("\n=== Example 1: Submit a simple Job ===")
    simple_job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "python-example-job", "namespace": "gco-jobs"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "example",
                            "image": "busybox:1.38.0",
                            "command": ["echo", "Hello from GCO Python client!"],
                        }
                    ],
                    "restartPolicy": "Never",
                }
            },
            "backoffLimit": 3,
        },
    }

    try:
        result = submit_manifests(api_endpoint, auth, [simple_job])
        print(f"Success: {json.dumps(result, indent=2)}")
    except requests.exceptions.HTTPError as e:
        print(f"Error: {e}")
        print(f"Response: {e.response.text}")

    # Example 2: GPU Job with node selector for on-demand capacity
    print("\n=== Example 2: Submit a GPU Job (on-demand) ===")
    gpu_job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "gpu-python-job", "namespace": "gco-jobs"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "gpu-example",
                            "image": "nvidia/cuda:12.0-base",
                            "command": ["nvidia-smi"],
                            "resources": {"limits": {"nvidia.com/gpu": "1"}},
                        }
                    ],
                    "restartPolicy": "Never",
                    "nodeSelector": {"karpenter.sh/capacity-type": "on-demand"},
                    "tolerations": [
                        {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
                    ],
                }
            },
            "backoffLimit": 3,
        },
    }

    print(f"GPU Job manifest: {json.dumps(gpu_job, indent=2)}")
    print("(Not submitting - uncomment to test with GPU nodes)")
    # Uncomment to submit:
    # try:
    #     result = submit_manifests(api_endpoint, auth, [gpu_job])
    #     print(f"Success: {json.dumps(result, indent=2)}")
    # except requests.exceptions.HTTPError as e:
    #     print(f"Error: {e}")
    #     print(f"Response: {e.response.text}")

    # Example 3: Multiple manifests at once
    print("\n=== Example 3: Submit multiple manifests ===")
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "python-example-config", "namespace": "gco-jobs"},
        "data": {"config.yaml": "key: value\nother: setting"},
    }

    config_reader_job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "config-reader-python-job", "namespace": "gco-jobs"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "reader",
                            "image": "busybox:1.38.0",
                            "command": ["cat", "/config/config.yaml"],
                            "volumeMounts": [{"name": "config-volume", "mountPath": "/config"}],
                        }
                    ],
                    "volumes": [
                        {"name": "config-volume", "configMap": {"name": "python-example-config"}}
                    ],
                    "restartPolicy": "Never",
                }
            },
            "backoffLimit": 3,
        },
    }

    try:
        result = submit_manifests(api_endpoint, auth, [config_map, config_reader_job])
        print(f"Success: {json.dumps(result, indent=2)}")
    except requests.exceptions.HTTPError as e:
        print(f"Error: {e}")
        print(f"Response: {e.response.text}")

    # Example 4: Dry run validation
    print("\n=== Example 4: Dry run validation ===")
    try:
        result = submit_manifests(api_endpoint, auth, [simple_job], dry_run=True)
        print(f"Dry run result: {json.dumps(result, indent=2)}")
    except requests.exceptions.HTTPError as e:
        print(f"Error: {e}")
        print(f"Response: {e.response.text}")

    print("\n=== Examples Complete ===")
    print("\nKey points:")
    print("1. The API expects 'manifests' as a list of JSON objects (not YAML strings)")
    print("2. Each manifest must include apiVersion, kind, and metadata with name")
    print("3. Use aws-requests-auth for SigV4 signing with requests library")
    print("4. Use nodeSelector 'karpenter.sh/capacity-type' to control spot vs on-demand")


if __name__ == "__main__":
    main()
