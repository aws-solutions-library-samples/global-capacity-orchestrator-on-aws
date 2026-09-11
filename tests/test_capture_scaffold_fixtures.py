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


# ---------------------------------------------------------------------------
# Capture path: backend construction, per-directive capture, the atomic
# per-model fixture write, the serial/concurrent runners and the CLI entry.
# Every Bedrock touchpoint (``boto3.client``, ``boto3.Session``, the sampling
# backend's ``sample``) is replaced; the parse/normalize/validate pipeline the
# script gates captures through is the real production code.
# ---------------------------------------------------------------------------

import datetime  # noqa: E402 - grouped with the harness it serves
import hashlib  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402
import sys  # noqa: E402
from typing import cast  # noqa: E402

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402

# ``mission.*`` resolves through the sys.path injection the script performs on
# import (``gco_mcp`` is on the path by the time this line runs), so these are
# the very same module objects the script raises and catches with. Importing
# them under the dotted ``gco_mcp.mission`` spelling would create second,
# distinct classes that the script's ``except`` clauses would not recognise.
from mission import criteria_scaffold  # noqa: E402
from mission.sampling import BedrockSamplingBackend, SamplingTransportError  # noqa: E402
from mission.validation import MissionValidationError  # noqa: E402

from gco.bedrock import BedrockFTUFormNotAcceptedError, get_default_mission_model_id  # noqa: E402

# One minimal, validator-clean answer per canonical directive. Each takes the
# preferred shape for its template branch (tool_call_succeeded / metric /
# event) so it survives ``_validate_sampled_criteria_context`` unchanged.
_VALID_RAWS: dict[str, str] = {
    "search_inference_docs": json.dumps(
        [
            {
                "criterion_id": "docs_found",
                "kind": "tool_call_succeeded",
                "required": True,
                "tool_name": "find_docs",
            }
        ]
    ),
    "metric_drive_loss": json.dumps(
        [
            {
                "criterion_id": "loss_ok",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.val_loss",
                "op": "<=",
                "target": 0.1,
            }
        ]
    ),
    "event_goal_reached": json.dumps(
        [
            {
                "criterion_id": "goal",
                "kind": "event",
                "required": True,
                "event_name": "goal_reached",
            }
        ]
    ),
}


def _directive(slug: str) -> Any:
    return next(directive for directive in capture._DIRECTIVES if directive.slug == slug)


class _FakeBackend:
    """Duck-typed ``BedrockSamplingBackend``: answers from a per-directive table.

    Values are raw response strings, or exceptions to raise in place of a
    Converse round trip. The rendered prompt is recorded so tests can pin the
    provenance digest.
    """

    def __init__(self, raws: dict[str, str | BaseException]) -> None:
        self.raws = raws
        self.prompts: list[str] = []

    async def sample(self, prompt: Any) -> str:
        text = prompt.assemble()
        self.prompts.append(text)
        for directive in capture._DIRECTIVES:
            if directive.text in text:
                answer = self.raws[directive.slug]
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        raise AssertionError("prompt did not embed a canonical directive")


def _capture_one(backend: _FakeBackend, slug: str) -> dict[str, Any]:
    """Run ``capture._capture_one`` for one canonical directive against a fake backend."""
    return asyncio.run(
        capture._capture_one(cast(BedrockSamplingBackend, backend), _directive(slug))
    )


def _install_backend(monkeypatch: pytest.MonkeyPatch, backend: _FakeBackend) -> list[Any]:
    seen: list[tuple[str, str, int | None]] = []

    def fake_backend_for_capture(
        model_id: str, region: str, *, read_timeout_seconds: int | None = None
    ) -> _FakeBackend:
        seen.append((model_id, region, read_timeout_seconds))
        return backend

    monkeypatch.setattr(capture, "_backend_for_capture", fake_backend_for_capture)
    return seen


def _run_args(tmp_path: Path, models: list[str], **extra: Any) -> argparse.Namespace:
    fields: dict[str, Any] = {
        "discover_all_models": False,
        "models": models,
        "region": "us-east-1",
        "output_dir": tmp_path / "out",
        "list_candidates": False,
        "read_timeout_seconds": None,
        "workers": None,
    }
    fields.update(extra)
    return argparse.Namespace(**fields)


# --- catalog helpers --------------------------------------------------------


def test_fixture_model_ids_strips_ids_and_rejects_blank_or_missing(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text(json.dumps({"model_id": " us.amazon.nova-lite-v1:0 "}))
    (tmp_path / "b.json").write_text(json.dumps({"model_id": "us.meta.llama"}))
    (tmp_path / "README.md").write_text("not a fixture")
    assert capture._fixture_model_ids(tmp_path) == frozenset(
        {"us.amazon.nova-lite-v1:0", "us.meta.llama"}
    )

    bad = tmp_path / "c.json"
    bad.write_text(json.dumps({"model_id": "   "}))
    with pytest.raises(ValueError, match=r"c\.json: model_id must be a non-empty string"):
        capture._fixture_model_ids(tmp_path)
    bad.write_text(json.dumps({"captures": {}}))
    with pytest.raises(ValueError, match="model_id must be a non-empty string"):
        capture._fixture_model_ids(tmp_path)


def test_profile_preference_orders_global_then_us_then_other_geographies() -> None:
    assert capture._profile_preference("global.meta.llama") == (0, "global.meta.llama")
    assert capture._profile_preference("us.meta.llama") == (1, "us.meta.llama")
    assert capture._profile_preference("eu.meta.llama") == (2, "eu.meta.llama")
    assert sorted(["eu.x", "us.x", "global.x", "apac.x"], key=capture._profile_preference) == [
        "global.x",
        "us.x",
        "apac.x",
        "eu.x",
    ]


def test_discovery_builds_a_bedrock_client_when_none_is_injected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    catalog = _CatalogClient()

    def fake_client(service: str, **kwargs: Any) -> _CatalogClient:
        calls.append((service, kwargs))
        return catalog

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setattr(capture, "_default_models", lambda: ())

    models = capture._discover_all_models("eu-west-1", tmp_path)

    assert calls == [("bedrock", {"region_name": "eu-west-1"})]
    assert models == (
        "qwen.qwen-test",
        "us.amazon.nova-pro-v1:0",
        "global.anthropic.claude-fable-5",
    )


class _EdgeCaseCatalogClient:
    """Catalog shapes the happy-path client does not exercise."""

    def list_foundation_models(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "modelSummaries": [
                # Base already captured: neither the curated global profile
                # nor the live us profile may re-select it.
                _model("amazon.nova-pro-v1:0", inference=["INFERENCE_PROFILE"]),
                # Two live profiles; the us one is seen first and must win
                # over the later, lower-ranked eu one.
                _model("meta.llama-x", inference=["INFERENCE_PROFILE"]),
                # No profile and no on-demand access: nothing can invoke it.
                _model("mistral.solo", inference=["INFERENCE_PROFILE"]),
                # No profile but on-demand: captured under its own id.
                _model("cohere.command-z", inference=["ON_DEMAND"]),
            ]
        }

    def list_inference_profiles(self, **kwargs: Any) -> dict[str, Any]:
        arn = "arn:aws:bedrock:us-east-1::foundation-model/"
        return {
            "inferenceProfileSummaries": [
                {
                    "status": "LEGACY",
                    "inferenceProfileId": "global.meta.llama-x",
                    "models": [{"modelArn": arn + "meta.llama-x"}],
                },
                {"status": "ACTIVE", "models": [{"modelArn": arn + "meta.llama-x"}]},
                {
                    "status": "ACTIVE",
                    "inferenceProfileId": "us.cohere.embed-v4:0",
                    "models": [{"modelArn": arn + "cohere.embed-v4:0"}],
                },
                {
                    "status": "ACTIVE",
                    "inferenceProfileId": "us.meta.llama-x",
                    "models": [{"modelArn": arn + "meta.llama-x"}],
                },
                {
                    "status": "ACTIVE",
                    "inferenceProfileId": "eu.meta.llama-x",
                    "models": [{"modelArn": arn + "meta.llama-x"}],
                },
                {
                    "status": "ACTIVE",
                    "inferenceProfileId": "us.amazon.nova-pro-v1:0",
                    "models": [{"modelArn": arn + "amazon.nova-pro-v1:0"}],
                },
                {
                    "status": "ACTIVE",
                    "inferenceProfileId": "us.unknown.model",
                    "models": [{"modelArn": arn + "unknown.model"}],
                },
            ]
        }


def test_discovery_skips_inactive_unnamed_and_lower_ranked_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "us_amazon_nova_pro_v1_0.json").write_text(
        json.dumps({"model_id": "us.amazon.nova-pro-v1:0"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        capture,
        "_default_models",
        lambda: ("global.amazon.nova-pro-v1:0", "us.deepseek.r1-v1:0"),
    )

    models = capture._discover_all_models(
        "us-east-1", tmp_path, bedrock_client=_EdgeCaseCatalogClient()
    )

    assert models == ("cohere.command-z", "us.deepseek.r1-v1:0", "us.meta.llama-x")


# --- capture helpers --------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "slug"),
    [
        (
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "us_anthropic_claude_haiku_4_5_20251001_v1_0",
        ),
        ("a..b::c", "a_b_c"),
        (".leading-and-trailing.", "leading_and_trailing"),
        ("plain", "plain"),
    ],
)
def test_slug_for_model_collapses_runs_of_non_alphanumerics(model_id: str, slug: str) -> None:
    assert capture._slug_for_model(model_id) == slug


def test_prompt_adapter_hands_the_rendered_prompt_to_the_backend() -> None:
    assert capture._PromptAdapter("rendered prompt").assemble() == "rendered prompt"


def test_backend_for_capture_preserves_canonical_default_provenance() -> None:
    default_id = get_default_mission_model_id()

    canonical = capture._backend_for_capture(default_id, "us-east-1")
    assert isinstance(canonical, BedrockSamplingBackend)
    assert canonical.model_id == default_id
    assert canonical._uses_default_model is True
    assert canonical._client is None

    explicit = capture._backend_for_capture("us.amazon.nova-lite-v1:0", "us-west-2")
    assert explicit.model_id == "us.amazon.nova-lite-v1:0"
    assert explicit._uses_default_model is False
    assert explicit._region == "us-west-2"
    assert explicit._client is None


@pytest.mark.parametrize(("read_timeout", "connect_timeout"), [(42, 10), (5, 5)])
def test_backend_for_capture_bounds_the_runtime_client_timeouts(
    monkeypatch: pytest.MonkeyPatch, read_timeout: int, connect_timeout: int
) -> None:
    sentinel = object()
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeSession:
        def client(self, service: str, **kwargs: Any) -> object:
            calls.append((service, kwargs))
            return sentinel

    monkeypatch.setattr(boto3, "Session", FakeSession)

    backend = capture._backend_for_capture(
        "us.amazon.nova-lite-v1:0", "us-east-2", read_timeout_seconds=read_timeout
    )

    assert backend._client is sentinel
    ((service, kwargs),) = calls
    assert service == "bedrock-runtime"
    assert kwargs["region_name"] == "us-east-2"
    assert isinstance(kwargs["config"], Config)
    assert kwargs["config"].read_timeout == read_timeout
    assert kwargs["config"].connect_timeout == connect_timeout


def test_capture_one_records_prompt_provenance_and_the_untouched_response() -> None:
    backend = _FakeBackend(dict(_VALID_RAWS))
    directive = _directive("metric_drive_loss")

    result = _capture_one(backend, "metric_drive_loss")

    expected_prompt = criteria_scaffold.build_scaffold_prompt(
        directive.text, allowlist=list(directive.allowlist)
    )
    assert backend.prompts == [expected_prompt]
    assert result == {
        "prompt_directive": "Drive validation loss below 0.1 on the demo training tool.",
        "prompt_allowlist": ["find_examples"],
        "prompt_sha256": hashlib.sha256(expected_prompt.encode("utf-8")).hexdigest(),
        "raw_response": _VALID_RAWS["metric_drive_loss"],
    }


def test_capture_one_validates_only_the_first_max_criteria_but_stores_all() -> None:
    six = json.dumps(
        [
            {
                "criterion_id": f"docs_{index}",
                "kind": "tool_call_succeeded",
                "required": True,
                "tool_name": "find_docs",
            }
            for index in range(criteria_scaffold.DEFAULT_MAX_CRITERIA + 1)
        ]
    )
    backend = _FakeBackend({"search_inference_docs": six})

    result = _capture_one(backend, "search_inference_docs")

    assert result["raw_response"] == six
    assert len(json.loads(result["raw_response"])) == 6


def test_capture_one_fails_closed_on_criteria_the_validators_reject() -> None:
    disallowed = json.dumps(
        [
            {
                "criterion_id": "job_ok",
                "kind": "tool_call_succeeded",
                "required": True,
                "tool_name": "submit_job",
            }
        ]
    )
    backend = _FakeBackend({"search_inference_docs": disallowed})
    with pytest.raises(MissionValidationError) as excinfo:
        _capture_one(backend, "search_inference_docs")
    assert excinfo.value.details == {
        "field": "criteria",
        "criterion_id": "job_ok",
        "reason": "tool_name_not_allowlisted",
        "tool_names": ["submit_job"],
    }

    prose = _FakeBackend({"search_inference_docs": "Sure! Here are some criteria."})
    with pytest.raises(ValueError, match="no JSON array found in response"):
        _capture_one(prose, "search_inference_docs")


# --- per-model capture ------------------------------------------------------


def test_capture_model_writes_a_private_fixture_the_replay_loader_accepts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests._scaffold_replay import load_fixture

    monkeypatch.setattr(capture, "_REPO_ROOT", tmp_path)
    output_dir = tmp_path / "fixtures"
    output_dir.mkdir()
    backend = _FakeBackend(dict(_VALID_RAWS))
    seen = _install_backend(monkeypatch, backend)

    ok = asyncio.run(
        capture._capture_model(
            "us.amazon.nova-micro-v1:0", "us-east-1", output_dir, read_timeout_seconds=90
        )
    )

    assert ok is True
    assert seen == [("us.amazon.nova-micro-v1:0", "us-east-1", 90)]
    assert len(backend.prompts) == len(capture._DIRECTIVES)

    written = output_dir / "us_amazon_nova_micro_v1_0.json"
    # Published atomically: no ``.tmp`` sibling survives, and the file is
    # owner-only readable.
    assert [path.name for path in output_dir.iterdir()] == [written.name]
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    text = written.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    payload = json.loads(text)
    assert payload["model_id"] == "us.amazon.nova-micro-v1:0"
    assert payload["region"] == "us-east-1"
    assert datetime.datetime.fromisoformat(payload["captured_at"]).tzinfo is not None
    assert list(payload["captures"]) == [directive.slug for directive in capture._DIRECTIVES]
    for directive in capture._DIRECTIVES:
        assert payload["captures"][directive.slug]["raw_response"] == _VALID_RAWS[directive.slug]

    # The strict loader the replay suite uses accepts the file as written.
    fixture = load_fixture(written)
    assert fixture.model_id == "us.amazon.nova-micro-v1:0"
    assert [c.slug for c in fixture.captures] == [d.slug for d in capture._DIRECTIVES]

    assert capsys.readouterr().out == (
        "[us.amazon.nova-micro-v1:0] wrote fixtures/us_amazon_nova_micro_v1_0.json\n"
    )


def test_capture_model_lets_the_ftu_gate_propagate_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raws: dict[str, str | BaseException] = dict(_VALID_RAWS)
    raws["search_inference_docs"] = BedrockFTUFormNotAcceptedError("submit the FTU form")
    _install_backend(monkeypatch, _FakeBackend(raws))

    with pytest.raises(BedrockFTUFormNotAcceptedError, match="submit the FTU form"):
        asyncio.run(capture._capture_model("us.anthropic.claude-x", "us-east-1", tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("with_cause", [True, False])
def test_capture_model_reports_transport_failures_per_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    with_cause: bool,
) -> None:
    error = SamplingTransportError("bedrock_ThrottlingException", "Too many requests")
    if with_cause:
        error.__cause__ = RuntimeError("slow down")
    raws: dict[str, str | BaseException] = dict(_VALID_RAWS)
    raws["metric_drive_loss"] = error
    backend = _FakeBackend(raws)
    _install_backend(monkeypatch, backend)

    ok = asyncio.run(capture._capture_model("us.meta.llama-x", "us-east-1", tmp_path))

    assert ok is False
    # The first directive succeeded, the second failed, the third never ran
    # and nothing was published.
    assert len(backend.prompts) == 2
    assert list(tmp_path.iterdir()) == []
    suffix = "; slow down" if with_cause else ""
    assert capsys.readouterr().err == (
        "[us.meta.llama-x] capture failed for 'metric_drive_loss': "
        f"bedrock_ThrottlingException: bedrock_ThrottlingException: Too many requests{suffix}\n"
    )


def test_capture_model_rejects_output_the_production_validators_refuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raws: dict[str, str | BaseException] = dict(_VALID_RAWS)
    raws["event_goal_reached"] = json.dumps(
        [
            {
                "criterion_id": "job_ok",
                "kind": "tool_call_succeeded",
                "required": True,
                "tool_name": "submit_job",
            }
        ]
    )
    _install_backend(monkeypatch, _FakeBackend(raws))

    assert asyncio.run(capture._capture_model("us.meta.llama-x", "us-east-1", tmp_path)) is False
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr().err == (
        "[us.meta.llama-x] capture rejected for 'event_goal_reached': "
        "MissionValidationError: validation_error; details={'field': 'criteria', "
        "'criterion_id': 'job_ok', 'reason': 'tool_name_not_allowlisted', "
        "'tool_names': ['submit_job']}\n"
    )

    raws["event_goal_reached"] = "Sure! Here are some criteria."
    assert asyncio.run(capture._capture_model("us.meta.llama-x", "us-east-1", tmp_path)) is False
    assert capsys.readouterr().err == (
        "[us.meta.llama-x] capture rejected for 'event_goal_reached': "
        "ValueError: no JSON array found in response\n"
    )


def test_capture_model_surfaces_unexpected_errors_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raws: dict[str, str | BaseException] = dict(_VALID_RAWS)
    raws["search_inference_docs"] = KeyError("output")
    _install_backend(monkeypatch, _FakeBackend(raws))

    assert asyncio.run(capture._capture_model("us.meta.llama-x", "us-east-1", tmp_path)) is False
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr().err == (
        "[us.meta.llama-x] unexpected error for 'search_inference_docs': KeyError: 'output'\n"
    )


def test_capture_model_removes_its_temporary_file_when_publishing_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _OsWithFailingReplace:
        def __getattr__(self, name: str) -> Any:
            return getattr(os, name)

        def replace(self, src: Any, dst: Any) -> None:
            raise OSError("read-only file system")

    monkeypatch.setattr(capture, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(capture, "os", _OsWithFailingReplace())
    _install_backend(monkeypatch, _FakeBackend(dict(_VALID_RAWS)))

    with pytest.raises(OSError, match="read-only file system"):
        asyncio.run(capture._capture_model("us.amazon.nova-micro-v1:0", "us-east-1", tmp_path))
    assert list(tmp_path.iterdir()) == []


# --- selection and runners --------------------------------------------------


def test_selected_models_falls_back_to_the_configured_default_plus_curated_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = argparse.Namespace(discover_all_models=False, models=None)

    monkeypatch.setattr(capture, "get_default_mission_model_id", lambda: "us.amazon.nova-lite-v1:0")
    selected = capture._selected_models(args)
    assert selected == capture._default_models()
    assert selected[0] == "us.amazon.nova-lite-v1:0"
    assert len(selected) == len(capture._CURATED_MODELS)

    monkeypatch.setattr(capture, "get_default_mission_model_id", lambda: "global.new.model-v1:0")
    assert capture._selected_models(args) == ("global.new.model-v1:0", *capture._CURATED_MODELS)


def test_serial_run_counts_outcomes_and_keeps_the_backend_default_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, str, Path, int | None]] = []

    async def fake_capture(
        model_id: str, region: str, output_dir: Path, *, read_timeout_seconds: int | None
    ) -> bool:
        seen.append((model_id, region, output_dir, read_timeout_seconds))
        return model_id == "model.ok"

    monkeypatch.setattr(capture, "_capture_model", fake_capture)
    args = _run_args(tmp_path, ["model.ok", "model.denied"])

    assert asyncio.run(capture._main_async(args)) == 0

    assert seen == [
        ("model.ok", "us-east-1", tmp_path / "out", None),
        ("model.denied", "us-east-1", tmp_path / "out", None),
    ]
    assert (tmp_path / "out").is_dir()
    out = capsys.readouterr().out
    assert "Selected" not in out
    assert "Per-request read timeout" not in out
    assert "Concurrent model workers" not in out
    assert out.strip() == "Captured 1 model(s); 1 failed; 0 skipped."


def test_serial_run_exits_nonzero_when_every_model_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_capture(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(capture, "_capture_model", fake_capture)
    assert asyncio.run(capture._main_async(_run_args(tmp_path, ["model.a", "model.b"]))) == 1
    assert capsys.readouterr().out.strip() == "Captured 0 model(s); 2 failed; 0 skipped."


def test_serial_run_stops_at_the_account_wide_ftu_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    attempted: list[str] = []

    async def fake_capture(model_id: str, *_args: Any, **_kwargs: Any) -> bool:
        attempted.append(model_id)
        if "anthropic." in model_id:
            raise BedrockFTUFormNotAcceptedError("fill in the FTU form")
        return True

    monkeypatch.setattr(capture, "_capture_model", fake_capture)
    models = ["us.amazon.a", "us.anthropic.b", "us.amazon.c"]

    assert asyncio.run(capture._main_async(_run_args(tmp_path, models))) == 1

    assert attempted == ["us.amazon.a", "us.anthropic.b"]
    captured = capsys.readouterr()
    assert "Captured" not in captured.out
    assert captured.err == "\nfill in the FTU form\nCaptured 1 model(s) before aborting.\n"


def test_discovery_with_no_candidates_exits_cleanly_without_touching_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(capture, "_discover_all_models", lambda region, output_dir: ())
    args = argparse.Namespace(
        discover_all_models=True,
        models=None,
        region="us-east-1",
        output_dir=tmp_path / "out",
        list_candidates=False,
    )

    assert asyncio.run(capture._main_async(args)) == 0

    assert not (tmp_path / "out").exists()
    assert capsys.readouterr().out == (
        "Selected 0 model candidate(s); a capture run makes up to 0 paid Converse calls.\n"
        "No uncaptured model candidates found.\n"
    )


def test_concurrent_run_isolates_failures_and_skips_anthropic_after_the_ftu_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_capture(
        model_id: str, region: str, output_dir: Path, *, read_timeout_seconds: int | None
    ) -> bool:
        # An explicit (non-discovery) run keeps the backend's own timeout.
        assert read_timeout_seconds is None
        if model_id == "us.anthropic.first":
            raise BedrockFTUFormNotAcceptedError("fill in the FTU form")
        if model_id == "us.meta.broken":
            raise RuntimeError("boom")
        return model_id == "us.amazon.good"

    monkeypatch.setattr(capture, "_capture_model", fake_capture)
    models = [
        "us.anthropic.first",
        "us.amazon.good",
        "us.anthropic.second",
        "us.meta.broken",
        "us.mistral.denied",
    ]

    assert asyncio.run(capture._main_async(_run_args(tmp_path, models, workers=2))) == 0

    out, err = capsys.readouterr()
    assert "Concurrent model workers: 2" in out
    assert out.strip().endswith("Captured 1 model(s); 3 failed; 1 skipped.")
    assert "[us.anthropic.second] skipped after Anthropic FTU failure" in err
    assert "[us.meta.broken] unexpected capture failure: RuntimeError: boom" in err
    # The account-wide remediation is printed once, after every model settled.
    assert err.count("fill in the FTU form") == 1
    assert err.rstrip().endswith("fill in the FTU form")


def test_main_parses_argv_and_runs_the_async_entrypoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture_scaffold_fixtures.py",
            "--model",
            "example.model",
            "--list-candidates",
            "--output-dir",
            str(tmp_path / "out"),
        ],
    )

    async def unexpected_capture(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("list-only mode must not invoke a model")

    monkeypatch.setattr(capture, "_capture_model", unexpected_capture)

    assert capture.main() == 0

    assert capsys.readouterr().out == (
        "Selected 1 model candidate(s); a capture run makes up to 3 paid Converse calls.\n"
        "example.model\n"
    )
    assert not (tmp_path / "out").exists()


def test_import_puts_the_repository_and_mission_roots_on_sys_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mission.*`` must resolve however the script is launched.

    The module prepends the repository root and ``gco_mcp`` to ``sys.path`` on
    import, skipping entries already present. Under pytest both are usually on
    the path before this module loads (the rootdir conftest and earlier MCP
    suites put them there), so the insertion is only proven by executing the
    module against a path that lacks them — from a bare interpreter, which is
    how ``python scripts/capture_scaffold_fixtures.py`` runs.
    """
    import importlib.util
    import sys

    roots = [str(capture._REPO_ROOT), str(capture._REPO_ROOT / "gco_mcp")]
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry not in roots])
    assert not any(root in sys.path for root in roots)

    spec = importlib.util.spec_from_file_location(
        "_gco_capture_scaffold_reimport", capture.__file__
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves the module's postponed annotations through sys.modules.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    # Inserted at the front, most recent first; a second execution adds nothing.
    assert sys.path[:2] == [str(capture._REPO_ROOT / "gco_mcp"), str(capture._REPO_ROOT)]
    spec.loader.exec_module(module)
    assert sys.path.count(str(capture._REPO_ROOT)) == 1
    assert sys.path.count(str(capture._REPO_ROOT / "gco_mcp")) == 1
