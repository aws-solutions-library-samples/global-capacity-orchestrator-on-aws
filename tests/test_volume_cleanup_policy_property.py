"""Property tests for EBS volume-cleanup policy resolution."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    DestroyCommandKind,
    VolumePolicy,
    VolumePolicyConflictError,
    resolve_volume_cleanup_request,
)


@settings(max_examples=100, deadline=None)
@given(
    command=st.sampled_from(tuple(DestroyCommandKind)),
    retain_volumes=st.booleans(),
    delete_volumes=st.booleans(),
    yes=st.booleans(),
    repeat_count=st.integers(min_value=1, max_value=20),
)
def test_command_aware_policy_resolution_is_deterministic_and_safe(
    command: DestroyCommandKind,
    retain_volumes: bool,
    delete_volumes: bool,
    yes: bool,
    repeat_count: int,
) -> None:
    # Feature: cleanup-dynamic-ebs-volumes, Property 1: Command-aware policy resolution is deterministic and safe
    # **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.6, 1.8, 1.9**
    inputs = {
        "command": command,
        "retain_volumes": retain_volumes,
        "delete_volumes": delete_volumes,
        "yes": yes,
    }

    if retain_volumes and delete_volumes:
        for _ in range(repeat_count):
            with pytest.raises(VolumePolicyConflictError):
                resolve_volume_cleanup_request(**inputs)
        return

    decisions = [resolve_volume_cleanup_request(**inputs) for _ in range(repeat_count)]
    assert all(decision == decisions[0] for decision in decisions)
    decision = decisions[0]

    if retain_volumes:
        expected = (
            VolumePolicy.RETAIN,
            False,
            DeletionAuthorizationSource.NONE,
            False,
        )
    elif delete_volumes:
        expected = (
            VolumePolicy.DELETE,
            yes,
            (
                DeletionAuthorizationSource.EXPLICIT_DELETE_WITH_YES
                if yes
                else DeletionAuthorizationSource.NONE
            ),
            not yes,
        )
    elif command is DestroyCommandKind.ALL and yes:
        expected = (
            VolumePolicy.DELETE,
            True,
            DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
            False,
        )
    else:
        expected = (
            VolumePolicy.RETAIN,
            False,
            DeletionAuthorizationSource.NONE,
            False,
        )

    assert (
        decision.policy,
        decision.deletion_authorized,
        decision.authorization_source,
        decision.requires_volume_confirmation,
    ) == expected
