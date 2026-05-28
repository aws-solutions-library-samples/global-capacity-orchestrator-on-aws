"""Unit tests for ``mcp/mission/criteria_scaffold.py``.

Three groups of tests:

* **Deterministic generator** — keyword-template lookups produce the
  expected criterion shape, with a fallback placeholder when no
  keyword matches.
* **Validator contract** — every output of the deterministic generator
  is accepted by :func:`mission.validation.validate_criteria`. This
  is a **contract test**: no matter what directive shape we throw at
  the generator, the result has to be a file the operator can hand to
  ``mission start --criteria-file`` without further editing.
* **Sampling path** — :func:`generate_sampled_criteria` walks its
  retry loop correctly: a backend that returns a well-formed JSON
  array succeeds in one attempt; a backend that returns garbage and
  then a valid array succeeds on the retry; a backend that returns
  garbage on every attempt eventually raises
  :class:`ScaffoldSamplingError`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))

from mission import criteria_scaffold  # noqa: E402
from mission import validation as mission_validation  # noqa: E402

# ---------------------------------------------------------------------------
# Deterministic generator
# ---------------------------------------------------------------------------


class TestDeterministicLossKeyword:
    """Loss-flavoured directives produce a metric_threshold with op '<='."""

    def test_loss_keyword_produces_metric_threshold(self) -> None:
        result = criteria_scaffold.generate_deterministic_criteria(
            "Drive validation loss below 0.1."
        )
        assert len(result) == 1
        c = result[0]
        assert c["kind"] == "metric_threshold"
        assert c["op"] == "<="
        assert c["required"] is True
        # The metric name reflects the captured keyword.
        assert "loss" in c["metric"]

    def test_latency_keyword_produces_metric_threshold(self) -> None:
        result = criteria_scaffold.generate_deterministic_criteria("Reduce request latency.")
        assert len(result) == 1
        c = result[0]
        assert c["kind"] == "metric_threshold"
        assert c["op"] == "<="
        assert c["metric"] == "latency"


class TestDeterministicHigherIsBetter:
    """Accuracy-flavoured directives produce a metric_threshold with op '>='."""

    def test_accuracy_keyword_produces_metric_threshold(self) -> None:
        result = criteria_scaffold.generate_deterministic_criteria(
            "Improve validation accuracy above 90 percent."
        )
        assert len(result) == 1
        c = result[0]
        assert c["kind"] == "metric_threshold"
        assert c["op"] == ">="
        assert "accuracy" in c["metric"]

    def test_throughput_keyword_produces_metric_threshold(self) -> None:
        result = criteria_scaffold.generate_deterministic_criteria("Increase throughput.")
        assert len(result) == 1
        assert result[0]["op"] == ">="
        assert result[0]["metric"] == "throughput"


class TestDeterministicSearchKeyword:
    """Search-flavoured directives produce a predicate criterion."""

    def test_find_keyword_produces_predicate(self) -> None:
        result = criteria_scaffold.generate_deterministic_criteria(
            "Find documentation about inference endpoints."
        )
        assert len(result) == 1
        c = result[0]
        assert c["kind"] == "predicate"
        assert c["required"] is True
        assert "tool_results" in c["expression"]

    def test_search_keyword_produces_predicate(self) -> None:
        result = criteria_scaffold.generate_deterministic_criteria(
            "Search the AWS catalog for GPU instance types."
        )
        assert result[0]["kind"] == "predicate"

    def test_discover_keyword_produces_predicate(self) -> None:
        result = criteria_scaffold.generate_deterministic_criteria(
            "Discover suitable training images."
        )
        assert result[0]["kind"] == "predicate"


class TestDeterministicEventKeyword:
    """Event-flavoured directives produce an event criterion."""

    def test_succeeded_keyword_produces_event(self) -> None:
        result = criteria_scaffold.generate_deterministic_criteria(
            "Wait for the training job to have succeeded."
        )
        assert len(result) == 1
        c = result[0]
        assert c["kind"] == "event"
        assert c["event_name"]


class TestDeterministicDefaultFallback:
    """Generic directives produce a single placeholder predicate."""

    def test_generic_directive_produces_placeholder(self) -> None:
        result = criteria_scaffold.generate_deterministic_criteria(
            "Make the cluster better somehow."
        )
        assert len(result) == 1
        c = result[0]
        assert c["kind"] == "predicate"
        assert c["expression"] == "True"
        assert c["required"] is True
        # The TODO note signals the operator to edit before use.
        assert "TODO" in c["description"]

    def test_empty_directive_produces_placeholder(self) -> None:
        result = criteria_scaffold.generate_deterministic_criteria("")
        assert len(result) == 1
        assert result[0]["kind"] == "predicate"
        assert result[0]["expression"] == "True"


class TestValidatesAgainstValidateCriteria:
    """Every deterministic output is accepted by validate_criteria.

    Contract test: the scaffolder's job is to produce a file the
    operator can use immediately. A regression that emitted a missing
    or malformed key would surface here. Coverage spans every keyword
    template plus the placeholder fallback.
    """

    @pytest.mark.parametrize(
        "directive",
        [
            "Drive validation loss below 0.1.",
            "Reduce inference latency.",
            "Drop the error rate.",
            "Lower training cost.",
            "Improve validation accuracy.",
            "Increase throughput.",
            "Boost recall on the test set.",
            "Push F1 higher.",
            "Find related examples.",
            "Search for matching docs.",
            "Discover usable images.",
            "Locate the nearest GPU pool.",
            "Wait for the job to have succeeded.",
            "Confirm the model has finished.",
            "Make things better.",  # default fallback
            "",  # empty falls back to placeholder
            "Drive XYZ to ABC.",  # unmatched keyword falls back
        ],
    )
    def test_directive_produces_accepted_criteria(self, directive: str) -> None:
        result = criteria_scaffold.generate_deterministic_criteria(directive)
        # validate_criteria mutates the dicts to attach _parsed_ast for
        # predicate entries; we don't care about the return value.
        mission_validation.validate_criteria(result)


# ---------------------------------------------------------------------------
# Sampling path
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Test-only stub backend with a canned response queue."""

    backend_name = "mcp"
    model_id = "test-model"

    def __init__(self, responses: list[str | Exception]) -> None:
        self._queue: list[str | Exception] = list(responses)
        self.calls: list[str] = []

    async def sample(self, prompt: Any) -> str:
        rendered = prompt.assemble()
        self.calls.append(rendered)
        if not self._queue:
            raise RuntimeError("no canned response left")
        next_response = self._queue.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        return next_response


class TestGenerateSampledCriteria:
    """Drive the sampling loop with a stub backend."""

    @pytest.mark.asyncio
    async def test_first_attempt_succeeds(self) -> None:
        canned = (
            "["
            '{"criterion_id": "loss", "kind": "metric_threshold", '
            '"required": true, "metric": "val_loss", "op": "<=", '
            '"target": 0.1}'
            "]"
        )
        backend = _FakeBackend([canned])
        result = await criteria_scaffold.generate_sampled_criteria(
            backend,  # type: ignore[arg-type]
            "Drive validation loss below 0.1.",
            allowlist=["find_examples"],
        )
        assert len(result) == 1
        assert result[0]["criterion_id"] == "loss"
        assert "_parsed_ast" not in result[0]  # private keys stripped
        assert len(backend.calls) == 1

    @pytest.mark.asyncio
    async def test_retry_after_garbage(self) -> None:
        valid = (
            "["
            '{"criterion_id": "loss", "kind": "metric_threshold", '
            '"required": true, "metric": "val_loss", "op": "<=", '
            '"target": 0.1}'
            "]"
        )
        backend = _FakeBackend(["this is not JSON", valid])
        result = await criteria_scaffold.generate_sampled_criteria(
            backend,  # type: ignore[arg-type]
            "Drive validation loss below 0.1.",
            retries=3,
        )
        assert len(result) == 1
        # The second prompt should carry a feedback block.
        assert "Feedback on previous attempt" in backend.calls[1]
        assert len(backend.calls) == 2

    @pytest.mark.asyncio
    async def test_all_retries_fail(self) -> None:
        backend = _FakeBackend(["garbage1", "garbage2", "garbage3"])
        with pytest.raises(criteria_scaffold.ScaffoldSamplingError) as excinfo:
            await criteria_scaffold.generate_sampled_criteria(
                backend,  # type: ignore[arg-type]
                "Drive validation loss below 0.1.",
                retries=2,
            )
        assert excinfo.value.last_reason == "json_parse"
        # retries=2 means 3 total attempts (initial + 2 retries).
        assert len(backend.calls) == 3

    @pytest.mark.asyncio
    async def test_validator_rejection_triggers_retry(self) -> None:
        bad_payload = '[{"criterion_id": "x"}]'  # missing kind/required
        good_payload = (
            '[{"criterion_id": "x", "kind": "predicate", "required": true, "expression": "True"}]'
        )
        backend = _FakeBackend([bad_payload, good_payload])
        result = await criteria_scaffold.generate_sampled_criteria(
            backend,  # type: ignore[arg-type]
            "Make things great.",
            retries=2,
        )
        assert len(result) == 1
        # Feedback prompt mentions the rejection reason.
        assert "Rejection reason" in backend.calls[1]

    @pytest.mark.asyncio
    async def test_transport_error_propagates(self) -> None:
        backend = _FakeBackend([RuntimeError("network down")])
        with pytest.raises(criteria_scaffold.ScaffoldSamplingError) as excinfo:
            await criteria_scaffold.generate_sampled_criteria(
                backend,  # type: ignore[arg-type]
                "anything",
            )
        assert excinfo.value.last_reason == "transport_error"

    @pytest.mark.asyncio
    async def test_max_criteria_truncates(self) -> None:
        # Backend emits 6 entries; max_criteria=3 should truncate.
        entries = [
            f'{{"criterion_id": "c{i}", "kind": "predicate", '
            f'"required": false, "expression": "True"}}'
            for i in range(6)
        ]
        canned = "[" + ",".join(entries) + "]"
        backend = _FakeBackend([canned])
        result = await criteria_scaffold.generate_sampled_criteria(
            backend,  # type: ignore[arg-type]
            "anything",
            max_criteria=3,
        )
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_markdown_fenced_response_is_parsed(self) -> None:
        fenced = (
            "```json\n"
            "["
            '{"criterion_id": "loss", "kind": "metric_threshold", '
            '"required": true, "metric": "val_loss", "op": "<=", '
            '"target": 0.1}'
            "]\n"
            "```"
        )
        backend = _FakeBackend([fenced])
        result = await criteria_scaffold.generate_sampled_criteria(
            backend,  # type: ignore[arg-type]
            "Drive validation loss below 0.1.",
        )
        assert len(result) == 1
        assert result[0]["criterion_id"] == "loss"


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


class TestBuildScaffoldPrompt:
    """The prompt builder produces stable, parseable output."""

    def test_prompt_contains_directive(self) -> None:
        prompt = criteria_scaffold.build_scaffold_prompt("Lower latency.")
        assert "Lower latency." in prompt
        # Always asks for JSON array output.
        assert "JSON array" in prompt

    def test_prompt_lists_allowlist(self) -> None:
        prompt = criteria_scaffold.build_scaffold_prompt(
            "Lower latency.",
            allowlist=["find_examples", "find_docs"],
        )
        assert "find_examples" in prompt
        assert "find_docs" in prompt

    def test_prompt_with_no_allowlist_says_none(self) -> None:
        prompt = criteria_scaffold.build_scaffold_prompt("Lower latency.")
        assert "(none specified)" in prompt

    def test_feedback_appended_when_provided(self) -> None:
        prompt = criteria_scaffold.build_scaffold_prompt(
            "Lower latency.",
            feedback="Previous attempt missing required key 'kind'.",
        )
        assert "Feedback on previous attempt" in prompt
        assert "missing required key" in prompt

    def test_max_criteria_in_prompt(self) -> None:
        prompt = criteria_scaffold.build_scaffold_prompt(
            "Lower latency.",
            max_criteria=7,
        )
        assert "at most 7" in prompt
