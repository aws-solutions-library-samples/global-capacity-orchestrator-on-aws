"""
AWS Client utilities for GCO CLI.

Provides authenticated access to AWS services with SigV4 signing,
stack discovery, and region management.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from .config import GCOConfig, get_config

logger = logging.getLogger(__name__)

# HTTP status codes that are safe to retry (transient failures)
_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0  # seconds


@dataclass
class RegionalStack:
    """Information about a regional GCO stack."""

    region: str
    stack_name: str
    cluster_name: str
    status: str
    api_endpoint: str | None = None
    efs_file_system_id: str | None = None
    fsx_file_system_id: str | None = None
    created_time: datetime | None = None


@dataclass
class ApiEndpoint:
    """API Gateway endpoint information."""

    url: str
    region: str
    api_id: str
    is_regional: bool = False  # True if this is a regional API (for private access)


class GCOAWSClient:
    """
    AWS client for GCO operations.

    Handles:
    - Stack discovery across regions
    - Authenticated API requests with SigV4
    - CloudFormation stack queries
    - EKS cluster information
    """

    def __init__(self, config: GCOConfig | None = None):
        self.config = config or get_config()
        self._session = boto3.Session()
        self._api_endpoint_cache: ApiEndpoint | None = None
        self._regional_api_cache: dict[str, ApiEndpoint] = {}
        self._regional_stacks_cache: dict[str, RegionalStack] | None = None
        self._cache_timestamp: float | None = None
        self._use_regional_api = getattr(self.config, "use_regional_api", False) is True

    def _is_cache_valid(self) -> bool:
        """Check if cache is still valid."""
        if self._cache_timestamp is None:
            return False
        return (time.time() - self._cache_timestamp) < self.config.cache_ttl_seconds

    def _invalidate_cache(self) -> None:
        """Invalidate all caches."""
        self._api_endpoint_cache = None
        self._regional_api_cache = {}
        self._regional_stacks_cache = None
        self._cache_timestamp = None

    def set_use_regional_api(self, use_regional: bool) -> None:
        """Set whether to use regional APIs instead of global API.

        When enabled, API calls will be routed through regional API Gateways
        that use VPC Lambdas to access internal ALBs. This is required when
        public access is disabled.

        Args:
            use_regional: True to use regional APIs, False for global API
        """
        self._use_regional_api = use_regional

    def get_regional_api_endpoint(
        self, region: str, force_refresh: bool = False
    ) -> ApiEndpoint | None:
        """
        Get the regional API Gateway endpoint for a specific region.

        Regional APIs are used when public access is disabled and the ALB
        is internal-only.

        Args:
            region: AWS region
            force_refresh: Force refresh from CloudFormation

        Returns:
            ApiEndpoint with URL and metadata, or None if not found
        """
        if not force_refresh and region in self._regional_api_cache and self._is_cache_valid():
            return self._regional_api_cache[region]

        cfn = self._session.client("cloudformation", region_name=region)
        stack_name = f"{self.config.project_name}-regional-api-{region}"

        try:
            response = cfn.describe_stacks(StackName=stack_name)
            stack = response["Stacks"][0]

            api_url = None
            for output in stack.get("Outputs", []):
                if output["OutputKey"] == "RegionalApiEndpoint":
                    api_url = output["OutputValue"].rstrip("/")
                    break

            if not api_url:
                return None

            # Extract API ID from URL
            api_id = api_url.split(".")[0].replace("https://", "")

            endpoint = ApiEndpoint(url=api_url, region=region, api_id=api_id, is_regional=True)
            self._regional_api_cache[region] = endpoint
            return endpoint

        except cfn.exceptions.ClientError:
            # Stack doesn't exist
            return None
        except Exception as e:
            logger.debug("Failed to get regional API endpoint for %s: %s", region, e)
            return None

    def get_api_endpoint(self, force_refresh: bool = False) -> ApiEndpoint:
        """
        Get the global API Gateway endpoint.

        Args:
            force_refresh: Force refresh from CloudFormation

        Returns:
            ApiEndpoint with URL and metadata
        """
        if not force_refresh and self._api_endpoint_cache and self._is_cache_valid():
            return self._api_endpoint_cache

        cfn = self._session.client("cloudformation", region_name=self.config.api_gateway_region)

        try:
            response = cfn.describe_stacks(StackName=self.config.api_gateway_stack_name)
            stack = response["Stacks"][0]

            api_url = None
            for output in stack.get("Outputs", []):
                if output["OutputKey"] == "ApiEndpoint":
                    api_url = output["OutputValue"].rstrip("/")
                    break

            if not api_url:
                raise ValueError(
                    f"ApiEndpoint not found in stack {self.config.api_gateway_stack_name}"
                )

            # Extract API ID from URL
            # Format: https://{api-id}.execute-api.{region}.amazonaws.com/prod
            api_id = api_url.split(".")[0].replace("https://", "")

            self._api_endpoint_cache = ApiEndpoint(
                url=api_url, region=self.config.api_gateway_region, api_id=api_id
            )
            self._cache_timestamp = time.time()

            return self._api_endpoint_cache

        except Exception as e:
            raise RuntimeError(f"Failed to get API endpoint: {e}") from e

    def discover_regional_stacks(self, force_refresh: bool = False) -> dict[str, RegionalStack]:
        """
        Discover all regional GCO stacks.

        Checks configured regions from cdk.json first for fast discovery,
        then falls back to scanning all AWS regions if no stacks are found.

        Args:
            force_refresh: Force refresh from CloudFormation

        Returns:
            Dictionary mapping region to RegionalStack
        """
        if not force_refresh and self._regional_stacks_cache and self._is_cache_valid():
            return self._regional_stacks_cache

        regional_stacks: dict[str, RegionalStack] = {}

        # Try configured regions first (fast path)
        configured_regions = self._get_configured_regions()
        if configured_regions:
            for region in configured_regions:
                stack = self._probe_regional_stack(region)
                if stack:
                    regional_stacks[region] = stack

        # If we found stacks in configured regions, skip the full scan
        if not regional_stacks:
            # Fall back to scanning all regions
            logger.debug("No stacks found in configured regions, scanning all AWS regions")
            ec2 = self._session.client("ec2", region_name="us-east-1")
            regions_response = ec2.describe_regions()
            all_regions = [r["RegionName"] for r in regions_response["Regions"]]

            for region in all_regions:
                if region in configured_regions:
                    continue  # Already checked
                stack = self._probe_regional_stack(region)
                if stack:
                    regional_stacks[region] = stack

        self._regional_stacks_cache = regional_stacks
        self._cache_timestamp = time.time()

        return regional_stacks

    def _get_configured_regions(self) -> list[str]:
        """Get the list of configured deployment regions from cdk.json."""
        from .config import _load_cdk_json

        cdk_regions = _load_cdk_json()
        regions: list[str] = cdk_regions.get("regional", [])
        return regions

    def _probe_regional_stack(self, region: str) -> RegionalStack | None:
        """Probe a single region for a GCO regional stack.

        Args:
            region: AWS region to check

        Returns:
            RegionalStack if found, None otherwise
        """
        try:
            cfn = self._session.client("cloudformation", region_name=region)
            stack_name = f"{self.config.regional_stack_prefix}-{region}"

            try:
                response = cfn.describe_stacks(StackName=stack_name)
                stack = response["Stacks"][0]

                outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}

                return RegionalStack(
                    region=region,
                    stack_name=stack_name,
                    cluster_name=outputs.get("ClusterName", f"{self.config.project_name}-{region}"),
                    status=stack["StackStatus"],
                    efs_file_system_id=outputs.get("EfsFileSystemId"),
                    fsx_file_system_id=outputs.get("FsxFileSystemId"),
                    created_time=stack.get("CreationTime"),
                )
            except cfn.exceptions.ClientError:
                return None

        except Exception as e:
            logger.debug("Failed to get regional stack info for %s: %s", region, e)
            return None

    def get_regional_stack(self, region: str) -> RegionalStack | None:
        """Get information about a specific regional stack."""
        stacks = self.discover_regional_stacks()
        return stacks.get(region)

    def call_api(
        self,
        method: str,
        path: str,
        region: str | None = None,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Make an API call and return the JSON response.

        This is a convenience wrapper around make_authenticated_request.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            path: API path (e.g., /api/v1/templates)
            region: Target region for the request
            body: Request body (will be JSON encoded)
            params: Query parameters

        Returns:
            JSON response as dictionary

        Raises:
            RuntimeError: If the request fails with a descriptive error message
        """
        # Add URL-encoded query parameters to path
        if params:
            encoded_pairs = [
                f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
                for k, v in params.items()
                if v is not None
            ]
            if encoded_pairs:
                path = f"{path}?{'&'.join(encoded_pairs)}"

        response = self.make_authenticated_request(
            method=method,
            path=path,
            body=body,
            target_region=region,
        )

        if not response.ok:
            error_msg = f"{response.status_code} {response.reason}"
            try:
                error_data = response.json()
                if "error" in error_data:
                    error_msg = error_data["error"]
                elif "message" in error_data:
                    error_msg = error_data["message"]
                elif "detail" in error_data:
                    error_msg = error_data["detail"]
            except json.JSONDecodeError, KeyError:
                error_msg = response.text or error_msg
            raise RuntimeError(f"API request failed: {error_msg}")

        result: dict[str, Any] = response.json()
        return result

    def make_authenticated_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        target_region: str | None = None,
        stream: bool = False,
    ) -> requests.Response:
        """
        Make an authenticated request to the GCO API.

        Requests with ``target_region`` always use that region's API Gateway so
        exact region pinning is enforced without sending routing headers through
        the global endpoint. Unpinned requests use the global API unless regional
        mode is enabled, in which case they use ``config.default_region``. Global
        aggregation paths are unavailable in regional mode. Missing regional
        endpoints fail closed instead of silently using the global API.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: API path (e.g., /api/v1/manifests)
            body: Request body (will be JSON encoded)
            headers: Additional headers
            target_region: Exact region for the request. When set, the request
                uses that region's API Gateway directly.
            stream: Leave the response body unbuffered for incremental consumption.

        Returns:
            requests.Response object
        """
        # Global aggregation endpoints exist only on the global API. Regional
        # mode must reject them clearly rather than send a global path to a
        # regional bridge and surface an opaque 404.
        if self._use_regional_api and (
            path == "/api/v1/global" or path.startswith("/api/v1/global/")
        ):
            raise ValueError("Global API operations are unavailable in regional API mode")

        # Strict regional mode has no global fallback. Resolve an omitted
        # optional ``--region`` to the configured default, then require a real
        # non-blank Region before attempting endpoint discovery. Keep this as a
        # separate branch so ``get_api_endpoint`` is unreachable in strict mode.
        if self._use_regional_api:
            effective_region = (
                target_region if target_region is not None else self.config.default_region
            )
            if not isinstance(effective_region, str) or not effective_region.strip():
                raise ValueError(
                    "Regional API mode requires a non-empty target or default AWS region"
                )
            target_region = effective_region.strip()
            endpoint = self.get_regional_api_endpoint(target_region)
        elif target_region:
            # Exact region pinning always uses the regional API. The global
            # proxy is intentionally not VPC-attached and rejects
            # X-GCO-Target-Region, so it cannot honor a pin without pretending
            # success or weakening isolation.
            endpoint = self.get_regional_api_endpoint(target_region)
        else:
            endpoint = self.get_api_endpoint()

        if endpoint is None:
            # Only regional discovery returns None; the global endpoint helper
            # either returns an endpoint or raises its own actionable error.
            assert target_region is not None
            raise RuntimeError(
                f"Regional API endpoint is not deployed in {target_region}; "
                "exact region routing requires the regional API bridge"
            )

        url = f"{endpoint.url}{path}"

        # Normalize the method once. Only read-only operations are eligible
        # for automatic replay; retrying POST/PUT/PATCH/DELETE can duplicate a
        # model invocation or state transition after an ambiguous response.
        method = method.upper()
        retryable_method = method in {"GET", "HEAD", "OPTIONS"}

        # Prepare headers without mutating the caller's mapping.
        request_headers = dict(headers or {})
        request_headers["Content-Type"] = "application/json"

        # Prepare body
        body_str = json.dumps(body) if body is not None else ""

        # Create AWS request for signing
        aws_request = AWSRequest(method=method, url=url, headers=request_headers, data=body_str)

        # Sign the request with the endpoint's region
        credentials = self._session.get_credentials()
        if credentials is None:
            raise RuntimeError(
                "No AWS credentials found. Configure credentials via environment variables, "
                "~/.aws/credentials, IAM role, or SSO (aws sso login)."
            )
        SigV4Auth(credentials, "execute-api", endpoint.region).add_auth(aws_request)

        # Read-only requests retry transient failures and may refresh expired
        # SigV4 credentials once. Mutating requests receive exactly one network
        # attempt and return its response unchanged.
        last_response = None
        retried_auth = False
        max_attempts = _MAX_RETRIES if retryable_method else 1
        for attempt in range(max_attempts):
            response = requests.request(
                method=method,
                url=url,
                headers=dict(aws_request.headers),
                data=body_str,
                timeout=(10, 310) if stream else 30,
                stream=stream,
            )
            last_response = response

            # A read-only 403 may mean an expired SigV4 signature. Refresh and
            # retry once; mutating requests are never replayed automatically.
            if response.status_code == 403 and retryable_method and not retried_auth:
                retried_auth = True
                logger.warning(
                    "Request to %s returned 403, refreshing credentials and retrying",
                    path,
                )
                # Force a new session to pick up refreshed credentials
                self._session = boto3.Session()
                aws_request = AWSRequest(
                    method=method, url=url, headers=request_headers, data=body_str
                )
                credentials = self._session.get_credentials()
                if credentials is None:
                    return response  # No credentials available, return the 403
                SigV4Auth(credentials, "execute-api", endpoint.region).add_auth(aws_request)
                response.close()
                continue

            if response.status_code not in _RETRYABLE_STATUS_CODES or not retryable_method:
                return response

            # Retryable read-only error — back off and retry.
            if attempt < max_attempts - 1:
                wait_time = _RETRY_BACKOFF_BASE * (2**attempt)
                logger.warning(
                    "Request to %s returned %d, retrying in %.1fs (attempt %d/%d)",
                    path,
                    response.status_code,
                    wait_time,
                    attempt + 1,
                    max_attempts,
                )
                response.close()
                time.sleep(wait_time)

                # Re-sign the request for the retry (credentials/time may have changed)
                aws_request = AWSRequest(
                    method=method, url=url, headers=request_headers, data=body_str
                )
                credentials = self._session.get_credentials()
                if credentials is None:
                    return last_response
                SigV4Auth(credentials, "execute-api", endpoint.region).add_auth(aws_request)

        # All retries exhausted — return the last response
        return last_response  # type: ignore[return-value]

    def submit_manifests(
        self,
        manifests: list[dict[str, Any]],
        namespace: str | None = None,
        target_region: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Submit manifests to the GCO API.

        Args:
            manifests: List of Kubernetes manifest dictionaries
            namespace: Default namespace for manifests
            target_region: Target region for job execution
            dry_run: If True, validate without applying

        Returns:
            API response dictionary

        Raises:
            RuntimeError: If submission fails with descriptive error message
        """
        body = {"manifests": manifests, "dry_run": dry_run}

        if namespace:
            body["namespace"] = namespace

        response = self.make_authenticated_request(
            method="POST", path="/api/v1/manifests", body=body, target_region=target_region
        )

        # Parse response and provide descriptive error messages
        if not response.ok:
            error_msg = f"{response.status_code} {response.reason}"
            try:
                error_data = response.json()
                # Extract meaningful error details from the response
                if "resources" in error_data:
                    failed = [r for r in error_data["resources"] if r.get("status") == "failed"]
                    if failed:
                        messages = [
                            f"{r.get('name')}: {r.get('message', 'Unknown error')}" for r in failed
                        ]
                        error_msg = "; ".join(messages)
                elif "error" in error_data:
                    error_msg = error_data["error"]
                elif "message" in error_data:
                    error_msg = error_data["message"]
            except json.JSONDecodeError, KeyError:
                error_msg = response.text or error_msg
            raise RuntimeError(error_msg)

        result: dict[str, Any] = response.json()
        return result

    def get_jobs(
        self,
        region: str | None = None,
        namespace: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get jobs from GCO clusters.

        Args:
            region: Specific region to query (None for all regions)
            namespace: Filter by namespace
            status: Filter by status (running, completed, failed)

        Returns:
            List of job information dictionaries
        """
        params = []
        if namespace:
            params.append(f"namespace={namespace}")
        if status:
            params.append(f"status={status}")

        query_string = f"?{'&'.join(params)}" if params else ""

        response = self.make_authenticated_request(
            method="GET", path=f"/api/v1/jobs{query_string}", target_region=region
        )

        response.raise_for_status()
        result: list[dict[str, Any]] = response.json()
        return result

    def get_job_details(
        self, job_name: str, namespace: str, region: str | None = None
    ) -> dict[str, Any]:
        """
        Get detailed information about a specific job.

        Args:
            job_name: Name of the job
            namespace: Namespace of the job
            region: Region where the job is running

        Returns:
            Job details dictionary
        """
        response = self.make_authenticated_request(
            method="GET", path=f"/api/v1/jobs/{namespace}/{job_name}", target_region=region
        )

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_job_logs(
        self, job_name: str, namespace: str, region: str | None = None, tail_lines: int = 100
    ) -> str:
        """
        Get logs from a job.

        Args:
            job_name: Name of the job
            namespace: Namespace of the job
            region: Region where the job is running
            tail_lines: Number of lines to return from the end

        Returns:
            Log content as string
        """
        response = self.make_authenticated_request(
            method="GET",
            path=f"/api/v1/jobs/{namespace}/{job_name}/logs?tail={tail_lines}",
            target_region=region,
        )

        if not response.ok:
            # Try to extract a useful error message from the response body
            try:
                error_data = response.json()
                detail = error_data.get("detail", response.reason)
            except Exception:
                detail = response.text or response.reason
            raise RuntimeError(detail)

        return str(response.json().get("logs", ""))

    def delete_job(
        self, job_name: str, namespace: str, region: str | None = None
    ) -> dict[str, Any]:
        """
        Delete a job.

        Args:
            job_name: Name of the job
            namespace: Namespace of the job
            region: Region where the job is running

        Returns:
            Deletion result dictionary
        """
        response = self.make_authenticated_request(
            method="DELETE", path=f"/api/v1/jobs/{namespace}/{job_name}", target_region=region
        )

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_regional_alb_endpoint(self, region: str) -> str | None:
        """
        Get the ALB endpoint for a specific region.

        Args:
            region: AWS region

        Returns:
            ALB DNS name or None if not found
        """
        stack = self.get_regional_stack(region)
        if not stack:
            return None

        cfn = self._session.client("cloudformation", region_name=region)
        try:
            response = cfn.describe_stacks(StackName=stack.stack_name)
            stack_data = response["Stacks"][0]
            outputs = {o["OutputKey"]: o["OutputValue"] for o in stack_data.get("Outputs", [])}
            return outputs.get("AlbDnsName") or outputs.get("LoadBalancerDnsName")
        except Exception as e:
            logger.debug("Failed to get ALB DNS for %s: %s", region, e)
            return None

    # =========================================================================
    # Global Aggregation Methods (Cross-Region)
    # =========================================================================

    def get_global_jobs(
        self,
        namespace: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        Get jobs across all regions via the global aggregation API.

        Args:
            namespace: Filter by namespace
            status: Filter by status
            limit: Maximum jobs to return

        Returns:
            Aggregated job list with region information
        """
        params = [f"limit={limit}"]
        if namespace:
            params.append(f"namespace={namespace}")
        if status:
            params.append(f"status={status}")

        query_string = f"?{'&'.join(params)}"

        response = self.make_authenticated_request(
            method="GET", path=f"/api/v1/global/jobs{query_string}"
        )

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_global_health(self) -> dict[str, Any]:
        """
        Get health status across all regions.

        Returns:
            Aggregated health status from all regional clusters
        """
        response = self.make_authenticated_request(method="GET", path="/api/v1/global/health")

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_global_status(self) -> dict[str, Any]:
        """
        Get cluster status across all regions.

        Returns:
            Aggregated status from all regional clusters
        """
        response = self.make_authenticated_request(method="GET", path="/api/v1/global/status")

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def bulk_delete_global(
        self,
        namespace: str | None = None,
        status: str | None = None,
        older_than_days: int | None = None,
        label_selector: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Bulk delete jobs across all regions.

        Args:
            namespace: Filter by namespace
            status: Filter by status
            older_than_days: Delete jobs older than N days
            label_selector: Kubernetes label selector
            dry_run: If True, only return what would be deleted

        Returns:
            Deletion results from all regions
        """
        body: dict[str, Any] = {"dry_run": dry_run}
        if namespace:
            body["namespace"] = namespace
        if status:
            body["status"] = status
        if older_than_days:
            body["older_than_days"] = older_than_days
        if label_selector:
            body["label_selector"] = label_selector

        response = self.make_authenticated_request(
            method="DELETE", path="/api/v1/global/jobs", body=body
        )

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    # =========================================================================
    # Regional Job Operations (New API Endpoints)
    # =========================================================================

    def get_job_events(self, job_name: str, namespace: str, region: str) -> dict[str, Any]:
        """
        Get Kubernetes events for a job.

        Args:
            job_name: Name of the job
            namespace: Namespace of the job
            region: Region where the job is running

        Returns:
            Events related to the job
        """
        response = self.make_authenticated_request(
            method="GET",
            path=f"/api/v1/jobs/{namespace}/{job_name}/events",
            target_region=region,
        )

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_job_pods(self, job_name: str, namespace: str, region: str) -> dict[str, Any]:
        """
        Get pods for a job.

        Args:
            job_name: Name of the job
            namespace: Namespace of the job
            region: Region where the job is running

        Returns:
            Pod details for the job
        """
        response = self.make_authenticated_request(
            method="GET",
            path=f"/api/v1/jobs/{namespace}/{job_name}/pods",
            target_region=region,
        )

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_pod_logs(
        self,
        job_name: str,
        pod_name: str,
        namespace: str,
        region: str,
        tail_lines: int = 100,
        container: str | None = None,
    ) -> dict[str, Any]:
        """
        Get logs from a specific pod of a job.

        Args:
            job_name: Name of the job
            pod_name: Name of the pod
            namespace: Namespace of the job
            region: Region where the job is running
            tail_lines: Number of lines to return from the end
            container: Container name (for multi-container pods)

        Returns:
            Pod logs response
        """
        params = [f"tail={tail_lines}"]
        if container:
            params.append(f"container={container}")

        query_string = f"?{'&'.join(params)}"

        response = self.make_authenticated_request(
            method="GET",
            path=f"/api/v1/jobs/{namespace}/{job_name}/pods/{pod_name}/logs{query_string}",
            target_region=region,
        )

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_job_metrics(self, job_name: str, namespace: str, region: str) -> dict[str, Any]:
        """
        Get resource metrics for a job.

        Args:
            job_name: Name of the job
            namespace: Namespace of the job
            region: Region where the job is running

        Returns:
            Resource usage metrics for the job's pods
        """
        response = self.make_authenticated_request(
            method="GET",
            path=f"/api/v1/jobs/{namespace}/{job_name}/metrics",
            target_region=region,
        )

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def retry_job(self, job_name: str, namespace: str, region: str) -> dict[str, Any]:
        """
        Retry a failed job.

        Creates a new job from the failed job's spec with a new name.

        Args:
            job_name: Name of the failed job
            namespace: Namespace of the job
            region: Region where the job is running

        Returns:
            Result with new job name
        """
        response = self.make_authenticated_request(
            method="POST",
            path=f"/api/v1/jobs/{namespace}/{job_name}/retry",
            target_region=region,
        )

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def bulk_delete_jobs(
        self,
        namespace: str | None = None,
        status: str | None = None,
        older_than_days: int | None = None,
        label_selector: str | None = None,
        region: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Bulk delete jobs in a region.

        Args:
            namespace: Filter by namespace
            status: Filter by status
            older_than_days: Delete jobs older than N days
            label_selector: Kubernetes label selector
            region: Target region
            dry_run: If True, only return what would be deleted

        Returns:
            Deletion results
        """
        body: dict[str, Any] = {"dry_run": dry_run}
        if namespace:
            body["namespace"] = namespace
        if status:
            body["status"] = status
        if older_than_days:
            body["older_than_days"] = older_than_days
        if label_selector:
            body["label_selector"] = label_selector

        response = self.make_authenticated_request(
            method="DELETE", path="/api/v1/jobs", body=body, target_region=region
        )

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_health(self, region: str) -> dict[str, Any]:
        """
        Get health status for a specific region.

        Args:
            region: Target region

        Returns:
            Health status for the regional cluster
        """
        response = self.make_authenticated_request(
            method="GET", path="/api/v1/health", target_region=region
        )

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result


def get_aws_client(config: GCOConfig | None = None) -> GCOAWSClient:
    """Get a configured AWS client instance."""
    return GCOAWSClient(config)
