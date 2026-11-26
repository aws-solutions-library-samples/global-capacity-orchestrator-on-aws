"""Where AWS Regions are, for drawing them on the dashboard's map.

Display names come from botocore's bundled endpoint data (the same source the
CLI uses to resolve endpoints), so a Region's label is always the one AWS
publishes. Coordinates are not in botocore: the table below places each public
Region at its metropolitan area, which is all a world map needs. A Region that
is missing from the table (one AWS launched after this table was written) is
still listed everywhere else in the dashboard; it is only left off the map.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass

from botocore.session import get_session as get_botocore_session

#: Approximate (latitude, longitude) of each public Region's metro area.
REGION_COORDINATES: dict[str, tuple[float, float]] = {
    "af-south-1": (-33.92, 18.42),
    "ap-east-1": (22.32, 114.17),
    "ap-east-2": (25.03, 121.57),
    "ap-northeast-1": (35.68, 139.69),
    "ap-northeast-2": (37.57, 126.98),
    "ap-northeast-3": (34.69, 135.50),
    "ap-south-1": (19.08, 72.88),
    "ap-south-2": (17.39, 78.49),
    "ap-southeast-1": (1.35, 103.82),
    "ap-southeast-2": (-33.87, 151.21),
    "ap-southeast-3": (-6.21, 106.85),
    "ap-southeast-4": (-37.81, 144.96),
    "ap-southeast-5": (3.14, 101.69),
    "ap-southeast-6": (-36.85, 174.76),
    "ap-southeast-7": (13.76, 100.50),
    "ca-central-1": (45.50, -73.57),
    "ca-west-1": (51.05, -114.07),
    "cn-north-1": (39.90, 116.41),
    "cn-northwest-1": (38.49, 106.23),
    "eu-central-1": (50.11, 8.68),
    "eu-central-2": (47.38, 8.54),
    "eu-north-1": (59.33, 18.07),
    "eu-south-1": (45.46, 9.19),
    "eu-south-2": (41.65, -0.89),
    "eu-west-1": (53.35, -6.26),
    "eu-west-2": (51.51, -0.13),
    "eu-west-3": (48.86, 2.35),
    "eusc-de-east-1": (52.41, 12.53),
    "il-central-1": (32.09, 34.78),
    "me-central-1": (25.20, 55.27),
    "me-south-1": (26.23, 50.59),
    "mx-central-1": (20.59, -100.39),
    "sa-east-1": (-23.55, -46.63),
    "us-east-1": (39.04, -77.49),
    "us-east-2": (39.96, -83.00),
    "us-gov-east-1": (40.42, -82.91),
    "us-gov-west-1": (44.06, -121.31),
    "us-west-1": (37.77, -122.42),
    "us-west-2": (45.84, -119.70),
}

_PARENTHESIZED = re.compile(r"\(([^)]+)\)")


@dataclass(frozen=True)
class RegionInfo:
    """One Region as the dashboard labels and places it."""

    code: str
    name: str
    city: str
    latitude: float | None
    longitude: float | None

    @property
    def on_map(self) -> bool:
        """Whether the Region has coordinates to draw."""
        return self.latitude is not None and self.longitude is not None


@functools.cache
def _descriptions() -> dict[str, str]:
    """``{region code: AWS description}`` across every partition botocore knows."""
    endpoints = get_botocore_session().get_data("endpoints")
    descriptions: dict[str, str] = {}
    for partition in endpoints.get("partitions", []):
        for code, details in partition.get("regions", {}).items():
            description = details.get("description") if isinstance(details, dict) else None
            if isinstance(description, str) and description:
                descriptions[code] = description
    return descriptions


def region_info(code: str) -> RegionInfo:
    """Label and place ``code``; unknown codes keep the code as their name."""
    name = _descriptions().get(code, code)
    match = _PARENTHESIZED.search(name)
    city = match.group(1) if match else name
    coordinates = REGION_COORDINATES.get(code)
    latitude, longitude = coordinates if coordinates else (None, None)
    return RegionInfo(code=code, name=name, city=city, latitude=latitude, longitude=longitude)


def region_label(code: str) -> str:
    """``us-east-1 · N. Virginia`` (just the code when AWS publishes no name)."""
    info = region_info(code)
    return code if info.city == code else f"{code} · {info.city}"
