#!/usr/bin/env python3
"""
Test script for webhook delivery.

This script tests the webhook dispatcher by:
1. Starting a local HTTP server to receive webhooks
2. Creating a mock job event
3. Dispatching the webhook
4. Verifying the payload was received correctly

Usage:
    python scripts/test_webhook_delivery.py

For testing with a real webhook.site endpoint:
    python scripts/test_webhook_delivery.py --url https://webhook.site/your-uuid
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Store received webhooks
received_webhooks: list[dict[str, Any]] = []


class WebhookHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler to receive webhook requests."""

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        webhook_data = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": body.decode("utf-8"),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        received_webhooks.append(webhook_data)

        print(f"\n{'=' * 60}")
        print("WEBHOOK RECEIVED!")
        print(f"{'=' * 60}")
        print(f"Event: {self.headers.get('X-GCO-Event', 'unknown')}")
        print(f"Cluster: {self.headers.get('X-GCO-Cluster', 'unknown')}")
        print(f"Region: {self.headers.get('X-GCO-Region', 'unknown')}")
        sig = self.headers.get("X-GCO-Signature")
        if sig:
            print(f"Signature: {sig[:50]}...")

        try:
            payload = json.loads(body)
            print("\nPayload:")
            print(json.dumps(payload, indent=2))
        except json.JSONDecodeError:
            print(f"\nRaw body: {body.decode('utf-8')}")

        print(f"{'=' * 60}\n")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "received"}')

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default logging
        pass


def start_local_server(port: int = 8888) -> HTTPServer:
    """Start a local HTTP server to receive webhooks."""
    server = HTTPServer(("localhost", port), WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Local webhook server started on http://localhost:{port}")
    return server


def create_mock_job() -> Any:
    """Create a mock Kubernetes job object."""
    job = MagicMock()
    job.metadata.name = "test-webhook-job"
    job.metadata.namespace = "gco-jobs"
    job.metadata.uid = "test-job-uid-12345"
    job.metadata.labels = {"app": "webhook-test", "team": "platform"}
    job.status.conditions = [MagicMock(type="Complete", status="True")]
    job.status.active = 0
    job.status.succeeded = 1
    job.status.failed = 0
    job.status.start_time = datetime(2026, 2, 4, 12, 0, 0, tzinfo=UTC)
    job.status.completion_time = datetime(2026, 2, 4, 12, 5, 0, tzinfo=UTC)
    return job


async def test_with_local_server() -> bool:
    """Test webhook delivery with a local server."""
    from gco.services.webhook_dispatcher import WebhookDispatcher, WebhookEvent

    print("\n" + "=" * 60)
    print("WEBHOOK DELIVERY TEST - LOCAL SERVER")
    print("=" * 60 + "\n")

    # Start local server
    port = 8888
    server = start_local_server(port)
    webhook_url = f"http://localhost:{port}/webhook"
    webhook_secret = "test-secret-key"  # nosec B105 — local test fixture, not a real secret

    # Create mock webhook store
    mock_store = MagicMock()
    mock_store.get_webhooks_for_event.return_value = [
        {
            "id": "test-webhook-1",
            "url": webhook_url,
            "events": ["job.completed"],
            "namespace": "gco-jobs",
            "secret": webhook_secret,
        }
    ]

    # Create dispatcher with mocked K8s config
    with patch("gco.services.webhook_dispatcher.config") as mock_config:
        mock_config.ConfigException = Exception
        mock_config.load_incluster_config.side_effect = Exception()
        mock_config.load_kube_config.return_value = None

        with patch("gco.services.webhook_dispatcher.client"):
            dispatcher = WebhookDispatcher(
                cluster_id="test-cluster",
                region="us-east-1",
                webhook_store=mock_store,
                timeout=10,
                max_retries=1,
                retry_delay=1,
            )

    # Create mock job
    job = create_mock_job()

    print("Dispatching webhook for job.completed event...")
    print(f"Target URL: {webhook_url}")
    print("Secret configured: Yes")
    print()

    # Dispatch the event
    results = await dispatcher._dispatch_event(WebhookEvent.JOB_COMPLETED, job)

    # Check results
    print("\n" + "-" * 40)
    print("DELIVERY RESULTS:")
    print("-" * 40)

    for result in results:
        status = "✓ SUCCESS" if result.success else "✗ FAILED"
        print(f"{status}")
        print(f"  Webhook ID: {result.webhook_id}")
        print(f"  URL: {result.url}")
        print(f"  Status Code: {result.status_code}")
        print(f"  Attempts: {result.attempts}")
        print(f"  Duration: {result.duration_ms:.1f}ms")
        if result.error:
            print(f"  Error: {result.error}")

    # Verify signature
    if received_webhooks:
        print("\n" + "-" * 40)
        print("SIGNATURE VERIFICATION:")
        print("-" * 40)

        webhook = received_webhooks[-1]
        signature = webhook["headers"].get("X-GCO-Signature", "")
        body = webhook["body"]

        expected = hmac.new(
            webhook_secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if signature == f"sha256={expected}":
            print("✓ Signature verified successfully!")
        else:
            print("✗ Signature verification failed!")
            print(f"  Expected: sha256={expected}")
            print(f"  Received: {signature}")

    # Cleanup
    server.shutdown()

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60 + "\n")

    return len(results) > 0 and all(r.success for r in results)


async def test_with_external_url(url: str, secret: str | None = None) -> bool:
    """Test webhook delivery with an external URL (e.g., webhook.site)."""
    from gco.services.webhook_dispatcher import WebhookDispatcher, WebhookEvent

    print("\n" + "=" * 60)
    print("WEBHOOK DELIVERY TEST - EXTERNAL URL")
    print("=" * 60 + "\n")

    print(f"Target URL: {url}")
    print(f"Secret configured: {'Yes' if secret else 'No'}")
    print()

    # Create mock webhook store
    mock_store = MagicMock()
    webhook_config = {
        "id": "external-webhook",
        "url": url,
        "events": ["job.completed", "job.failed", "job.started"],
        "namespace": None,  # Global webhook
    }
    if secret:
        webhook_config["secret"] = secret
    mock_store.get_webhooks_for_event.return_value = [webhook_config]

    # Create dispatcher with mocked K8s config
    with patch("gco.services.webhook_dispatcher.config") as mock_config:
        mock_config.ConfigException = Exception
        mock_config.load_incluster_config.side_effect = Exception()
        mock_config.load_kube_config.return_value = None

        with patch("gco.services.webhook_dispatcher.client"):
            dispatcher = WebhookDispatcher(
                cluster_id="gco-test-cluster",
                region="us-east-1",
                webhook_store=mock_store,
                timeout=30,
                max_retries=2,
                retry_delay=2,
            )

    # Create mock job
    job = create_mock_job()

    # Test all three event types
    events = [
        (WebhookEvent.JOB_STARTED, "job.started"),
        (WebhookEvent.JOB_COMPLETED, "job.completed"),
        (WebhookEvent.JOB_FAILED, "job.failed"),
    ]

    all_success = True

    for event, event_name in events:
        # Adjust job status for the event
        if event == WebhookEvent.JOB_STARTED:
            job.status.conditions = []
            job.status.active = 1
            job.status.succeeded = 0
            job.status.completion_time = None
        elif event == WebhookEvent.JOB_COMPLETED:
            job.status.conditions = [MagicMock(type="Complete", status="True")]
            job.status.active = 0
            job.status.succeeded = 1
            job.status.completion_time = datetime(2026, 2, 4, 12, 5, 0, tzinfo=UTC)
        else:  # WebhookEvent.JOB_FAILED — the last of the three events above
            job.status.conditions = [MagicMock(type="Failed", status="True")]
            job.status.active = 0
            job.status.succeeded = 0
            job.status.failed = 1
            job.status.completion_time = datetime(2026, 2, 4, 12, 5, 0, tzinfo=UTC)

        print(f"Dispatching {event_name} event...")
        results = await dispatcher._dispatch_event(event, job)

        for result in results:
            status = "✓" if result.success else "✗"
            print(
                f"  {status} Status: {result.status_code}, "
                f"Attempts: {result.attempts}, "
                f"Duration: {result.duration_ms:.1f}ms"
            )
            if not result.success:
                print(f"    Error: {result.error}")
                all_success = False

        # Small delay between events
        await asyncio.sleep(1)

    print("\n" + "=" * 60)
    if all_success:
        print("ALL WEBHOOKS DELIVERED SUCCESSFULLY!")
        print(f"Check your webhook receiver at: {url}")
    else:
        print("SOME WEBHOOKS FAILED - Check errors above")
    print("=" * 60 + "\n")

    return all_success


async def main() -> int:
    parser = argparse.ArgumentParser(description="Test webhook delivery")
    parser.add_argument(
        "--url",
        help="External webhook URL (e.g., https://webhook.site/your-uuid)",
    )
    parser.add_argument(
        "--secret",
        help="HMAC secret for signature verification",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run test with local server (default if no URL provided)",
    )

    args = parser.parse_args()

    if args.url:
        success = await test_with_external_url(args.url, args.secret)
    else:
        success = await test_with_local_server()

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
