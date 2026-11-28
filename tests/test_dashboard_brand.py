"""Unit tests for the dashboard's palette (``cli.dashboard.brand``) and Region table
(``cli.dashboard.regions``).

The palette is checked against the checkout's icon file and against itself
(every status that has a colour has a badge, and the reverse). Region labels
are read from botocore's bundled endpoint data, so the label tests use that
data as shipped; the parsing edge cases (odd entries, a name without a city in
parentheses) swap in a hand-written endpoints document. No network is used.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.dashboard import brand, regions

REPO_ROOT = Path(__file__).resolve().parent.parent
HEX_COLOR = re.compile(r"#[0-9A-F]{6}")
#: The colours ``st.badge`` and the ``:color-badge[]`` Markdown directive accept.
STREAMLIT_BADGE_COLORS = {
    "red",
    "orange",
    "yellow",
    "blue",
    "green",
    "violet",
    "gray",
    "grey",
    "primary",
}


class TestIcon:
    def test_icon_is_the_checkout_icon_byte_for_byte(self) -> None:
        icon = (REPO_ROOT / "images" / "gco-icon.svg").read_bytes()
        assert brand.ICON_SVG.encode("utf-8") == icon

    def test_icon_paints_with_the_brand_colours(self) -> None:
        root = ET.fromstring(brand.ICON_SVG)
        fills = {element.get("fill") for element in root.iter() if element.get("fill")}
        assert fills == {brand.INK, brand.PAPER, brand.ORANGE}


class TestPalette:
    def test_every_status_colour_has_a_badge(self) -> None:
        assert list(brand.STATUS_BADGES) == list(brand.STATUS_COLORS)

    def test_every_health_colour_has_a_badge(self) -> None:
        assert list(brand.HEALTH_BADGES) == list(brand.HEALTH_COLORS)

    def test_badges_use_colours_streamlit_draws(self) -> None:
        used = set(brand.STATUS_BADGES.values()) | set(brand.HEALTH_BADGES.values())
        assert used <= STREAMLIT_BADGE_COLORS

    def test_colours_are_six_digit_hex(self) -> None:
        colours = [
            brand.ORANGE,
            brand.INK,
            brand.PAPER,
            brand.MUTED,
            *brand.STATUS_COLORS.values(),
            *brand.ENDPOINT_COLORS.values(),
            *brand.HEALTH_COLORS.values(),
            *brand.SERIES,
        ]
        assert [c for c in colours if not HEX_COLOR.fullmatch(c)] == []

    def test_series_leads_with_the_brand_orange_and_never_repeats(self) -> None:
        assert brand.SERIES[0] == brand.ORANGE
        assert len(set(brand.SERIES)) == len(brand.SERIES)


class TestRgba:
    @pytest.mark.parametrize(
        ("hex_color", "alpha", "expected"),
        [
            ("#FF6E42", 255, [255, 110, 66, 255]),
            ("#171B26", 70, [23, 27, 38, 70]),
            ("#000000", 0, [0, 0, 0, 0]),
            ("ffffff", 128, [255, 255, 255, 128]),
        ],
    )
    def test_hex_to_deck_gl_colour(self, hex_color: str, alpha: int, expected: list[int]) -> None:
        assert brand.rgba(hex_color, alpha) == expected

    def test_opaque_by_default(self) -> None:
        assert brand.rgba(brand.ORANGE) == [255, 110, 66, 255]


class TestBadges:
    @pytest.mark.parametrize(("status", "color"), list(brand.STATUS_BADGES.items()))
    def test_status_badge(self, status: str, color: str) -> None:
        assert brand.status_badge(status) == f":{color}-badge[{status}]"

    @pytest.mark.parametrize("status", ["unknown", "", "Evicted"])
    def test_unlisted_status_is_gray(self, status: str) -> None:
        assert brand.status_badge(status) == f":gray-badge[{status}]"

    @pytest.mark.parametrize(("health", "color"), list(brand.HEALTH_BADGES.items()))
    def test_health_badge(self, health: str, color: str) -> None:
        assert brand.health_badge(health) == f":{color}-badge[{health}]"

    def test_unlisted_health_is_gray(self) -> None:
        assert brand.health_badge("in-progress") == ":gray-badge[in-progress]"


@pytest.fixture
def endpoints_document(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Any], list[str]]]:
    """Serve a hand-written botocore ``endpoints`` document to ``regions``.

    Returns an installer; each call clears the cached descriptions and
    returns the list of data names botocore was asked for, so a test can
    count the reads. The real data is restored (and the cache cleared) after.
    """

    def install(document: Any) -> list[str]:
        reads: list[str] = []

        def get_data(name: str) -> Any:
            reads.append(name)
            return document

        monkeypatch.setattr(
            regions, "get_botocore_session", lambda: SimpleNamespace(get_data=get_data)
        )
        regions._descriptions.cache_clear()
        return reads

    yield install
    regions._descriptions.cache_clear()


ODD_ENDPOINTS = {
    "partitions": [
        {
            "partition": "aws",
            "regions": {
                "xx-east-1": {"description": "Example East (Springfield)"},
                "xx-west-1": {"description": "Example West"},
                "xx-blank-1": {"description": ""},
                "xx-null-1": {"description": None},
                "xx-number-1": {"description": 7},
                "xx-bare-1": {},
                "xx-odd-1": "not an object",
            },
        },
        {"partition": "aws-without-regions"},
        {
            "partition": "aws-second",
            "regions": {"xx-south-1": {"description": "Example (Shelbyville)"}},
        },
    ]
}


class TestRegionDescriptions:
    def test_keeps_only_regions_with_a_text_description(
        self, endpoints_document: Callable[[Any], list[str]]
    ) -> None:
        endpoints_document(ODD_ENDPOINTS)
        assert regions._descriptions() == {
            "xx-east-1": "Example East (Springfield)",
            "xx-west-1": "Example West",
            "xx-south-1": "Example (Shelbyville)",
        }

    def test_document_without_partitions_names_nothing(
        self, endpoints_document: Callable[[Any], list[str]]
    ) -> None:
        endpoints_document({})
        assert regions._descriptions() == {}
        assert regions.region_info("us-east-1").name == "us-east-1"

    def test_botocore_is_read_once_per_process(
        self, endpoints_document: Callable[[Any], list[str]]
    ) -> None:
        reads = endpoints_document(ODD_ENDPOINTS)
        for code in ("xx-east-1", "xx-west-1", "us-east-1", "xx-east-1"):
            regions.region_label(code)
        assert reads == ["endpoints"]

    def test_city_is_the_parenthesized_part_of_the_name(
        self, endpoints_document: Callable[[Any], list[str]]
    ) -> None:
        endpoints_document(ODD_ENDPOINTS)
        info = regions.region_info("xx-east-1")
        assert (info.name, info.city) == ("Example East (Springfield)", "Springfield")
        assert regions.region_label("xx-east-1") == "xx-east-1 · Springfield"

    def test_name_without_parentheses_is_its_own_city(
        self, endpoints_document: Callable[[Any], list[str]]
    ) -> None:
        endpoints_document(ODD_ENDPOINTS)
        info = regions.region_info("xx-west-1")
        assert (info.name, info.city) == ("Example West", "Example West")
        assert regions.region_label("xx-west-1") == "xx-west-1 · Example West"

    @pytest.mark.parametrize("code", ["xx-blank-1", "xx-null-1", "xx-bare-1", "xx-odd-1"])
    def test_region_without_a_description_is_labelled_by_code(
        self, endpoints_document: Callable[[Any], list[str]], code: str
    ) -> None:
        endpoints_document(ODD_ENDPOINTS)
        info = regions.region_info(code)
        assert (info.name, info.city) == (code, code)
        assert regions.region_label(code) == code

    def test_named_region_missing_from_the_table_is_left_off_the_map(
        self, endpoints_document: Callable[[Any], list[str]]
    ) -> None:
        endpoints_document(ODD_ENDPOINTS)
        info = regions.region_info("xx-south-1")
        assert (info.latitude, info.longitude, info.on_map) == (None, None, False)

    def test_coordinates_do_not_depend_on_botocore(
        self, endpoints_document: Callable[[Any], list[str]]
    ) -> None:
        endpoints_document({"partitions": []})
        info = regions.region_info("us-east-1")
        assert (info.name, info.latitude, info.longitude) == ("us-east-1", 39.04, -77.49)
        assert info.on_map


class TestRegionInfo:
    def test_published_region(self) -> None:
        assert regions.region_info("us-east-1") == regions.RegionInfo(
            code="us-east-1",
            name="US East (N. Virginia)",
            city="N. Virginia",
            latitude=39.04,
            longitude=-77.49,
        )

    @pytest.mark.parametrize(
        ("code", "label"),
        [
            ("us-east-1", "us-east-1 · N. Virginia"),
            ("us-west-2", "us-west-2 · Oregon"),
            ("eu-west-1", "eu-west-1 · Ireland"),
            ("ap-northeast-1", "ap-northeast-1 · Tokyo"),
        ],
    )
    def test_label_pairs_code_and_city(self, code: str, label: str) -> None:
        assert regions.region_label(code) == label

    def test_unknown_code_keeps_the_code_and_has_no_place(self) -> None:
        info = regions.region_info("zz-nowhere-9")
        assert info == regions.RegionInfo(
            code="zz-nowhere-9",
            name="zz-nowhere-9",
            city="zz-nowhere-9",
            latitude=None,
            longitude=None,
        )
        assert not info.on_map
        assert regions.region_label("zz-nowhere-9") == "zz-nowhere-9"

    @pytest.mark.parametrize(
        ("latitude", "longitude", "on_map"),
        [
            (0.0, 0.0, True),
            (45.5, -73.6, True),
            (None, -73.6, False),
            (45.5, None, False),
            (None, None, False),
        ],
    )
    def test_on_map_needs_both_coordinates(
        self, latitude: float | None, longitude: float | None, on_map: bool
    ) -> None:
        info = regions.RegionInfo("xx-1", "xx-1", "xx-1", latitude, longitude)
        assert info.on_map is on_map


class TestCoordinateTable:
    def test_every_placed_region_is_one_aws_publishes(self) -> None:
        # A typo in the table would place a Region no deployment can have.
        assert sorted(set(regions.REGION_COORDINATES) - set(regions._descriptions())) == []

    def test_coordinates_are_latitude_then_longitude(self) -> None:
        # The Americas are west of Greenwich and Asia, Africa and the Middle
        # East east of it, so a swapped pair lands in the wrong hemisphere.
        west = ("us-", "ca-", "mx-", "sa-")
        east = ("ap-", "cn-", "me-", "il-", "af-")
        misplaced = {
            code: (latitude, longitude)
            for code, (latitude, longitude) in regions.REGION_COORDINATES.items()
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180)
            or (code.startswith(west) and longitude >= 0)
            or (code.startswith(east) and longitude <= 0)
        }
        assert misplaced == {}
