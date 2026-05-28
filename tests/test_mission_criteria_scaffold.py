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
        # The metric path is prefixed with ``metrics.`` so the engine's
        # dot-path resolver against the Observation root picks up the
        # value the dispatcher returns under ``{"metrics": {...}}``.
        # Without the prefix the criterion would land as
        # ``inconclusive: metric_path_missing`` on every iteration.
        assert c["metric"] == "metrics.latency"


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
        # See the latency-keyword test above for the ``metrics.`` prefix
        # rationale — the dispatcher emits ``{"metrics": {...}}`` and the
        # engine's metric-path resolver walks against the Observation root.
        assert result[0]["metric"] == "metrics.throughput"


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


class TestPromptObservationSchema:
    """The prompt teaches the model the Observation shape and metric path convention.

    A live smoke test against Bedrock revealed that without explicit
    schema documentation the model emits bare metric names
    (``"val_loss"``) instead of the dot-path
    (``"metrics.val_loss"``) the engine actually walks. Sessions
    built from those criteria silently evaluated
    ``inconclusive: metric_path_missing`` on every iteration. The
    prompt now documents the Observation shape and the metric-path
    convention; pin those documentation strings here so a future
    edit that drops them flips this test red.
    """

    def test_prompt_describes_observation_fields(self) -> None:
        """The prompt names every Observation field the validator exposes."""
        prompt = criteria_scaffold.build_scaffold_prompt("Lower latency.")
        assert "Observation shape" in prompt
        # Each canonical Observation field is mentioned by name so
        # the model knows what subscripts predicates can reach.
        assert "tool_results" in prompt
        assert '"metrics"' in prompt
        assert '"events"' in prompt

    def test_prompt_documents_metric_dot_path_convention(self) -> None:
        """The prompt explicitly says metric paths are dot-paths into ``metrics``."""
        prompt = criteria_scaffold.build_scaffold_prompt("Lower latency.")
        # The prompt names the canonical example so the model emits
        # ``metrics.val_loss`` rather than ``val_loss``.
        assert "metrics.val_loss" in prompt
        # And explicitly warns about the bare-name failure mode so a
        # future model that needs more hand-holding understands the
        # consequence.
        assert "metric_path_missing" in prompt

    def test_prompt_warns_about_attribute_access(self) -> None:
        """The prompt tells the model attribute access is rejected.

        The predicate AST validator rejects attribute access on
        ``obs`` (``obs.metrics`` raises ``attribute_target_not_allowed``)
        but accepts subscript notation (``obs['metrics']``). Models
        that emit attribute access burn retries; the prompt now
        flags this convention explicitly.
        """
        prompt = criteria_scaffold.build_scaffold_prompt("Lower latency.")
        assert "subscript" in prompt.lower()


class TestNormalizeMetricPath:
    """``_normalize_metric_path`` injects ``metrics.`` prefix on bare names."""

    def test_bare_name_gets_metrics_prefix(self) -> None:
        """A metric_threshold criterion with no ``.`` gets ``metrics.`` prefix."""
        out = criteria_scaffold._normalize_metric_path(
            {
                "criterion_id": "loss",
                "kind": "metric_threshold",
                "metric": "val_loss",
                "op": "<",
                "target": 0.1,
            }
        )
        assert out["metric"] == "metrics.val_loss"

    def test_already_qualified_path_passes_through(self) -> None:
        """A metric path that already contains a ``.`` is left alone."""
        out = criteria_scaffold._normalize_metric_path(
            {
                "criterion_id": "loss",
                "kind": "metric_threshold",
                "metric": "metrics.val_loss",
                "op": "<",
                "target": 0.1,
            }
        )
        assert out["metric"] == "metrics.val_loss"

    def test_arbitrary_dot_path_passes_through(self) -> None:
        """Tool-results dot-paths are not rewritten — only bare names are."""
        out = criteria_scaffold._normalize_metric_path(
            {
                "criterion_id": "score",
                "kind": "metric_threshold",
                "metric": "tool_results.0.score",
                "op": ">=",
                "target": 0.9,
            }
        )
        assert out["metric"] == "tool_results.0.score"

    def test_non_metric_threshold_passes_through(self) -> None:
        """``predicate`` and ``event`` criteria are never touched."""
        pred = {
            "criterion_id": "p",
            "kind": "predicate",
            "expression": "obs['x'] > 0",
            "required": True,
        }
        assert criteria_scaffold._normalize_metric_path(pred) is pred
        evt = {
            "criterion_id": "e",
            "kind": "event",
            "event_name": "started",
            "required": True,
        }
        assert criteria_scaffold._normalize_metric_path(evt) is evt

    def test_does_not_mutate_input(self) -> None:
        """The normaliser returns a shallow copy; the input is untouched."""
        original = {
            "criterion_id": "loss",
            "kind": "metric_threshold",
            "metric": "val_loss",
            "op": "<",
            "target": 0.1,
        }
        out = criteria_scaffold._normalize_metric_path(original)
        assert out is not original
        assert original["metric"] == "val_loss"  # input still bare
        assert out["metric"] == "metrics.val_loss"  # output prefixed

    def test_empty_or_non_string_metric_passes_through(self) -> None:
        """Defensive: a malformed metric value triggers the ``return criterion`` path."""
        # The validator will reject these later; the normaliser just
        # passes them through so the validator's error message stays
        # the canonical signal.
        empty = {"kind": "metric_threshold", "metric": ""}
        assert criteria_scaffold._normalize_metric_path(empty) is empty
        non_str = {"kind": "metric_threshold", "metric": 42}
        assert criteria_scaffold._normalize_metric_path(non_str) is non_str
