"""Shared tags, labels, and tuning constants for live validation."""

from __future__ import annotations

import copy
import uuid
from typing import Any

_TERMINAL_QUEUE_STATUSES = {"succeeded", "failed", "cancelled"}


_HEALTHY_STACK_STATUSES = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}


_RUN_STACK_TAG = "GcoLiveValidationRun"


_RUN_JOB_LABEL = "gco.aws/validation-run"


_PATH_JOB_LABEL = "gco.aws/validation-path"


_CENTRAL_MANAGED_BY_LABEL = "gco.io/managed-by"


_CENTRAL_QUEUE_KEY_LABEL = "gco.io/queue-job-key"


_CENTRAL_QUEUE_ID_ANNOTATION = "gco.io/queue-job-id"


_CENTRAL_ORIGINAL_NAME_ANNOTATION = "gco.io/original-job-name"


_EKS_KEY_LOGICAL_ID = "EksSecretsEncryptionKey74AFFE88"


_KMS_PENDING_WINDOW_DAYS = 7


_LOG_CLEANUP_TOKEN_TAG = "GcoLiveValidationCleanupToken"


_LOG_CLEANUP_HELPER_STACK_PREFIX = "LiveValidationLogCleanup"


_LOG_CLEANUP_HELPER_RUN_TAG = "LiveValidationHelperRun"


_LOG_CLEANUP_HELPER_TOKEN_TAG = "LiveValidationHelperToken"


_LOG_CLEANUP_ROLE_RUN_TAG = "LiveValidationCleanupRoleRun"


_LOG_CLEANUP_ROLE_TOKEN_TAG = "LiveValidationCleanupRoleToken"


_LOG_CLEANUP_ROLE_OUTPUT = "CleanupRoleArn"


_LOG_CLEANUP_ROLE_POLICY_NAME = "DeleteTaggedLogGroups"


_LOG_CLEANUP_SESSION_SECONDS = 900


_LOG_CLEANUP_STACK_POLL_ATTEMPTS = 120


_LOG_CLEANUP_STACK_POLL_SECONDS = 5


_LOG_GROUP_OBSERVATION_ATTEMPTS = 6


_LOG_GROUP_CLEANUP_MAX_PASSES = 3


_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS = 2


_LOG_GROUP_CLEANUP_STABLE_OBSERVATIONS = 2


_LOG_GROUP_ABSENCE_OBSERVATIONS = 3


_LOG_GROUP_OBSERVATION_POLL_SECONDS = 1


_LOG_GROUP_OBSERVATION_HISTORY_LIMIT = 40


_LOG_GROUP_RETRYABLE_OBSERVATION_CODES = frozenset(
    {
        "InternalFailure",
        "InternalServerError",
        "OperationAbortedException",
        "RequestLimitExceeded",
        "ServiceUnavailableException",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
    }
)


_LOG_GROUP_SOURCE_TYPES = {
    "AWS::EKS::Cluster",
    "AWS::Lambda::Function",
    "AWS::Logs::LogGroup",
}


_EKS_LOG_GROUP_SUFFIXES = ("application", "dataplane", "host", "performance")


# UUID version-5 namespaces. Both are arbitrary constants generated once and
# then frozen: their only job is to seed `uuid.uuid5`, which hashes
# (namespace, name) into a deterministic UUID. Using a private namespace rather
# than hashing the name alone keeps these identifiers from colliding with any
# other UUID this project or AWS derives from the same input string.
#
# Treat both values as immutable. Changing one silently changes every ID derived
# from it, which for an in-flight or resumed run means the harness would compute
# a different identifier for the same logical thing and lose the trail back to
# what it already created.

#: Namespace for central-queue Job IDs. ``_central_queue_job_id`` derives the
#: DynamoDB queue Job ID as ``uuid5(this, idempotency_key)``, so the same
#: idempotency key always produces the same Job ID. That is what makes the
#: central-queue submission safely retryable: a resumed run recomputes the
#: identical ID, finds its own existing queue record, and reconciles it instead
#: of enqueueing a duplicate workload.
_CENTRAL_QUEUE_IDEMPOTENCY_NAMESPACE = uuid.UUID("88284d12-1e04-47d5-8871-607a9e4dac09")

#: Namespace for the delegated log-cleanup helper's identifiers, used twice:
#: ``_log_cleanup_helper_spec`` derives the helper CloudFormation stack and IAM
#: role name from ``uuid5(this, "<partition>:<account>:<run_id>:<cleanup_token>")``,
#: and the tag-conditioned deleter derives its STS session name from
#: ``uuid5(this, run_id)``. Deriving rather than randomizing means a resumed run
#: recomputes the exact same helper stack, role, and session names, so it can
#: find and delete the helper it created earlier instead of orphaning it — while
#: still keeping those names unique per run and account.
_LOG_CLEANUP_HELPER_NAMESPACE = uuid.UUID("83af5e0b-f987-4ca6-8bb6-aa174c57096c")


class _LogGroupCleanupError(RuntimeError):
    """Retain structured cleanup evidence while propagating a failed phase."""

    def __init__(self, message: str, details: dict[str, Any]):
        super().__init__(message)
        self.details = copy.deepcopy(details)
