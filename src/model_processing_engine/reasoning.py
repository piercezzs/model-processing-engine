from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .exceptions import ConfigurationError


ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
ReasoningEffortSetting = Literal[
    "auto",
    "none",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
]

REASONING_EFFORT_SETTINGS: tuple[str, ...] = (
    "auto",
    "none",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


@dataclass(frozen=True)
class ModelReasoningCapability:
    supported_efforts: tuple[ReasoningEffort, ...] = ()
    model_default: ReasoningEffort | None = None
    wire_parameter: str | None = None

    @property
    def configurable(self) -> bool:
        return bool(self.supported_efforts and self.wire_parameter)

    def descriptor(self) -> dict[str, object]:
        return {
            "configurable": self.configurable,
            "options": ["auto", *self.supported_efforts],
            "modelDefault": self.model_default,
            "wireParameter": self.wire_parameter,
        }


@dataclass(frozen=True)
class ResolvedReasoningEffort:
    requested: ReasoningEffortSetting
    effective: ReasoningEffort | None
    source: Literal["runtime", "task", "provider", "model_default"]
    wire_parameter: str | None

    def descriptor(self) -> dict[str, str | None]:
        return {
            "requested": self.requested,
            "effective": self.effective,
            "source": self.source,
            "wireParameter": self.wire_parameter,
        }


@dataclass(frozen=True)
class _CapabilityRule:
    pattern: re.Pattern[str]
    capability: ModelReasoningCapability


# This registry intentionally contains only model families with a verified Chat
# Completions reasoning_effort contract. Unknown and native Anthropic models stay
# auto-only instead of receiving a guessed parameter through a compatible gateway.
_OPENAI_CHAT_COMPLETIONS_RULES: tuple[_CapabilityRule, ...] = (
    _CapabilityRule(
        pattern=re.compile(r"^gpt-5\.6-(?:sol|terra|luna)(?:$|[-.])", re.IGNORECASE),
        capability=ModelReasoningCapability(
            supported_efforts=("none", "low", "medium", "high", "xhigh", "max"),
            model_default="medium",
            wire_parameter="reasoning_effort",
        ),
    ),
    _CapabilityRule(
        pattern=re.compile(r"^gpt-5\.5-pro(?:$|[-.])", re.IGNORECASE),
        capability=ModelReasoningCapability(
            supported_efforts=("medium", "high", "xhigh"),
            model_default="high",
            wire_parameter="reasoning_effort",
        ),
    ),
    _CapabilityRule(
        pattern=re.compile(r"^gpt-5\.5(?:$|[-.])", re.IGNORECASE),
        capability=ModelReasoningCapability(
            supported_efforts=("none", "low", "medium", "high", "xhigh"),
            model_default="medium",
            wire_parameter="reasoning_effort",
        ),
    ),
)

_CODEX_SDK_RULES: tuple[_CapabilityRule, ...] = (
    _CapabilityRule(
        pattern=re.compile(r"^gpt-5\.6-(?:sol|terra|luna)(?:$|[-.])", re.IGNORECASE),
        capability=ModelReasoningCapability(
            supported_efforts=("none", "low", "medium", "high", "xhigh"),
            model_default="medium",
            wire_parameter="effort",
        ),
    ),
    _CapabilityRule(
        pattern=re.compile(r"^gpt-5\.5(?:$|[-.])", re.IGNORECASE),
        capability=ModelReasoningCapability(
            supported_efforts=("none", "low", "medium", "high", "xhigh"),
            model_default="medium",
            wire_parameter="effort",
        ),
    ),
)


def reasoning_capability(provider_type: str, model: str) -> ModelReasoningCapability:
    rules = {
        "openai_compatible": _OPENAI_CHAT_COMPLETIONS_RULES,
        "codex_sdk": _CODEX_SDK_RULES,
    }.get(provider_type)
    if rules is None:
        return ModelReasoningCapability()
    normalized_model = model.strip()
    for rule in rules:
        if rule.pattern.match(normalized_model):
            return rule.capability
    return ModelReasoningCapability()


def reasoning_capabilities(
    provider_type: str,
    models: list[str] | tuple[str, ...],
) -> dict[str, dict[str, object]]:
    return {
        model: reasoning_capability(provider_type, model).descriptor()
        for model in dict.fromkeys(item.strip() for item in models if item.strip())
    }


def resolve_reasoning_effort(
    *,
    provider_type: str,
    model: str,
    runtime_setting: ReasoningEffortSetting | None,
    task_setting: ReasoningEffortSetting | None,
    provider_setting: ReasoningEffortSetting | None,
) -> ResolvedReasoningEffort:
    if runtime_setting is not None:
        requested = runtime_setting
        source: Literal["runtime", "task", "provider", "model_default"] = "runtime"
    elif task_setting is not None:
        requested = task_setting
        source = "task"
    elif provider_setting is not None:
        requested = provider_setting
        source = "provider"
    else:
        requested = "auto"
        source = "model_default"

    capability = reasoning_capability(provider_type, model)
    if requested == "auto":
        return ResolvedReasoningEffort(
            requested=requested,
            effective=None,
            source=source,
            wire_parameter=None,
        )
    if requested not in capability.supported_efforts:
        supported = ", ".join(("auto", *capability.supported_efforts))
        raise ConfigurationError(
            f"Model {model!r} does not support reasoning effort {requested!r} "
            f"through provider type {provider_type!r}; supported values: {supported}"
        )
    return ResolvedReasoningEffort(
        requested=requested,
        effective=requested,
        source=source,
        wire_parameter=capability.wire_parameter,
    )
