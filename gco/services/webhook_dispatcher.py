"""
Webhook Dispatcher Service for GCO (Global Capacity Orchestrator on AWS).

This service monitors Kubernetes job status changes and dispatches webhook
notifications to registered endpoints. It runs as a background task alongside
the health monitor or as a standalone service.

Key Features:
- Watches Kubernetes jobs for status changes (started, completed, failed)
- Queries matching webhooks from DynamoDB based on event type and namespace
- Dispatches HTTP POST requests with JSON payloads
- Signs payloads with HMAC-SHA256 when a secret is configured
- Implements retry logic with exponential backoff for failed deliveries
- Publishes delivery metrics to CloudWatch

Webhook Payload Format:
    {
        "event": "job.completed",
        "timestamp": "2026-02-04T12:00:00Z",
        "cluster_id": "gco-cluster-us-east-1",
        "region": "us-east-1",
        "job": {
            "name": "my-job",
            "namespace": "gco-jobs",
            "uid": "abc-123",
            "status": "succeeded",
            "start_time": "2026-02-04T11:55:00Z",
            "completion_time": "2026-02-04T12:00:00Z",
            "succeeded": 1,
            "failed": 0
        }
    }

HMAC Signature:
    When a webhook has a secret configured, the payload is signed using
    HMAC-SHA256. The signature is included in the X-GCO-Signature header
    as "sha256=<hex_digest>".

Environment Variables:
    CLUSTER_NAME: Name of the EKS cluster
    REGION: AWS region of the cluster
    WEBHOOK_TIMEOUT: HTTP timeout for webhook calls (default: 30)
    WEBHOOK_MAX_RETRIES: Maximum total delivery attempts (default: 3)
    WEBHOOK_RETRY_DELAY: Initial retry delay in seconds (default: 5)
    WEBHOOKS_TABLE_NAME: DynamoDB table for webhooks
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import ParseResult, urlparse

import httpx
from kubernetes import client, config
from kubernetes.client.models import V1Job
from kubernetes.client.rest import ApiException
from kubernetes.watch import Watch

from gco.services.template_store import WebhookStore, get_webhook_store

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T14:42:56Z
# Generated from Git commit: 89b000378ed5a912a38c06f4feab2b029936ebcc
# Flowchart(s) generated from this file:
#   * ``WebhookDispatcher._deliver_webhook`` -> ``diagrams/code_diagrams/gco/services/webhook_dispatcher.WebhookDispatcher__deliver_webhook.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/webhook_dispatcher.WebhookDispatcher__deliver_webhook.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Networks blocked for SSRF prevention
BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


@dataclass(frozen=True)
class _ValidatedWebhookTarget:
    """One DNS-validated destination whose transport cannot resolve again."""

    original_url: str
    parsed: ParseResult
    hostname: str
    port: int
    addresses: tuple[str, ...]

    @property
    def log_identity(self) -> str:
        """Return an operator-useful identity without path/query credentials."""
        return f"host={self.hostname} port={self.port}"

    @property
    def host_header(self) -> str:
        return self.parsed.netloc

    def pinned_url(self, attempt: int) -> str:
        address = self.addresses[(attempt - 1) % len(self.addresses)]
        host = f"[{address}]" if ":" in address else address
        return self.parsed._replace(netloc=f"{host}:{self.port}").geturl()


def _globally_routable_unicast(
    value: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return a canonical public-unicast address, including mapped IPv4.

    ``is_global`` intentionally does not exclude multicast in ``ipaddress``.
    Normalize IPv4-mapped IPv6 first, then require a globally routable,
    non-multicast unicast address so loopback, private, link-local, shared,
    documentation, reserved, unspecified, and multicast destinations all fail
    closed.
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
    ):
        return None
    return address


def _resolve_webhook_target(
    url: str,
    allowed_domains: list[str] | None = None,
) -> tuple[_ValidatedWebhookTarget | None, str | None]:
    """Resolve and approve every address before any outbound connection."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None, "Only HTTPS webhook URLs are allowed"
    if parsed.username is not None or parsed.password is not None:
        return None, "Webhook URLs must not contain userinfo credentials"

    hostname = parsed.hostname
    if not hostname:
        return None, "Webhook URL must include a valid hostname"
    normalized_hostname = hostname.rstrip(".").lower()
    normalized_allowed = {value.rstrip(".").lower() for value in allowed_domains or []}
    if normalized_allowed and normalized_hostname not in normalized_allowed:
        return None, f"Domain '{hostname}' not in allowed domains list"

    try:
        port = parsed.port or 443
    except ValueError:
        return None, "Webhook URL contains an invalid port"
    try:
        resolved = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return None, f"DNS resolution failed for {hostname}"
    if not resolved:
        return None, f"DNS resolution returned no results for {hostname}"

    addresses: set[str] = set()
    for family, _type, _proto, _canonname, sockaddr in resolved:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            return None, "DNS resolution returned an unsupported address family"
        raw_address = sockaddr[0]
        if not isinstance(raw_address, str):
            return None, "DNS resolution returned a non-text IP address"
        address = _globally_routable_unicast(raw_address)
        if address is None:
            return None, (
                f"Resolved IP {raw_address} is blocked because it is not globally routable unicast"
            )
        addresses.add(str(address))
    if not addresses:
        return None, f"DNS resolution returned no usable addresses for {hostname}"

    return (
        _ValidatedWebhookTarget(
            original_url=url,
            parsed=parsed,
            hostname=hostname,
            port=port,
            addresses=tuple(sorted(addresses)),
        ),
        None,
    )


def validate_webhook_url(
    url: str, allowed_domains: list[str] | None = None
) -> tuple[bool, str | None]:
    """Validate HTTPS, allowlist, DNS, and blocked-network constraints."""
    target, error = _resolve_webhook_target(url, allowed_domains)
    return target is not None, error


class WebhookEvent(StrEnum):
    """Webhook event types."""

    JOB_STARTED = "job.started"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"


@dataclass
class WebhookDeliveryResult:
    """Result of a webhook delivery attempt."""

    webhook_id: str
    url: str
    event: str
    success: bool
    status_code: int | None = None
    error: str | None = None
    attempts: int = 1
    duration_ms: float = 0.0


@dataclass
class JobStateCache:
    """Cache of job states to detect transitions."""

    # Map of job_uid -> last known status
    job_states: dict[str, str] = field(default_factory=dict)

    def get_state(self, job_uid: str) -> str | None:
        """Get cached state for a job."""
        return self.job_states.get(job_uid)

    def set_state(self, job_uid: str, state: str) -> str | None:
        """Set state for a job, returns previous state."""
        previous = self.job_states.get(job_uid)
        self.job_states[job_uid] = state
        return previous

    def remove(self, job_uid: str) -> None:
        """Remove a job from the cache."""
        self.job_states.pop(job_uid, None)


class WebhookDispatcher:
    """
    Dispatches webhook notifications for Kubernetes job events.

    This class monitors job status changes and sends HTTP notifications
    to registered webhook endpoints.
    """

    def __init__(
        self,
        cluster_id: str,
        region: str,
        webhook_store: WebhookStore | None = None,
        timeout: int = 30,
        max_retries: int = 3,
        retry_delay: int = 5,
        namespaces: list[str] | None = None,
        allowed_domains: list[str] | None = None,
    ):
        """Initialize the webhook dispatcher.

        Args:
            cluster_id: EKS cluster identifier
            region: AWS region
            webhook_store: DynamoDB webhook store (uses singleton if None)
            timeout: HTTP timeout for webhook calls in seconds
            max_retries: Maximum total delivery attempts (initial request included)
            retry_delay: Initial retry delay in seconds (doubles each retry)
            namespaces: Namespaces to watch (None = all non-system namespaces)
            allowed_domains: Optional list of allowed webhook domains for SSRF prevention
        """
        self.cluster_id = cluster_id
        self.region = region
        self.webhook_store = webhook_store or get_webhook_store()
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.namespaces = namespaces or ["gco-jobs", "default"]
        self.allowed_domains = allowed_domains or []

        # Initialize Kubernetes client
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration")
        except config.ConfigException:
            try:
                config.load_kube_config()
                logger.info("Loaded local Kubernetes configuration")
            except config.ConfigException as e:
                logger.error(f"Failed to load Kubernetes configuration: {e}")
                raise

        self.batch_v1 = client.BatchV1Api()

        # Timeout for Kubernetes API calls (seconds)
        self._k8s_timeout = int(os.environ.get("K8S_API_TIMEOUT", "30"))

        # State tracking
        self._job_state_cache = JobStateCache()
        self._running = False
        self._watch_task: asyncio.Task[None] | None = None

        # Metrics
        self._deliveries_total = 0
        self._deliveries_success = 0
        self._deliveries_failed = 0

    def _compute_job_status(self, job: V1Job) -> str:
        """Compute the effective status of a Kubernetes job."""
        status = job.status
        conditions = status.conditions or []

        for condition in conditions:
            if condition.type == "Complete" and condition.status == "True":
                return "succeeded"
            if condition.type == "Failed" and condition.status == "True":
                return "failed"

        if (status.active or 0) > 0:
            return "running"

        if (status.succeeded or 0) > 0:
            return "succeeded"

        if (status.failed or 0) > 0:
            return "failed"

        return "pending"

    def _determine_event(
        self, previous_status: str | None, current_status: str
    ) -> WebhookEvent | None:
        """Determine which webhook event to fire based on status transition."""
        if previous_status is None:
            # New job - check if it's already running
            if current_status == "running":
                return WebhookEvent.JOB_STARTED
            return None

        # Status transitions
        if previous_status in ("pending",) and current_status == "running":
            return WebhookEvent.JOB_STARTED

        if previous_status in ("pending", "running") and current_status == "succeeded":
            return WebhookEvent.JOB_COMPLETED

        if previous_status in ("pending", "running") and current_status == "failed":
            return WebhookEvent.JOB_FAILED

        return None

    def _build_payload(self, event: WebhookEvent, job: V1Job) -> dict[str, Any]:
        """Build the webhook payload for a job event."""
        metadata = job.metadata
        status = job.status

        return {
            "event": event.value,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "cluster_id": self.cluster_id,
            "region": self.region,
            "job": {
                "name": metadata.name,
                "namespace": metadata.namespace,
                "uid": metadata.uid,
                "labels": metadata.labels or {},
                "status": self._compute_job_status(job),
                "start_time": (status.start_time.isoformat() if status.start_time else None),
                "completion_time": (
                    status.completion_time.isoformat() if status.completion_time else None
                ),
                "active": status.active or 0,
                "succeeded": status.succeeded or 0,
                "failed": status.failed or 0,
            },
        }

    def _sign_payload(self, payload: str, secret: str) -> str:
        """Sign a payload using HMAC-SHA256."""
        signature = hmac.new(
            secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"sha256={signature}"

    async def _deliver_webhook(
        self,
        webhook: dict[str, Any],
        payload: dict[str, Any],
    ) -> WebhookDeliveryResult:
        """Deliver one logical webhook with bounded, address-pinned attempts."""
        raw_webhook_id = webhook.get("id")
        webhook_id = raw_webhook_id if isinstance(raw_webhook_id, str) else "<unknown>"
        raw_url = webhook.get("url")
        url = raw_url if isinstance(raw_url, str) else ""
        raw_event = payload.get("event")
        event = raw_event if isinstance(raw_event, str) else "unknown"
        secret = webhook.get("secret")
        start_time = datetime.now(UTC)
        self._deliveries_total += 1

        target: _ValidatedWebhookTarget | None = None
        attempts = 0
        last_error: str | None = None
        last_status_code: int | None = None
        successful_status_code: int | None = None
        validation_error: str | None = None
        try:
            if not isinstance(raw_url, str):
                validation_error = "Webhook URL must be a string"
            else:
                target, validation_error = _resolve_webhook_target(
                    raw_url, self.allowed_domains or None
                )
            if target is None:
                last_error = f"URL validation failed: {validation_error}"
                logger.warning(
                    "Webhook URL validation failed: webhook_id=%s error=%s",
                    webhook_id,
                    validation_error,
                )
            else:
                payload_json = json.dumps(payload)
                headers = {
                    "Content-Type": "application/json",
                    "Host": target.host_header,
                    "User-Agent": f"GCO-Webhook/{self.cluster_id}",
                    "X-GCO-Event": event,
                    "X-GCO-Cluster": self.cluster_id,
                    "X-GCO-Region": self.region,
                }
                if secret:
                    headers["X-GCO-Signature"] = self._sign_payload(payload_json, secret)

                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    while attempts < self.max_retries:
                        attempts += 1
                        try:
                            response = await client.post(
                                target.pinned_url(attempts),
                                content=payload_json,
                                headers=headers,
                                extensions={"sni_hostname": target.hostname},
                            )
                            last_status_code = response.status_code
                            if 200 <= response.status_code < 300:
                                successful_status_code = response.status_code
                                break

                            last_error = f"HTTP {response.status_code}"
                            if response.status_code >= 500:
                                logger.warning(
                                    "Webhook attempt failed: webhook_id=%s %s status=%s attempt=%s",
                                    webhook_id,
                                    target.log_identity,
                                    response.status_code,
                                    attempts,
                                )
                                if attempts < self.max_retries:
                                    await asyncio.sleep(self.retry_delay * (2 ** (attempts - 1)))
                                continue
                            break  # Caller errors are terminal and must not be replayed.

                        except httpx.TimeoutException:
                            last_error = "Request timed out"
                            logger.warning(
                                "Webhook attempt timed out: webhook_id=%s %s attempt=%s",
                                webhook_id,
                                target.log_identity,
                                attempts,
                            )
                            if attempts < self.max_retries:
                                await asyncio.sleep(self.retry_delay * (2 ** (attempts - 1)))
                        except httpx.RequestError as exc:
                            last_error = type(exc).__name__
                            logger.warning(
                                "Webhook transport failed: webhook_id=%s %s "
                                "error_type=%s attempt=%s",
                                webhook_id,
                                target.log_identity,
                                type(exc).__name__,
                                attempts,
                            )
                            if attempts < self.max_retries:
                                await asyncio.sleep(self.retry_delay * (2 ** (attempts - 1)))

                # Exiting AsyncClient may itself await and be cancelled. Commit
                # success only after teardown completes so one delivery has
                # exactly one terminal metric classification.
                if successful_status_code is not None:
                    duration = (datetime.now(UTC) - start_time).total_seconds() * 1000
                    logger.info(
                        "Webhook delivered: webhook_id=%s %s status=%s attempts=%s",
                        webhook_id,
                        target.log_identity,
                        successful_status_code,
                        attempts,
                    )
                    self._deliveries_success += 1
                    return WebhookDeliveryResult(
                        webhook_id=webhook_id,
                        url=url,
                        event=event,
                        success=True,
                        status_code=successful_status_code,
                        attempts=attempts,
                        duration_ms=duration,
                    )
        except asyncio.CancelledError:
            # Cancellation is a terminal outcome for this logical delivery.
            # Preserve task cancellation while keeping total == success + failed.
            self._deliveries_failed += 1
            logger.info("Webhook delivery cancelled: webhook_id=%s", webhook_id)
            raise
        except Exception as exc:  # noqa: BLE001 — sanitize the logical delivery boundary
            last_error = f"Delivery failure: {type(exc).__name__}"
            logger.error(
                "Webhook delivery raised: webhook_id=%s error_type=%s",
                webhook_id,
                type(exc).__name__,
            )

        duration = (datetime.now(UTC) - start_time).total_seconds() * 1000
        log_identity = target.log_identity if target is not None else "target=unvalidated"
        logger.error(
            "Webhook delivery failed: webhook_id=%s %s attempts=%s status=%s error=%s",
            webhook_id,
            log_identity,
            attempts,
            last_status_code,
            last_error,
        )
        self._deliveries_failed += 1
        return WebhookDeliveryResult(
            webhook_id=webhook_id,
            url=url,
            event=event,
            success=False,
            status_code=last_status_code,
            error=last_error,
            attempts=attempts,
            duration_ms=duration,
        )

    async def _dispatch_event(self, event: WebhookEvent, job: V1Job) -> list[WebhookDeliveryResult]:
        """Dispatch webhooks for a job event."""
        namespace = job.metadata.namespace
        payload = self._build_payload(event, job)

        # Get webhooks subscribed to this event
        try:
            # Get webhooks for this specific namespace
            namespace_webhooks = self.webhook_store.get_webhooks_for_event(
                event.value, namespace=namespace
            )
            # Get global webhooks (no namespace filter)
            global_webhooks = self.webhook_store.get_webhooks_for_event(event.value, namespace=None)

            # Combine and deduplicate
            all_webhooks = {w["id"]: w for w in namespace_webhooks}
            for w in global_webhooks:
                if w.get("namespace") is None:  # Only add truly global webhooks
                    all_webhooks[w["id"]] = w

            webhooks = list(all_webhooks.values())

        except Exception as exc:
            logger.error(
                "Failed to get webhooks for event %s: error_type=%s",
                event.value,
                type(exc).__name__,
            )
            return []

        if not webhooks:
            logger.debug(f"No webhooks registered for event {event.value} in namespace {namespace}")
            return []

        logger.info(
            f"Dispatching {len(webhooks)} webhooks for {event.value} "
            f"(job={job.metadata.name}, namespace={namespace})"
        )

        # Dispatch all webhooks concurrently. _deliver_webhook owns normal
        # exception conversion; this boundary protects accounting and redaction
        # if a future regression (or an injected implementation) escapes it.
        tasks = [self._deliver_webhook(webhook, payload) for webhook in webhooks]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        delivery_results: list[WebhookDeliveryResult] = []
        for webhook, result in zip(webhooks, results, strict=True):
            if isinstance(result, WebhookDeliveryResult):
                delivery_results.append(result)
                continue
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                raw_webhook_id = webhook.get("id")
                webhook_id = raw_webhook_id if isinstance(raw_webhook_id, str) else "<unknown>"
                raw_url = webhook.get("url")
                url = raw_url if isinstance(raw_url, str) else ""
                logger.error(
                    "Webhook delivery escaped boundary: webhook_id=%s error_type=%s",
                    webhook_id,
                    type(result).__name__,
                )
                self._deliveries_total += 1
                self._deliveries_failed += 1
                delivery_results.append(
                    WebhookDeliveryResult(
                        webhook_id=webhook_id,
                        url=url,
                        event=event.value,
                        success=False,
                        error=f"Delivery failure: {type(result).__name__}",
                        attempts=0,
                        duration_ms=0.0,
                    )
                )

        return delivery_results

    async def _process_job_event(self, event_type: str, job: V1Job) -> None:
        """Process a Kubernetes job event."""
        job_uid = job.metadata.uid
        job_name = job.metadata.name
        namespace = job.metadata.namespace

        # Skip system namespaces
        if namespace in ("kube-system", "kube-public", "kube-node-lease"):
            return

        # Skip if not in watched namespaces (if specified)
        if self.namespaces and namespace not in self.namespaces:
            return

        current_status = self._compute_job_status(job)

        if event_type == "DELETED":
            self._job_state_cache.remove(job_uid)
            return

        # Get previous state and update cache
        previous_status = self._job_state_cache.set_state(job_uid, current_status)

        # Determine if we should fire an event
        webhook_event = self._determine_event(previous_status, current_status)

        if webhook_event:
            logger.info(
                f"Job status transition: {job_name} ({namespace}) "
                f"{previous_status or 'new'} -> {current_status} "
                f"-> firing {webhook_event.value}"
            )
            await self._dispatch_event(webhook_event, job)

    def _sync_watch_jobs(self) -> list[tuple[str, Any]]:
        """
        Synchronous job watcher that yields batches of events.
        This runs in a thread executor to avoid blocking the async event loop.
        """
        w = Watch()
        events = []

        try:
            # Watch jobs with a short timeout to allow periodic returns
            for event in w.stream(
                self.batch_v1.list_job_for_all_namespaces,
                timeout_seconds=30,  # Short timeout to return control periodically
            ):
                if not self._running:
                    break

                events.append((event["type"], event["object"]))

                # Return batch after collecting some events or if we have any
                if len(events) >= 10:
                    break

        except ApiException as e:
            if e.status == 410:  # Gone - resource version too old
                logger.warning("Watch expired, will restart...")
            else:
                logger.error(f"Kubernetes API error in job watcher: {e}")
                raise
        except Exception as e:
            logger.error(f"Error in sync job watcher: {e}")
            raise

        return events

    async def _watch_jobs(self) -> None:
        """Watch Kubernetes jobs for status changes using thread executor."""
        logger.info(f"Starting job watcher for namespaces: {self.namespaces}")

        while self._running:
            try:
                # Run the synchronous watch in a thread executor
                events = await asyncio.to_thread(self._sync_watch_jobs)

                # Process collected events
                for event_type, job in events:
                    if not self._running:
                        break

                    try:
                        await self._process_job_event(event_type, job)
                    except Exception as e:
                        logger.error(f"Error processing job event: {e}")

                # Small delay between watch cycles if no events
                if not events:
                    await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Error in job watcher: {e}")
                await asyncio.sleep(5)

    async def start(self) -> None:
        """Start the webhook dispatcher."""
        if self._running:
            logger.warning("Webhook dispatcher already running")
            return

        self._running = True
        logger.info(f"Starting webhook dispatcher for cluster {self.cluster_id}")

        # Initialize job state cache with current jobs
        await self._initialize_job_cache()

        # Start the watch task
        self._watch_task = asyncio.create_task(self._watch_jobs())

    async def stop(self) -> None:
        """Stop the webhook dispatcher."""
        logger.info("Stopping webhook dispatcher")
        self._running = False

        if self._watch_task:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
            self._watch_task = None

    async def _initialize_job_cache(self) -> None:
        """Initialize the job state cache with current job states."""
        try:
            jobs = self.batch_v1.list_job_for_all_namespaces(
                _request_timeout=self._k8s_timeout,
            )
            for job in jobs.items:
                namespace = job.metadata.namespace
                if namespace in ("kube-system", "kube-public", "kube-node-lease"):
                    continue
                if self.namespaces and namespace not in self.namespaces:
                    continue

                job_uid = job.metadata.uid
                status = self._compute_job_status(job)
                self._job_state_cache.set_state(job_uid, status)

            logger.info(f"Initialized job cache with {len(self._job_state_cache.job_states)} jobs")
        except Exception as e:
            logger.error(f"Failed to initialize job cache: {e}")

    def get_metrics(self) -> dict[str, Any]:
        """Get dispatcher metrics."""
        return {
            "deliveries_total": self._deliveries_total,
            "deliveries_success": self._deliveries_success,
            "deliveries_failed": self._deliveries_failed,
            "cached_jobs": len(self._job_state_cache.job_states),
            "running": self._running,
        }


def create_webhook_dispatcher_from_env() -> WebhookDispatcher:
    """Create WebhookDispatcher instance from environment variables."""
    cluster_id = os.getenv("CLUSTER_NAME", "unknown-cluster")
    region = os.getenv("REGION", "unknown-region")
    timeout = int(os.getenv("WEBHOOK_TIMEOUT", "30"))
    max_retries = int(os.getenv("WEBHOOK_MAX_RETRIES", "3"))
    retry_delay = int(os.getenv("WEBHOOK_RETRY_DELAY", "5"))

    # Parse namespaces from env
    namespaces_str = os.getenv("ALLOWED_NAMESPACES", "gco-jobs,default")
    namespaces = [ns.strip() for ns in namespaces_str.split(",") if ns.strip()]

    # Parse allowed domains from env (comma-separated)
    allowed_domains_str = os.getenv("WEBHOOK_ALLOWED_DOMAINS", "")
    allowed_domains = [d.strip() for d in allowed_domains_str.split(",") if d.strip()]

    return WebhookDispatcher(
        cluster_id=cluster_id,
        region=region,
        timeout=timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
        namespaces=namespaces,
        allowed_domains=allowed_domains,
    )


async def main() -> None:
    """Main function for running the webhook dispatcher standalone."""
    dispatcher = create_webhook_dispatcher_from_env()

    try:
        await dispatcher.start()

        # Keep running until interrupted
        while True:
            await asyncio.sleep(60)
            metrics = dispatcher.get_metrics()
            logger.info(f"Webhook dispatcher metrics: {metrics}")

    except KeyboardInterrupt:
        logger.info("Webhook dispatcher stopped by user")
    finally:
        await dispatcher.stop()


if __name__ == "__main__":
    asyncio.run(main())
