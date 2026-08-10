"""Run settings for the example-job validation harness."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.live_release_validation.models import RunSettings

from .specs import EXAMPLE_SPECS, required_feature_overrides, required_helm_overrides


@dataclass(frozen=True)
class ExampleRunSettings(RunSettings):
    """Operator inputs for one example-validation run.

    Extends the live-release-validation settings with the example selection;
    the required helm/feature enablement is DERIVED from the selection so the
    deployed graph always matches exactly what the selected examples need.
    """

    #: Example names (file stems) to validate this run, in registry order.
    selected_examples: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        unknown = sorted(set(self.selected_examples) - set(EXAMPLE_SPECS))
        if unknown:
            raise ValueError(
                f"Unknown example name(s): {', '.join(unknown)}. "
                f"Valid: {', '.join(sorted(EXAMPLE_SPECS))}"
            )
        # The scheduler charts the selection needs are threaded through the
        # same optional_schedulers field the base class already carries so
        # identity(), resume checks, and extra_cdk_context() stay coherent.
        derived = required_helm_overrides(list(self.selected_examples))
        object.__setattr__(
            self, "optional_schedulers", tuple(sorted({*self.optional_schedulers, *derived}))
        )

    @property
    def feature_overrides(self) -> tuple[str, ...]:
        return required_feature_overrides(list(self.selected_examples))

    def extra_cdk_context(self) -> dict[str, str]:
        context = super().extra_cdk_context()
        if self.feature_overrides:
            context["feature_enabled_overrides"] = ",".join(self.feature_overrides)
        return context

    def identity(self) -> dict[str, Any]:
        identity = super().identity()
        identity["selected_examples"] = list(self.selected_examples)
        identity["feature_overrides"] = list(self.feature_overrides)
        return identity
