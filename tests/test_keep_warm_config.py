"""The keep-warm GPU pool's settings and idle-record format (gco.keep_warm).

``gpu_keep_warm.window`` is how long Karpenter keeps an empty node of
``gpu-keep-warm-pool`` after its last pod. It renders into the NodePool's
``consolidateAfter`` and the health monitor's environment, so it must be a
duration Karpenter accepts and a bounded one. The idle records the health
monitor writes onto Jobs are read back by Get Job; a malformed annotation must
never turn a job read into an error.
"""

from __future__ import annotations

import json
import types
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from gco.config.config_loader import ConfigLoader, ConfigValidationError
from gco.keep_warm import (
    IDLE_ANNOTATION,
    KEEP_WARM_CONTEXT_KEY,
    KEEP_WARM_DEFAULTS,
    KEEP_WARM_MAX_WINDOW_SECONDS,
    KEEP_WARM_MIN_WINDOW_SECONDS,
    KEEP_WARM_WINDOW_PLACEHOLDER,
    KEEP_WARM_WINDOW_SECONDS_PLACEHOLDER,
    MAX_IDLE_RECORDS,
    KeepWarmConfigError,
    compute_keep_warm_replacements,
    dump_idle_records,
    format_time,
    keep_warm_summary,
    load_idle_records,
    parse_time,
    render_keep_warm_defaults,
    validate_keep_warm_config,
    window_seconds,
)

ROOT = Path(__file__).resolve().parent.parent
SHIPPED_CDK_JSON = ROOT / "cdk.json"


class TestWindowSeconds:
    @pytest.mark.parametrize(
        ("window", "seconds"),
        [("30s", 30), ("10m", 600), ("1h", 3600), ("1h30m", 5400), ("2h0m15s", 7215)],
    )
    def test_durations_of_hours_minutes_and_seconds(self, window: str, seconds: int) -> None:
        assert window_seconds(window) == seconds

    @pytest.mark.parametrize("window", ["", "10", "10 m", "Never", "1d", "-5m", "10M", "5m3"])
    def test_anything_else_is_refused(self, window: str) -> None:
        with pytest.raises(KeepWarmConfigError, match=r"gpu_keep_warm\.window must be a duration"):
            window_seconds(window)


class TestValidateKeepWarmConfig:
    def test_absent_block_is_the_ten_minute_default(self) -> None:
        assert validate_keep_warm_config(None) == {"window": "10m", "window_seconds": 600}
        assert KEEP_WARM_DEFAULTS == {"window": "10m"}

    def test_comment_keys_are_skipped(self) -> None:
        config = validate_keep_warm_config({"_comment": "notes", "window": "1h"})
        assert config == {"window": "1h", "window_seconds": 3600}

    def test_bounds_are_inclusive(self) -> None:
        assert validate_keep_warm_config({"window": "30s"})["window_seconds"] == (
            KEEP_WARM_MIN_WINDOW_SECONDS
        )
        assert validate_keep_warm_config({"window": "24h"})["window_seconds"] == (
            KEEP_WARM_MAX_WINDOW_SECONDS
        )

    @pytest.mark.parametrize("window", ["29s", "24h1s", "25h"])
    def test_a_window_outside_the_bounds_is_refused(self, window: str) -> None:
        with pytest.raises(KeepWarmConfigError, match="between 30 seconds and 24 hours"):
            validate_keep_warm_config({"window": window})

    @pytest.mark.parametrize("window", [600, 10.0, True, None, ["10m"]])
    def test_a_non_string_window_is_refused(self, window: object) -> None:
        with pytest.raises(KeepWarmConfigError, match="must be a string duration"):
            validate_keep_warm_config({"window": window})

    def test_an_unknown_key_is_named_with_its_path(self) -> None:
        with pytest.raises(
            KeepWarmConfigError,
            match=r"unknown key\(s\): gpu_keep_warm.windw; allowed keys: gpu_keep_warm.window",
        ):
            validate_keep_warm_config({"windw": "10m"})

    @pytest.mark.parametrize("raw", ["10m", ["10m"], 10])
    def test_a_block_that_is_not_an_object_is_refused(self, raw: object) -> None:
        with pytest.raises(KeepWarmConfigError, match="gpu_keep_warm must be an object"):
            validate_keep_warm_config(raw)


class TestReplacements:
    def test_render_both_tokens(self) -> None:
        config = validate_keep_warm_config({"window": "1h30m"})
        assert compute_keep_warm_replacements(config) == {
            KEEP_WARM_WINDOW_PLACEHOLDER: "1h30m",
            KEEP_WARM_WINDOW_SECONDS_PLACEHOLDER: "5400",
        }

    def test_default_rendering_for_readers_other_than_the_applier(self) -> None:
        text = f"a: {KEEP_WARM_WINDOW_PLACEHOLDER}\nb: {KEEP_WARM_WINDOW_SECONDS_PLACEHOLDER}\n"
        assert render_keep_warm_defaults(text) == "a: 10m\nb: 600\n"


class TestConfigLoader:
    @staticmethod
    def _loader(context: dict[str, Any]) -> ConfigLoader:
        loader = ConfigLoader.__new__(ConfigLoader)
        loader.app = types.SimpleNamespace(
            node=types.SimpleNamespace(try_get_context=lambda key: context.get(key))
        )
        return loader

    def test_shipped_cdk_json_sets_the_default_window(self) -> None:
        context = json.loads(SHIPPED_CDK_JSON.read_text(encoding="utf-8"))["context"]
        assert context[KEEP_WARM_CONTEXT_KEY] == {"window": "10m"}
        assert context["_comment_gpu_keep_warm"]
        assert self._loader(context).get_gpu_keep_warm_config() == {
            "window": "10m",
            "window_seconds": 600,
        }

    def test_an_absent_block_is_the_default(self) -> None:
        assert self._loader({}).get_gpu_keep_warm_config()["window"] == "10m"

    def test_an_invalid_block_is_a_config_validation_error(self) -> None:
        with pytest.raises(ConfigValidationError, match=r"gpu_keep_warm\.window"):
            self._loader({KEEP_WARM_CONTEXT_KEY: {"window": "forever"}}).get_gpu_keep_warm_config()


_T0 = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)


def _record(node: str = "node-a", *, closed: bool = False, offset: int = 0) -> dict[str, Any]:
    since = _T0 + timedelta(seconds=offset)
    entry: dict[str, Any] = {"node": node, "since": format_time(since), "pod": "trial-1"}
    if closed:
        entry.update(
            until=format_time(since + timedelta(seconds=90)), seconds=90, ended_by="reused"
        )
    return entry


class TestTimes:
    def test_format_is_utc_with_a_z_and_second_precision(self) -> None:
        moment = datetime(2026, 11, 21, 13, 0, 5, 750000, tzinfo=timezone(timedelta(hours=-5)))
        assert format_time(moment) == "2026-11-21T18:00:05Z"

    def test_parse_reads_z_offsets_and_naive_times_as_utc(self) -> None:
        assert parse_time("2026-11-21T18:00:05Z") == datetime(2026, 11, 21, 18, 0, 5, tzinfo=UTC)
        assert parse_time("2026-11-21T18:00:05") == datetime(2026, 11, 21, 18, 0, 5, tzinfo=UTC)

    @pytest.mark.parametrize("value", [None, "", "yesterday", 5, "2026-13-01T00:00:00Z"])
    def test_parse_returns_none_for_anything_unreadable(self, value: object) -> None:
        assert parse_time(value) is None


class TestIdleRecords:
    def test_round_trip(self) -> None:
        records = [_record(closed=True), _record("node-b")]
        annotations = {IDLE_ANNOTATION: dump_idle_records(records)}
        assert load_idle_records(annotations) == records

    @pytest.mark.parametrize(
        "annotations",
        [
            None,
            {},
            {IDLE_ANNOTATION: 5},
            {IDLE_ANNOTATION: "not json"},
            {IDLE_ANNOTATION: '{"node": "n"}'},
        ],
        ids=["no-annotations", "absent", "not-a-string", "not-json", "not-a-list"],
    )
    def test_an_unreadable_annotation_is_no_records(self, annotations: Any) -> None:
        assert load_idle_records(annotations) == []

    def test_malformed_entries_are_dropped(self) -> None:
        good = _record()
        raw = json.dumps([good, "junk", {"since": good["since"]}, {"node": "n", "since": "x"}])
        assert load_idle_records({IDLE_ANNOTATION: raw}) == [good]

    def test_dump_drops_the_oldest_closed_records_first(self) -> None:
        records = [_record(f"n{i}", closed=True, offset=i) for i in range(MAX_IDLE_RECORDS)]
        records.append(_record("open-node", offset=999))
        records.append(_record("newest-closed", closed=True, offset=1000))

        kept = json.loads(dump_idle_records(records))

        assert len(kept) == MAX_IDLE_RECORDS
        assert [entry["node"] for entry in kept[:2]] == ["n2", "n3"]
        assert {"open-node", "newest-closed"} <= {entry["node"] for entry in kept}

    def test_dump_keeps_open_records_even_past_the_bound(self) -> None:
        records = [_record(f"n{i}", offset=i) for i in range(MAX_IDLE_RECORDS + 3)]
        assert len(json.loads(dump_idle_records(records))) == MAX_IDLE_RECORDS + 3

    def test_summary_totals_the_closed_intervals(self) -> None:
        records = [_record("a", closed=True), _record("b", closed=True), _record("c")]
        summary = keep_warm_summary({IDLE_ANNOTATION: dump_idle_records(records)})
        assert summary == {"idle": records, "idle_seconds": 180, "open": True}

    def test_summary_of_closed_records_only_is_not_open(self) -> None:
        summary = keep_warm_summary({IDLE_ANNOTATION: dump_idle_records([_record(closed=True)])})
        assert summary is not None
        assert summary["open"] is False

    def test_no_records_is_no_summary(self) -> None:
        assert keep_warm_summary({"other": "annotation"}) is None
