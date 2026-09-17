"""The SQS job envelope, shared by the producer and the consumer.

``gco jobs submit-sqs`` (:mod:`cli.jobs`) writes this envelope onto the
regional queue; the in-cluster queue processor
(:mod:`gco.services.queue_processor`) reads it back and applies the manifests.
The two halves ship in different artifacts — the CLI runs on an operator's
machine, the consumer inside a distroless service image that deliberately
cannot import ``cli/`` — so the wire format lived twice: once as a dict
literal in the CLI, once as a docstring and a set of ``body.get(...)`` calls
in the service. This module is that format, written once.

It is deliberately standard-library only, so anything that needs to *speak*
the protocol can import it: the service image, the CLI, and CI, which builds a
message with this exact code and hands it to the real consumer running in a
kind cluster.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: Namespace recorded in the envelope when neither the caller nor any manifest
#: names one. The consumer applies this same fallback when reading a message,
#: and it is the namespace the shipped ``job_validation_policy`` allows.
DEFAULT_JOB_NAMESPACE = "gco-jobs"


def first_manifest_namespace(manifests: list[dict[str, Any]]) -> str | None:
    """Return the first explicit ``metadata.namespace`` in a manifest list.

    The envelope's ``namespace`` field is informational — the consumer
    validates and applies each manifest under the namespace that manifest
    declares — so the producer reports the first declared one to keep its
    submission response truthful when the caller passes no namespace.
    Returns ``None`` when no manifest declares one.
    """
    for manifest in manifests:
        ns = manifest.get("metadata", {}).get("namespace") if isinstance(manifest, dict) else None
        if ns:
            return str(ns)
    return None


@dataclass(frozen=True)
class JobMessage:
    """One queue submission: the JSON body plus its SQS message attributes."""

    body: dict[str, Any]
    attributes: dict[str, dict[str, str]]

    @property
    def job_id(self) -> str:
        """The short id correlating this submission across CLI, queue and logs."""
        return str(self.body["job_id"])

    @property
    def namespace(self) -> str:
        """The envelope's informational namespace."""
        return str(self.body["namespace"])

    @property
    def priority(self) -> int:
        """The submitted priority (higher is more important)."""
        return int(self.body["priority"])

    def json(self) -> str:
        """Serialize the body exactly as it goes onto the queue."""
        return json.dumps(self.body)


def build_job_message(
    manifests: list[dict[str, Any]],
    *,
    namespace: str | None = None,
    priority: int = 0,
    job_id: str | None = None,
    submitted_at: datetime | None = None,
) -> JobMessage:
    """Build the queue message for ``manifests``.

    ``job_id`` and ``submitted_at`` default to a fresh short uuid and the
    current UTC instant; both are injectable so a caller that needs a
    reproducible message (a test, a replay) can pin them. ``namespace``
    falls back to the first namespace the manifests declare, then to
    :data:`DEFAULT_JOB_NAMESPACE`.
    """
    resolved_id = job_id or str(uuid.uuid4())[:8]
    resolved_namespace = namespace or first_manifest_namespace(manifests) or DEFAULT_JOB_NAMESPACE
    stamp = submitted_at or datetime.now(UTC)
    return JobMessage(
        body={
            "job_id": resolved_id,
            "manifests": manifests,
            "namespace": resolved_namespace,
            "priority": priority,
            "submitted_at": stamp.isoformat(),
        },
        attributes={
            "Priority": {"DataType": "Number", "StringValue": str(priority)},
            "JobId": {"DataType": "String", "StringValue": resolved_id},
        },
    )
