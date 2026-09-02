from .base import ProviderCallResult, ProviderConfig, ProviderFactory, ProviderRegistry
from .codex_sdk import CodexSdkProvider
from .mock import MockProvider
from .openai_compatible import OpenAICompatibleProvider


def default_provider_factories() -> dict[str, ProviderFactory]:
    """Return the complete built-in Provider registry for every config reader."""

    return {
        "mock": MockProvider,
        "codex_sdk": CodexSdkProvider,
        "openai_compatible": OpenAICompatibleProvider,
    }


__all__ = [
    "MockProvider",
    "CodexSdkProvider",
    "OpenAICompatibleProvider",
    "ProviderCallResult",
    "ProviderConfig",
    "ProviderRegistry",
    "default_provider_factories",
]
