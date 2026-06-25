"""
Capacity Block search primitives: duration math, instance-type normalization,
offering pricing, and de-dup / sort helpers.

EC2 Capacity Blocks for ML (the ``DescribeCapacityBlockOfferings`` API) accept
reservation durations in **1-day increments up to 14 days, then 7-day increments
up to 182 days** (26 weeks). ``CapacityDurationHours`` is a *required* parameter,
so searching a duration *range* — or asking "what's the longest block available?"
— means probing several discrete duration values and merging the results. These
helpers centralize that math, plus the offering enrichment (per-hour and
per-GPU-hour pricing) and the de-dup / sort the search layer applies across the
region x duration probe matrix.

Everything here is pure (no boto3, no I/O) so it is cheap to unit-test in
isolation. The single-region API call, the parallel fan-out, and the instance
validation that needs AWS live in ``checker.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Duration math
# ---------------------------------------------------------------------------

#: Default Capacity Block search duration (24h = 1 day), the smallest valid block.
DEFAULT_DURATION_HOURS = 24

#: AWS caps a single Capacity Block at 182 days (26 weeks).
MAX_DURATION_DAYS = 182

#: Durations are expressed in 1-day increments up to this many days...
_DAILY_INCREMENT_MAX_DAYS = 14

#: ...then in 7-day increments beyond that, up to ``MAX_DURATION_DAYS``.
_WEEKLY_INCREMENT_DAYS = 7

HOURS_PER_DAY = 24


def capacity_block_duration_days() -> tuple[int, ...]:
    """Return every Capacity Block duration AWS allows, in days, ascending.

    1..14 in 1-day steps, then 21, 28, ... 182 in 7-day steps.
    """
    daily = list(range(1, _DAILY_INCREMENT_MAX_DAYS + 1))
    weekly = list(
        range(
            _DAILY_INCREMENT_MAX_DAYS + _WEEKLY_INCREMENT_DAYS,
            MAX_DURATION_DAYS + 1,
            _WEEKLY_INCREMENT_DAYS,
        )
    )
    return tuple(daily + weekly)


def capacity_block_duration_hours() -> tuple[int, ...]:
    """Return every allowed Capacity Block duration, in hours, ascending."""
    return tuple(d * HOURS_PER_DAY for d in capacity_block_duration_days())


def is_valid_duration_hours(hours: int) -> bool:
    """True if ``hours`` is exactly one of the AWS-allowed Capacity Block durations."""
    return hours in capacity_block_duration_hours()


def snap_duration_hours(hours: int) -> int:
    """Snap an arbitrary hour count to the nearest valid Capacity Block duration.

    Ties (equidistant between two valid durations) round up to the longer one so
    callers never under-request. Values below/above the allowed range clamp to
    the smallest/largest valid duration.
    """
    options = capacity_block_duration_hours()
    if hours <= options[0]:
        return options[0]
    if hours >= options[-1]:
        return options[-1]
    # Nearest, ties → longer.
    return min(options, key=lambda opt: (abs(opt - hours), -opt))


def coerce_hours(hours: int | None, days: int | None) -> int | None:
    """Resolve a duration to hours from an hours-or-days pair.

    ``days`` wins when both are supplied so a caller that passes ``duration_days``
    always gets day-granularity. Returns ``None`` when neither is set.
    """
    if days is not None:
        return days * HOURS_PER_DAY
    return hours


def resolve_search_durations(
    *,
    duration_hours: int | None = None,
    min_duration_hours: int | None = None,
    max_duration_hours: int | None = None,
    find_longest: bool = False,
) -> list[int]:
    """Resolve the set of Capacity Block durations (in hours) to probe.

    Precedence:

    * A min/max range (either bound) — or ``find_longest`` — expands to *every*
      valid duration within the (possibly open-ended) range. ``find_longest``
      without a range sweeps the full 1-day..182-day ladder.
    * A single ``duration_hours`` snaps to the nearest valid duration.
    * Otherwise the default 24h block.

    The result is always non-empty and ascending so the caller can probe each
    value and let the search layer pick the longest / cheapest.
    """
    options = capacity_block_duration_hours()
    has_range = min_duration_hours is not None or max_duration_hours is not None

    if has_range or find_longest:
        low = min_duration_hours if min_duration_hours is not None else options[0]
        high = max_duration_hours if max_duration_hours is not None else options[-1]
        if low > high:
            low, high = high, low
        chosen = [h for h in options if low <= h <= high]
        # A tight range that straddles no valid duration (e.g. 30h..40h) still
        # gets a single best-effort probe rather than an empty search.
        return chosen or [snap_duration_hours((low + high) // 2)]

    if duration_hours is not None:
        return [snap_duration_hours(duration_hours)]

    return [DEFAULT_DURATION_HOURS]


def hours_to_days(hours: int | None) -> float | None:
    """Convert a duration in hours to days (float), or ``None`` passthrough."""
    if hours is None:
        return None
    return round(hours / HOURS_PER_DAY, 2)


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


def parse_date_input(value: str | datetime | None) -> datetime | None:
    """Parse a date/datetime input into a timezone-aware UTC ``datetime``.

    Accepts ``None`` (passthrough), a ``datetime`` (naive values are assumed
    UTC), an ISO-8601 string (``2026-07-01`` or ``2026-07-01T11:30:00Z``), and
    tolerates a trailing ``Z``. Raises ``ValueError`` with a friendly message on
    anything else so the CLI can surface a clear error.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"Invalid date {value!r}; use YYYY-MM-DD or an ISO-8601 timestamp "
            "(e.g. 2026-07-01 or 2026-07-01T11:30:00Z)."
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Instance-type normalization
# ---------------------------------------------------------------------------

#: Friendly / shorthand names mapped to their canonical EC2 instance type. Lets a
#: caller say ``p6-b200`` and get the full ``p6-b200.48xlarge``.
INSTANCE_TYPE_ALIASES: dict[str, str] = {
    "p6-b200": "p6-b200.48xlarge",
    "p6b200": "p6-b200.48xlarge",
    "b200": "p6-b200.48xlarge",
    "p6-b300": "p6-b300.48xlarge",
    "p6b300": "p6-b300.48xlarge",
    "b300": "p6-b300.48xlarge",
    "p5": "p5.48xlarge",
    "p5e": "p5e.48xlarge",
    "p5en": "p5en.48xlarge",
    "p4d": "p4d.24xlarge",
    "p4de": "p4de.24xlarge",
    "p3dn": "p3dn.24xlarge",
}

#: Accelerator families that are NOT sold as standalone EC2 instance types — the
#: Grace-Blackwell *superchips* (GB200/GB300) ship only as P6e-GB UltraServers, so
#: ``DescribeCapacityBlockOfferings(InstanceType=…)`` never returns them. The note
#: steers callers to the UltraServer search flow and to the standalone B200/B300
#: EC2 types where one exists. NB: the discrete ``b200``/``b300`` GPUs *are* sold
#: as standalone ``p6-b200.48xlarge`` / ``p6-b300.48xlarge`` instances (see
#: ``INSTANCE_TYPE_ALIASES``) — only the GB NVL72 superchips are UltraServer-only.
NON_STANDALONE_INSTANCE_NOTES: dict[str, str] = {
    "p6e-gb300": (
        "P6e-GB300 is an UltraServer family, not a standalone EC2 instance type. "
        "Search Capacity Blocks with UltraserverType=u-p6e-gb300x... instead of "
        "InstanceType. For a standalone Blackwell Ultra EC2 type use p6-b300.48xlarge."
    ),
    "gb300": (
        "GB300 (Grace Blackwell Ultra NVL72) ships as P6e-GB300 UltraServers, not "
        "a standalone EC2 instance type. For standalone Blackwell Ultra EC2 "
        "capacity use p6-b300.48xlarge instead."
    ),
    "p6e-gb200": (
        "P6e-GB200 is an UltraServer family, not a standalone EC2 instance type. "
        "Search Capacity Blocks with UltraserverType=u-p6e-gb200x... instead of "
        "InstanceType. For a standalone Blackwell EC2 type use p6-b200.48xlarge."
    ),
    "gb200": (
        "GB200 (Grace Blackwell NVL72) ships as P6e-GB200 UltraServers. For "
        "standalone Blackwell EC2 capacity use p6-b200.48xlarge instead."
    ),
}


def normalize_instance_type(name: str) -> tuple[str, str | None]:
    """Normalize an instance-type string to its canonical form.

    Returns ``(canonical_type, note)`` where ``note`` is a human-readable string
    when the input was expanded from an alias or names a not-standalone
    UltraServer family (otherwise ``None``). The canonical type is returned
    unchanged for an already-canonical or unknown input so the caller can still
    let the EC2 API be the source of truth on validity.
    """
    raw = (name or "").strip()
    lowered = raw.lower()

    note = NON_STANDALONE_INSTANCE_NOTES.get(lowered)
    if note:
        return raw, note

    canonical = INSTANCE_TYPE_ALIASES.get(lowered)
    if canonical and canonical != raw:
        return canonical, f"Interpreted '{raw}' as '{canonical}'."

    return raw, None


# ---------------------------------------------------------------------------
# Offering pricing
# ---------------------------------------------------------------------------


def parse_upfront_fee(value: Any) -> float | None:
    """Parse an upfront-fee value (the API returns it as a string) to a float.

    Returns ``None`` for missing/unparseable values rather than raising, so a
    malformed fee never breaks the whole search.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is a subclass of int
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).strip())
    except TypeError, ValueError:
        return None


def compute_offering_pricing(
    upfront_fee: Any,
    duration_hours: int | None,
    instance_count: int | None,
    gpus_per_instance: int | None,
) -> dict[str, float | None]:
    """Derive per-hour and per-GPU-hour pricing from a block's upfront fee.

    ``upfront_fee`` is the total one-time charge (USD) for the whole block — all
    instances for the whole duration. The derived figures:

    * ``upfront_fee_usd`` — the parsed total upfront fee.
    * ``price_per_hour`` — whole-block amortized cost per hour.
    * ``price_per_instance_hour`` — per-instance amortized cost per hour.
    * ``price_per_gpu_hour`` — per-GPU amortized cost per hour (needs GPU count).

    Any figure that can't be computed (missing fee, zero duration/count, unknown
    GPU count) is ``None`` rather than a misleading zero.
    """
    fee = parse_upfront_fee(upfront_fee)
    result: dict[str, float | None] = {
        "upfront_fee_usd": fee,
        "price_per_hour": None,
        "price_per_instance_hour": None,
        "price_per_gpu_hour": None,
    }
    if fee is None or not duration_hours or duration_hours <= 0:
        return result

    result["price_per_hour"] = round(fee / duration_hours, 4)

    if instance_count and instance_count > 0:
        per_instance_hour = fee / (duration_hours * instance_count)
        result["price_per_instance_hour"] = round(per_instance_hour, 4)
        if gpus_per_instance and gpus_per_instance > 0:
            result["price_per_gpu_hour"] = round(per_instance_hour / gpus_per_instance, 4)

    return result


# ---------------------------------------------------------------------------
# De-dup / sort
# ---------------------------------------------------------------------------


def offering_identity(offering: dict[str, Any]) -> tuple[Any, ...]:
    """Stable identity for an offering used to de-dup across duration probes.

    Prefers the AWS offering id; falls back to the (region, AZ, start, duration)
    tuple when an id is absent (e.g. partially-mocked test data).
    """
    offering_id = offering.get("offering_id")
    if offering_id:
        return ("id", offering_id)
    return (
        "tuple",
        offering.get("region"),
        offering.get("availability_zone"),
        offering.get("start_date"),
        offering.get("duration_hours"),
    )


def dedupe_offerings(offerings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate offerings (same identity) while preserving first-seen order.

    Probing adjacent durations against the same date window routinely returns the
    same physical block more than once; this collapses those to a single entry.
    """
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for offering in offerings:
        identity = offering_identity(offering)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(offering)
    return unique


def sort_offerings(offerings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort offerings by region, then AZ, then start date — a stable browse order."""
    return sorted(
        offerings,
        key=lambda o: (
            str(o.get("region") or ""),
            str(o.get("availability_zone") or ""),
            str(o.get("start_date") or ""),
        ),
    )


def _rank_key(offering: dict[str, Any]) -> tuple[float, float, str]:
    gpu_hour = offering.get("price_per_gpu_hour")
    per_hour = offering.get("price_per_hour")
    return (
        float(gpu_hour) if gpu_hour is not None else float("inf"),
        float(per_hour) if per_hour is not None else float("inf"),
        str(offering.get("start_date") or ""),
    )


def rank_offerings(offerings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank offerings cheapest-first by per-GPU-hour, then per-hour, then start date.

    Offerings missing a price sort last. Used to surface the single best block in
    a consolidated multi-region report.
    """
    return sorted(offerings, key=_rank_key)


def longest_offering(offerings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the offering with the greatest actual duration, or ``None`` if empty.

    Ties break toward the cheaper per-GPU-hour block so "find longest" still
    favours value when two blocks share the top duration.
    """
    if not offerings:
        return None
    return max(
        offerings,
        key=lambda o: (
            int(o.get("duration_hours") or 0),
            -_rank_key(o)[0],
        ),
    )
