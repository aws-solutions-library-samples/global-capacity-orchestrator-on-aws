"""Manual Global Accelerator traffic-dial controls.

Backs ``gco capacity traffic-dial show|set|clear``. The global stack publishes
each region's endpoint-group ARN to SSM (``/{project}/endpoint-group-{region}-arn``
in the global region), which this module uses for discovery so it works with
any configured accelerator name. Runtime state shares one SSM tree with the
scheduled controller (``lambda/traffic-dial-controller``):

- ``/{project}/traffic-dial/state`` — the controller's last run summary.
- ``/{project}/traffic-dial/override-{region}`` — a manual override recorded
  by ``set``; the controller never touches an overridden region until
  ``clear`` removes the parameter.

``set`` applies the dial via ``UpdateEndpointGroup`` carrying *only*
``TrafficDialPercentage``: the API patches omitted fields, and omitting
``EndpointConfigurations`` preserves the registered ALB endpoint.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError

from cli.config import GCOConfig, get_config

logger = logging.getLogger(__name__)

#: The Global Accelerator control plane is homed in us-west-2 in the
#: commercial partition (same convention as the GCO Lambdas).
GA_CONTROL_PLANE_REGION = "us-west-2"


class TrafficDialError(Exception):
    """Raised when a traffic-dial operation cannot be performed."""


@dataclass
class RegionDialStatus:
    """One region's dial, endpoint health, and controller/override state."""

    region: str
    traffic_dial: int
    endpoint_health: str
    override: str | None = None
    controller_reason: str | None = None
    healthy_percent: float | None = None
    endpoint_group_arn: str = ""
    warnings: list[str] = field(default_factory=list)


class TrafficDialManager:
    """Reads and mutates per-region Global Accelerator traffic dials."""

    def __init__(self, config: GCOConfig | None = None):
        self.config = config or get_config()
        self._session = boto3.Session()

    def _ssm_client(self) -> Any:
        return self._session.client("ssm", region_name=self.config.global_region)

    def _ga_client(self) -> Any:
        return self._session.client("globalaccelerator", region_name=GA_CONTROL_PLANE_REGION)

    def _endpoint_group_parameter_pattern(self) -> re.Pattern[str]:
        project = re.escape(self.config.project_name)
        return re.compile(rf"^/{project}/endpoint-group-(?P<region>[a-z0-9-]+)-arn$")

    def _override_parameter_name(self, region: str) -> str:
        return f"/{self.config.project_name}/traffic-dial/override-{region}"

    def _state_parameter_name(self) -> str:
        return f"/{self.config.project_name}/traffic-dial/state"

    def discover_endpoint_groups(self) -> dict[str, str]:
        """Return ``{region: endpoint_group_arn}`` from the SSM registry."""
        ssm = self._ssm_client()
        pattern = self._endpoint_group_parameter_pattern()
        groups: dict[str, str] = {}
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "Path": f"/{self.config.project_name}",
                "Recursive": False,
            }
            if token:
                kwargs["NextToken"] = token
            response = ssm.get_parameters_by_path(**kwargs)
            for parameter in response.get("Parameters", []):
                match = pattern.match(str(parameter.get("Name", "")))
                if match:
                    groups[match.group("region")] = str(parameter.get("Value", ""))
            token = response.get("NextToken")
            if not token:
                break
        if not groups:
            raise TrafficDialError(
                "No Global Accelerator endpoint groups found in the SSM registry "
                f"(searched /{self.config.project_name}/endpoint-group-*-arn in "
                f"{self.config.global_region}). Traffic dialing requires the "
                "commercial-partition Global Accelerator topology."
            )
        return groups

    def read_overrides(self) -> dict[str, str]:
        """Return ``{region: value}`` for every manual override parameter."""
        ssm = self._ssm_client()
        prefix = f"/{self.config.project_name}/traffic-dial/"
        marker = f"{prefix}override-"
        overrides: dict[str, str] = {}
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Path": prefix, "Recursive": False}
            if token:
                kwargs["NextToken"] = token
            response = ssm.get_parameters_by_path(**kwargs)
            for parameter in response.get("Parameters", []):
                name = str(parameter.get("Name", ""))
                if name.startswith(marker):
                    overrides[name.removeprefix(marker)] = str(parameter.get("Value", ""))
            token = response.get("NextToken")
            if not token:
                break
        return overrides

    def read_controller_state(self) -> dict[str, Any] | None:
        """Return the controller's last run summary, or None when absent."""
        ssm = self._ssm_client()
        try:
            response = ssm.get_parameter(Name=self._state_parameter_name())
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
                return None
            raise
        try:
            state = json.loads(str(response["Parameter"]["Value"]))
        except (KeyError, ValueError):
            logger.warning("Traffic-dial state parameter holds invalid JSON")
            return None
        return state if isinstance(state, dict) else None

    @staticmethod
    def _summarize_endpoint_health(group: dict[str, Any]) -> str:
        descriptions = group.get("EndpointDescriptions", [])
        if not descriptions:
            return "no endpoints"
        healthy = sum(
            1 for endpoint in descriptions if endpoint.get("HealthState") == "HEALTHY"
        )
        return f"{healthy}/{len(descriptions)} healthy"

    def get_status(self) -> list[RegionDialStatus]:
        """Describe every region's dial, endpoint health, and override state."""
        groups = self.discover_endpoint_groups()
        overrides = self.read_overrides()
        state = self.read_controller_state() or {}
        decisions = {
            str(decision.get("region")): decision
            for decision in state.get("decisions", [])
            if isinstance(decision, dict)
        }

        ga = self._ga_client()
        statuses: list[RegionDialStatus] = []
        for region in sorted(groups):
            arn = groups[region]
            try:
                group = ga.describe_endpoint_group(EndpointGroupArn=arn).get(
                    "EndpointGroup", {}
                )
            except ClientError as exc:
                raise TrafficDialError(
                    f"Failed to describe the {region} endpoint group: {exc}"
                ) from exc
            decision = decisions.get(region, {})
            healthy_percent = decision.get("healthy_percent")
            statuses.append(
                RegionDialStatus(
                    region=region,
                    traffic_dial=int(round(float(group.get("TrafficDialPercentage", 100.0)))),
                    endpoint_health=self._summarize_endpoint_health(group),
                    override=overrides.get(region),
                    controller_reason=decision.get("reason"),
                    healthy_percent=(
                        float(healthy_percent) if healthy_percent is not None else None
                    ),
                    endpoint_group_arn=arn,
                )
            )
        return statuses

    def set_dial(self, region: str, percentage: int) -> RegionDialStatus:
        """Apply a manual dial and record the override the controller honors."""
        if not isinstance(percentage, int) or isinstance(percentage, bool):
            raise TrafficDialError(f"Percentage must be an integer, got {percentage!r}")
        if not 0 <= percentage <= 100:
            raise TrafficDialError(
                f"Percentage must be between 0 and 100, got {percentage}"
            )

        groups = self.discover_endpoint_groups()
        if region not in groups:
            raise TrafficDialError(
                f"No endpoint group registered for region '{region}'. "
                f"Known regions: {', '.join(sorted(groups))}"
            )

        warnings: list[str] = []
        if percentage < 100:
            ga = self._ga_client()
            others_below_100 = True
            for other_region, other_arn in groups.items():
                if other_region == region:
                    continue
                other = ga.describe_endpoint_group(EndpointGroupArn=other_arn).get(
                    "EndpointGroup", {}
                )
                if float(other.get("TrafficDialPercentage", 100.0)) >= 100.0:
                    others_below_100 = False
                    break
            if others_below_100:
                warnings.append(
                    "Every other region is already dialed below 100; this leaves the "
                    "listener with no fully dialed region to absorb redirected "
                    "traffic — a configuration whose resulting distribution Global "
                    "Accelerator does not document. The scheduled controller never "
                    "creates this state; proceeding because a manual override is "
                    "explicit operator intent."
                )

        ga = self._ga_client()
        try:
            # Only the dial: UpdateEndpointGroup patches omitted fields, and
            # omitting EndpointConfigurations preserves the registered ALB.
            updated = ga.update_endpoint_group(
                EndpointGroupArn=groups[region],
                TrafficDialPercentage=float(percentage),
            ).get("EndpointGroup", {})
        except ClientError as exc:
            raise TrafficDialError(
                f"Failed to update the {region} traffic dial: {exc}"
            ) from exc

        ssm = self._ssm_client()
        ssm.put_parameter(
            Name=self._override_parameter_name(region),
            Value=str(percentage),
            Type="String",
            Overwrite=True,
            Description=(
                f"Manual traffic-dial override for {region}; the scheduled "
                "controller skips this region until the override is cleared."
            ),
        )

        return RegionDialStatus(
            region=region,
            traffic_dial=int(round(float(updated.get("TrafficDialPercentage", percentage)))),
            endpoint_health=self._summarize_endpoint_health(updated),
            override=str(percentage),
            endpoint_group_arn=groups[region],
            warnings=warnings,
        )

    def clear_override(self, region: str) -> bool:
        """Remove a manual override; returns whether one existed.

        The dial itself is left unchanged: with the controller disabled or in
        monitor mode it keeps the last manual value, and in enforce mode the
        controller re-converges it from the region's health signal on its
        next cycle.
        """
        ssm = self._ssm_client()
        try:
            ssm.delete_parameter(Name=self._override_parameter_name(region))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
                return False
            raise
        return True


def get_traffic_dial_manager(config: GCOConfig | None = None) -> TrafficDialManager:
    """Get a configured traffic-dial manager instance."""
    return TrafficDialManager(config)
