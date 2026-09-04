"""
AWS Client utilities for GCO CLI.

Provides authenticated access to AWS services with SigV4 signing,
stack discovery, and region management.
"""

import ast
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from urllib.parse import quote, urlsplit

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError
from botocore.session import get_session as get_botocore_session

from .config import GCOConfig, get_config

logger = logging.getLogger(__name__)

# HTTP status codes that are safe to retry (transient failures)
_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0  # seconds


class APIRequestError(RuntimeError):
    """HTTP API failure that preserves status for policy-aware callers."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"API request failed: {message}")
        self.status_code = status_code


class RegionalApiDiscoveryError(RuntimeError):
    """Regional endpoint discovery failed without confirming stack absence."""


def _cloudformation_stack_missing(exc: ClientError) -> bool:
    """Return whether CloudFormation authoritatively reports an absent stack."""
    error = exc.response.get("Error", {})
    return bool(
        error.get("Code") == "ValidationError"
        and "does not exist" in str(error.get("Message", "")).lower()
    )


def _aws_credential_context(session: Any) -> str:
    """Describe configured profile/role hints without claiming provider selection."""
    hints = []
    profile_name = getattr(session, "profile_name", None)
    if isinstance(profile_name, str) and profile_name.strip():
        hints.append(f"session profile {profile_name.strip()[:128]!r}")

    role_arn = os.getenv("AWS_ROLE_ARN", "").strip()
    if role_arn:
        hints.append(f"AWS_ROLE_ARN is set to {role_arn[:256]!r}")

    return "; ".join(hints) or "no profile or role hint is available"


def _safe_aws_error_message(error: dict[str, Any]) -> str:
    """Return bounded service-provided detail without terminal control bytes."""
    raw_message = error.get("Message")
    if not isinstance(raw_message, str):
        return "AWS did not return an error message"
    printable = "".join(
        character if character.isprintable() and character != "\x1b" else " "
        for character in raw_message
    )
    normalized = " ".join(printable.split())
    return normalized[:512] or "AWS did not return an error message"


def _execute_api_service_hostname(region: str) -> str:
    """Resolve the partition-correct execute-api hostname from botocore data."""
    resolver = get_botocore_session().get_component("endpoint_resolver")
    endpoint = resolver.construct_endpoint("execute-api", region)
    hostname = endpoint.get("hostname") if isinstance(endpoint, dict) else None
    if not isinstance(hostname, str):
        raise ValueError("execute-api endpoint metadata is unavailable")
    return hostname.lower()


def _normalize_regional_api_endpoint(value: Any, region: str) -> tuple[str, str]:
    """Validate a regional stack output as this region's execute-api prod URL."""
    if not isinstance(value, str):
        raise ValueError("regional endpoint output is not a string")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("regional endpoint output is not a valid URL") from exc

    host = (parsed.hostname or "").lower()
    service_hostname = _execute_api_service_hostname(region)
    host_suffix = f".{service_hostname}"
    api_id = host[: -len(host_suffix)] if host.endswith(host_suffix) else ""
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or re.fullmatch(r"[a-z0-9]+", api_id) is None
        or parsed.path.rstrip("/") != "/prod"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("regional endpoint output is not an execute-api prod URL")
    return f"https://{host}/prod", api_id


def _validate_max_attempts(max_attempts: int | None) -> None:
    """Validate an explicitly supplied request-attempt limit."""
    if max_attempts is not None and (
        isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts <= 0
    ):
        raise ValueError("max_attempts must be a positive integer")


def _decode_log_payload(payload: Any) -> str:
    """Return log text, decoding bytes and exact Python bytes-literal envelopes."""
    if isinstance(payload, str):
        if len(payload) >= 3 and payload[0] == "b" and payload[1] in {"'", '"'}:
            try:
                return cast(bytes, ast.literal_eval(payload)).decode("utf-8", errors="replace")
            except SyntaxError, ValueError:
                return payload
        return payload
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode("utf-8", errors="replace")
    raise TypeError(f"API returned an unsupported log payload: {type(payload)!r}")


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
        """Get the regional API Gateway endpoint for a specific region.

        ``None`` is reserved for CloudFormation's authoritative confirmation
        that the regional API stack does not exist. Credential, authorization,
        transport, and malformed-response failures raise instead, so callers
        never misreport an observation failure as missing infrastructure.

        Args:
            region: AWS region
            force_refresh: Force refresh from CloudFormation

        Returns:
            ApiEndpoint with URL and metadata, or None when CloudFormation
            confirms the regional API stack is absent

        Raises:
            RegionalApiDiscoveryError: If discovery cannot run or the existing
                stack does not publish a usable endpoint
        """
        if not force_refresh and region in self._regional_api_cache and self._is_cache_valid():
            return self._regional_api_cache[region]

        stack_name = f"{self.config.project_name}-regional-api-{region}"
        credential_context = _aws_credential_context(self._session)

        try:
            cfn = self._session.client("cloudformation", region_name=region)
            response = cfn.describe_stacks(StackName=stack_name)
        except ClientError as exc:
            if _cloudformation_stack_missing(exc):
                return None
            error = exc.response.get("Error", {})
            raw_error_code = error.get("Code")
            error_code = (
                raw_error_code
                if isinstance(raw_error_code, str)
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", raw_error_code)
                else "ClientError"
            )
            error_message = _safe_aws_error_message(error)
            raise RegionalApiDiscoveryError(
                f"Regional API endpoint discovery could not run for stack '{stack_name}' "
                f"in {region}. Credential context: {credential_context}. "
                f"AWS error {error_code}: {error_message}"
            ) from exc
        except Exception as exc:
            failure_type = re.sub(r"[^A-Za-z0-9_.-]", "", type(exc).__name__)[:128]
            raise RegionalApiDiscoveryError(
                f"Regional API endpoint discovery could not run for stack '{stack_name}' "
                f"in {region}. Credential context: {credential_context}. Discovery failed "
                f"with {failure_type or 'UnexpectedError'}; check AWS credential and network "
                "configuration"
            ) from exc

        if not isinstance(response, dict):
            raise RegionalApiDiscoveryError(
                f"Regional API endpoint discovery returned an invalid CloudFormation "
                f"response for stack '{stack_name}' in {region}"
            )
        stacks = response.get("Stacks")
        if not isinstance(stacks, list) or len(stacks) != 1 or not isinstance(stacks[0], dict):
            raise RegionalApiDiscoveryError(
                f"Regional API endpoint discovery returned no unique usable stack record for "
                f"'{stack_name}' in {region}"
            )
        stack = stacks[0]

        outputs = stack.get("Outputs", [])
        if not isinstance(outputs, list):
            raise RegionalApiDiscoveryError(
                f"Regional API endpoint discovery returned malformed CloudFormation Outputs "
                f"for stack '{stack_name}' in {region}"
            )

        endpoint_output: Any = None
        endpoint_output_found = False
        for output in outputs:
            if not isinstance(output, dict) or not isinstance(output.get("OutputKey"), str):
                raise RegionalApiDiscoveryError(
                    f"Regional API endpoint discovery returned malformed CloudFormation "
                    f"Outputs for stack '{stack_name}' in {region}"
                )
            if output["OutputKey"] == "RegionalApiEndpoint":
                endpoint_output_found = True
                endpoint_output = output.get("OutputValue")
                break

        if not endpoint_output_found:
            raise RegionalApiDiscoveryError(
                f"Regional API stack '{stack_name}' exists in {region} but does not publish "
                "a RegionalApiEndpoint output; the bridge deployment may be incomplete"
            )

        try:
            api_url, api_id = _normalize_regional_api_endpoint(endpoint_output, region)
        except Exception as exc:
            raise RegionalApiDiscoveryError(
                f"Regional API stack '{stack_name}' in {region} publishes an invalid "
                "RegionalApiEndpoint output"
            ) from exc

        endpoint = ApiEndpoint(url=api_url, region=region, api_id=api_id, is_regional=True)
        self._regional_api_cache[region] = endpoint
        return endpoint

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
        *,
        max_attempts: int | None = None,
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
            max_attempts: Maximum attempts for read-only requests. Mutating
                requests always make exactly one attempt. Defaults to the
                existing retry limit.

        Returns:
            JSON response as dictionary

        Raises:
            RuntimeError: If the request fails with a descriptive error message
            ValueError: If max_attempts is not a positive integer
        """
        _validate_max_attempts(max_attempts)

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
            max_attempts=max_attempts,
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
            raise APIRequestError(response.status_code, str(error_msg))

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
        *,
        max_attempts: int | None = None,
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
            max_attempts: Maximum attempts for read-only requests. Mutating
                requests always make exactly one attempt. Defaults to the
                existing retry limit.

        Returns:
            requests.Response object

        Raises:
            ValueError: If max_attempts is not a positive integer
        """
        _validate_max_attempts(max_attempts)

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
        retried_auth = False
        attempt_limit = (
            (max_attempts if max_attempts is not None else _MAX_RETRIES) if retryable_method else 1
        )
        attempt = 0
        while True:
            response = requests.request(
                method=method,
                url=url,
                headers=dict(aws_request.headers),
                data=body_str,
                timeout=(10, 310) if stream else 30,
                stream=stream,
            )

            # A read-only 403 may mean an expired SigV4 signature. Refresh and
            # retry once; mutating requests are never replayed automatically.
            if (
                response.status_code == 403
                and retryable_method
                and not retried_auth
                and attempt < attempt_limit - 1
            ):
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
                attempt += 1
                continue

            if response.status_code not in _RETRYABLE_STATUS_CODES or not retryable_method:
                return response
            if attempt == attempt_limit - 1:
                return response

            # Retryable read-only error — close before backoff, then re-sign.
            wait_time = _RETRY_BACKOFF_BASE * (2**attempt)
            logger.warning(
                "Request to %s returned %d, retrying in %.1fs (attempt %d/%d)",
                path,
                response.status_code,
                wait_time,
                attempt + 1,
                attempt_limit,
            )
            response.close()
            time.sleep(wait_time)

            # Re-sign the request for the retry (credentials/time may have changed).
            aws_request = AWSRequest(method=method, url=url, headers=request_headers, data=body_str)
            credentials = self._session.get_credentials()
            if credentials is None:
                return response
            SigV4Auth(credentials, "execute-api", endpoint.region).add_auth(aws_request)
            attempt += 1

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

        return _decode_log_payload(response.json().get("logs", ""))

    def delete_job(
        self,
        job_name: str,
        namespace: str,
        region: str | None = None,
        expected_uid: str | None = None,
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
        path = f"/api/v1/jobs/{quote(namespace, safe='')}/{quote(job_name, safe='')}"
        if expected_uid is not None:
            path += f"?expected_uid={quote(expected_uid, safe='')}"
        response = self.make_authenticated_request(
            method="DELETE",
            path=path,
            target_region=region,
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
        if "logs" in result:
            result["logs"] = _decode_log_payload(result["logs"])
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

    def get_job_validation_policy(self, region: str) -> dict[str, Any]:
        """
        Get the job validation policy a region actually enforces.

        Reads the deployed manifest processor's live configuration, not a
        local ``cdk.json`` — the two can diverge whenever a stack was deployed
        from a different checkout, and CDK augments ``trusted_registries``
        with the project's own ECR hostnames at synth time.

        Args:
            region: Target region

        Returns:
            The region's effective policy plus its live namespace
            ResourceQuota / LimitRange ceilings
        """
        response = self.make_authenticated_request(
            method="GET", path="/api/v1/policy", target_region=region
        )

        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result


def get_aws_client(config: GCOConfig | None = None) -> GCOAWSClient:
    """Get a configured AWS client instance."""
    return GCOAWSClient(config)
