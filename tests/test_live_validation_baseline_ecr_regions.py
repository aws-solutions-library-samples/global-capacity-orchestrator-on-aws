"""The baseline accepts project ECR repositories outside the topology's Regions.

``gco stacks destroy-all`` leaves the image mirror's repositories
(``gco/dockerhub/...``) behind, because ``gco images mirror`` creates them
outside CloudFormation. After a fleet that used other Regions was torn down,
the clean-account gate found those repositories in Regions whose ECR the
baseline had not recorded and refused the account ("Fresh baseline contains
project resources not owned by this run") until they were deleted by hand.
The baseline now records ECR in the topology's Regions and in every other
Region that already holds a project repository, so the leftovers are accepted
the way a topology Region's repositories are: final-inventory requires them
unchanged.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from scripts.live_release_validation.actions import baseline as actions_baseline
from scripts.live_release_validation.inventory import project as inventory_project
from tests.test_live_release_validation import _context

ACCOUNT = "123456789012"
MIRROR_ARN = f"arn:aws:ecr:eu-west-1:{ACCOUNT}:repository/gco-live/dockerhub/volcanosh/vc-scheduler"


def _repository(region: str, name: str) -> dict[str, Any]:
    return {"name": name, "arn": f"arn:aws:ecr:{region}:{ACCOUNT}:repository/{name}", "images": []}


def _inventory(regional: dict[str, dict[str, list[Any]]]) -> dict[str, Any]:
    """A shape-valid inventory: the absence gate is fail-closed on coverage."""
    return {
        "coverage": {
            "complete": True,
            "required_scanners": list(inventory_project._PROJECT_RESOURCE_SCANNERS),
            "completed_scanners": list(inventory_project._PROJECT_RESOURCE_SCANNERS),
            "resource_categories": list(inventory_project._PROJECT_RESOURCE_CATEGORIES),
        },
        "regional": regional,
    }


#: The leftovers of a torn-down fleet: the mirror's repositories in us-west-2
#: (found by name) and in eu-west-1 (found by the Tagging API), and an
#: unrelated tagged resource in ap-south-1 that is not an ECR repository.
LEFTOVERS = {
    "us-east-1": {"ecr_repositories": [], "tagged_resources": []},
    "us-west-2": {
        "ecr_repositories": ["gco-live/dockerhub/volcanosh/vc-controller-manager"],
        "tagged_resources": [],
    },
    "eu-west-1": {"tagged_resources": [{"arn": MIRROR_ARN, "tags": {"gco:project": "gco-live"}}]},
    "ap-south-1": {
        "ecr_repositories": [],
        "tagged_resources": [
            {"arn": f"arn:aws:sqs:ap-south-1:{ACCOUNT}:gco-live-jobs", "tags": {}},
        ],
    },
}


class TestBaselineEcrRegions:
    def _ctx(self) -> Any:
        ctx = _context(
            state={"enabled_regions": ["ap-south-1", "eu-west-1", "us-east-1", "us-west-2"]}
        )
        ctx.checkpoint.baseline = None
        ctx.settings.protected_stack_names = ("CDKToolkit",)
        return ctx

    def test_the_topology_comes_first_then_each_region_holding_a_project_repository(self) -> None:
        with patch.object(actions_baseline, "_topology_regions", return_value=("us-east-1",)):
            regions = actions_baseline._baseline_ecr_regions(self._ctx(), _inventory(LEFTOVERS))

        assert regions == ["us-east-1", "eu-west-1", "us-west-2"]

    def test_a_topology_region_is_listed_once_and_an_empty_inventory_adds_none(self) -> None:
        with patch.object(
            actions_baseline, "_topology_regions", return_value=("us-east-2", "us-east-1")
        ):
            assert actions_baseline._baseline_ecr_regions(
                self._ctx(), _inventory({"us-east-1": {"ecr_repositories": ["gco-live/x"]}})
            ) == ["us-east-2", "us-east-1"]
            assert actions_baseline._baseline_ecr_regions(self._ctx(), {}) == [
                "us-east-2",
                "us-east-1",
            ]

    @pytest.mark.parametrize(
        ("arn", "expected"),
        [
            (MIRROR_ARN, True),
            (f"arn:aws:ecr:us-east-1:{ACCOUNT}:repository/gco-live", True),
            (f"arn:aws:ecr:us-east-1:{ACCOUNT}:pull-through-cache-rule/gco", False),
            (f"arn:aws:sqs:us-east-1:{ACCOUNT}:repository/gco", False),
            ("arn:aws:ecr:us-east-1", False),
            (None, False),
        ],
    )
    def test_only_ecr_repository_arns_count(self, arn: object, expected: bool) -> None:
        assert actions_baseline._is_ecr_repository_arn(arn) is expected

    def test_leftover_mirror_repositories_are_baselined_not_refused(self) -> None:
        """The regression: this account was refused before the fix."""
        ctx = self._ctx()
        captured: dict[str, Any] = {}

        def capture(_session: Any, *, enabled_regions: Any, ecr_regions: Any, **_kwargs: Any):
            captured["ecr_regions"] = list(ecr_regions)
            return {
                "protected_stacks": {},
                "ecr_regions": sorted(ecr_regions),
                "ecr_repositories": {
                    "us-east-1": [],
                    "us-west-2": [
                        _repository(
                            "us-west-2", "gco-live/dockerhub/volcanosh/vc-controller-manager"
                        )
                    ],
                    "eu-west-1": [
                        _repository("eu-west-1", "gco-live/dockerhub/volcanosh/vc-scheduler")
                    ],
                },
            }

        transaction_search = {
            "schema_version": 1,
            "policy_name": "gco-transaction-search-xray-access",
            "regions": {},
        }
        with (
            patch.object(actions_baseline, "_topology_regions", return_value=("us-east-1",)),
            patch.object(
                actions_baseline,
                "collect_project_resources",
                return_value=_inventory(
                    {region: found for region, found in LEFTOVERS.items() if region != "ap-south-1"}
                ),
            ),
            patch.object(actions_baseline, "capture_baseline", side_effect=capture),
            patch.object(
                actions_baseline,
                "_strip_accepted_efs_automatic_backup_recovery_points",
                side_effect=lambda _ctx, candidate: (candidate, []),
            ),
            patch.object(
                actions_baseline,
                "_strip_deleted_analytics_resources",
                side_effect=lambda _ctx, candidate: (candidate, []),
            ),
            patch.object(
                actions_baseline,
                "_strip_expired_table_streams",
                side_effect=lambda _ctx, candidate: (candidate, []),
            ),
            patch.object(
                actions_baseline,
                "_strip_deleted_vpc_endpoints",
                side_effect=lambda _ctx, candidate: (candidate, []),
            ),
            patch.object(
                actions_baseline,
                "_strip_deleted_volumes",
                side_effect=lambda _ctx, candidate: (candidate, []),
            ),
            patch.object(
                actions_baseline,
                "_strip_deleted_hyperpod_clusters",
                side_effect=lambda _ctx, candidate: (candidate, []),
            ),
            patch.object(
                actions_baseline,
                "_capture_transaction_search_baseline",
                return_value=json.loads(json.dumps(transaction_search)),
            ),
        ):
            result = actions_baseline.action_baseline(ctx)

        assert captured["ecr_regions"] == ["us-east-1", "eu-west-1", "us-west-2"]
        assert result["ecr_regions"] == ["eu-west-1", "us-east-1", "us-west-2"]
        # final-inventory re-reads ECR in exactly these Regions and compares.
        assert ctx.checkpoint.baseline["ecr_regions"] == ["eu-west-1", "us-east-1", "us-west-2"]

    def test_a_project_resource_that_is_not_ecr_is_still_refused(self) -> None:
        ctx = self._ctx()
        leftovers = _inventory(
            {
                "us-east-1": {"ecr_repositories": [], "tagged_resources": []},
                "ap-south-1": LEFTOVERS["ap-south-1"],
            }
        )

        def capture(_session: Any, *, ecr_regions: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "protected_stacks": {},
                "ecr_regions": sorted(ecr_regions),
                "ecr_repositories": {region: [] for region in ecr_regions},
            }

        with (
            patch.object(actions_baseline, "_topology_regions", return_value=("us-east-1",)),
            patch.object(actions_baseline, "collect_project_resources", return_value=leftovers),
            patch.object(actions_baseline, "capture_baseline", side_effect=capture),
            patch.object(
                actions_baseline,
                "_strip_accepted_efs_automatic_backup_recovery_points",
                side_effect=lambda _ctx, candidate: (candidate, []),
            ),
            pytest.raises(RuntimeError, match="not owned by this run"),
        ):
            actions_baseline.action_baseline(ctx)
