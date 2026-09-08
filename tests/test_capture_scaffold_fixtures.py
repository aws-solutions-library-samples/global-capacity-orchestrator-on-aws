"""Hermetic tests for broad Bedrock scaffold-fixture discovery."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import capture_scaffold_fixtures as capture


def _model(
    model_id: str,
    *,
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
    inference: list[str] | None = None,
    status: str = "ACTIVE",
) -> dict[str, Any]:
    return {
        "modelId": model_id,
        "inputModalities": inputs or ["TEXT"],
        "outputModalities": outputs or ["TEXT"],
        "inferenceTypesSupported": inference or ["ON_DEMAND"],
        "modelLifecycle": {"status": status},
    }


class _CatalogClient:
    def __init__(self) -> None:
        self.profile_calls: list[dict[str, Any]] = []

    def list_foundation_models(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"byOutputModality": "TEXT"}
        return {
            "modelSummaries": [
                _model("amazon.nova-pro-v1:0", inference=["INFERENCE_PROFILE"]),
                _model("anthropic.claude-fable-5", inference=["INFERENCE_PROFILE"]),
                _model("qwen.qwen-test", inference=["ON_DEMAND"]),
                _model("cohere.embed-v4:0"),
                _model("openai.gpt-oss-safeguard-20b"),
                _model("inactive.model", status="LEGACY"),
                _model("image.only", inputs=["IMAGE"]),
            ]
        }

    def list_inference_profiles(self, **kwargs: Any) -> dict[str, Any]:
        self.profile_calls.append(kwargs)
        if "nextToken" not in kwargs:
            return {
                "inferenceProfileSummaries": [
                    {
                        "status": "ACTIVE",
                        "inferenceProfileId": "us.anthropic.claude-fable-5",
                        "models": [
                            {
                                "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-fable-5"
                            }
                        ],
                    },
                    {
                        "status": "ACTIVE",
                        "inferenceProfileId": "us.amazon.nova-pro-v1:0",
                        "models": [
                            {
                                "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"
                            }
                        ],
                    },
                ],
                "nextToken": "page-2",
            }
        assert kwargs["nextToken"] == "page-2"
        return {
            "inferenceProfileSummaries": [
                {
                    "status": "ACTIVE",
                    "inferenceProfileId": "global.anthropic.claude-fable-5",
                    "models": [
                        {"modelArn": "arn:aws:bedrock:::foundation-model/anthropic.claude-fable-5"}
                    ],
                }
            ]
        }


def test_text_generation_candidate_filter_rejects_non_generation_models() -> None:
    assert capture._is_text_generation_candidate(_model("qwen.qwen-test")) is True
    assert capture._is_text_generation_candidate(_model("cohere.embed-v4:0")) is False
    assert capture._is_text_generation_candidate(_model("openai.gpt-oss-safeguard-20b")) is False
    assert capture._is_text_generation_candidate(_model("legacy.model", status="LEGACY")) is False
    assert capture._is_text_generation_candidate(_model("image.only", inputs=["IMAGE"])) is False
    assert (
        capture._is_text_generation_candidate(_model("text.to.image", outputs=["IMAGE"])) is False
    )


def test_discovery_deduplicates_profiles_skips_existing_and_keeps_curated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    existing = tmp_path / "us_amazon_nova_pro_v1_0.json"
    existing.write_text(json.dumps({"model_id": "us.amazon.nova-pro-v1:0"}), encoding="utf-8")
    monkeypatch.setattr(
        capture,
        "_default_models",
        lambda: ("us.anthropic.legacy-v1:0",),
    )
    client = _CatalogClient()

    models = capture._discover_all_models(
        "us-east-1",
        tmp_path,
        bedrock_client=client,
    )

    assert models == (
        "qwen.qwen-test",
        "global.anthropic.claude-fable-5",
        "us.anthropic.legacy-v1:0",
    )
    assert client.profile_calls == [
        {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000},
        {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000, "nextToken": "page-2"},
    ]


def test_parser_exposes_discovery_and_list_only_modes() -> None:
    parser = capture._build_parser()
    args = parser.parse_args(["--discover-all-models", "--list-candidates"])
    assert args.discover_all_models is True
    assert args.list_candidates is True
    assert args.models is None
    assert args.read_timeout_seconds is None

    custom = parser.parse_args(
        [
            "--discover-all-models",
            "--read-timeout-seconds",
            "45",
            "--workers",
            "3",
        ]
    )
    assert custom.read_timeout_seconds == 45
    assert custom.workers == 3

    with pytest.raises(SystemExit):
        parser.parse_args(["--discover-all-models", "--model", "example.model"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--read-timeout-seconds", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--workers", "0"])


def test_explicit_models_remain_ordered_and_deduplicated(tmp_path: Path) -> None:
    args = argparse.Namespace(
        discover_all_models=False,
        models=["model.b", "model.a", "model.b"],
        region="us-east-1",
        output_dir=tmp_path,
        list_candidates=False,
    )
    assert capture._selected_models(args) == ("model.b", "model.a")


def test_list_candidates_makes_no_model_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        capture,
        "_discover_all_models",
        lambda region, output_dir: ("model.one", "model.two"),
    )

    async def _unexpected_capture(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("list-only mode must not invoke a model")

    monkeypatch.setattr(capture, "_capture_model", _unexpected_capture)
    args = argparse.Namespace(
        discover_all_models=True,
        models=None,
        region="us-east-1",
        output_dir=tmp_path,
        list_candidates=True,
    )

    assert asyncio.run(capture._main_async(args)) == 0
    output = capsys.readouterr().out
    assert "Selected 2 model candidate(s)" in output
    assert "up to 6 paid Converse calls" in output
    assert output.endswith("model.one\nmodel.two\n")


def test_discovery_capture_uses_a_bounded_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        capture,
        "_discover_all_models",
        lambda region, output_dir: ("model.one",),
    )
    observed: list[tuple[str, int | None]] = []

    async def _capture(
        model_id: str,
        _region: str,
        _output_dir: Path,
        *,
        read_timeout_seconds: int | None,
    ) -> bool:
        observed.append((model_id, read_timeout_seconds))
        return True

    monkeypatch.setattr(capture, "_capture_model", _capture)
    args = argparse.Namespace(
        discover_all_models=True,
        models=None,
        region="us-east-1",
        output_dir=tmp_path,
        list_candidates=False,
        read_timeout_seconds=None,
    )

    assert asyncio.run(capture._main_async(args)) == 0
    assert observed == [("model.one", 300)]


def test_discovery_captures_models_with_bounded_concurrency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    models = tuple(f"model.{index}" for index in range(5))
    monkeypatch.setattr(
        capture,
        "_discover_all_models",
        lambda region, output_dir: models,
    )
    active = 0
    maximum_active = 0
    seen: list[str] = []

    async def _capture(
        model_id: str,
        _region: str,
        _output_dir: Path,
        *,
        read_timeout_seconds: int | None,
    ) -> bool:
        nonlocal active, maximum_active
        assert read_timeout_seconds == 300
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        seen.append(model_id)
        active -= 1
        return True

    monkeypatch.setattr(capture, "_capture_model", _capture)
    args = argparse.Namespace(
        discover_all_models=True,
        models=None,
        region="us-east-1",
        output_dir=tmp_path,
        list_candidates=False,
        read_timeout_seconds=None,
        workers=2,
    )

    assert asyncio.run(capture._main_async(args)) == 0
    assert maximum_active == 2
    assert sorted(seen) == list(models)
