from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from model_processing_engine.canonical import digest_json
from model_processing_engine.exceptions import ConfigurationError


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    type: str
    default_model: str
    base_url: str = ""
    chat_completions_path: str = "/chat/completions"
    api_key_env: str = ""
    timeout_seconds: int = 60
    transport_retries: int = 2
    cache_identity: str = "1"

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "baseUrl": self.base_url,
            "chatCompletionsPath": self.chat_completions_path,
            "cacheIdentity": self.cache_identity,
        }

    @property
    def digest(self) -> str:
        return digest_json(self.identity)


@dataclass(frozen=True)
class ProviderCallResult:
    content: dict[str, Any]
    usage: dict[str, Any]
    attempts: int
    elapsed_ms: int


class ModelProvider(Protocol):
    config: ProviderConfig

    def call_json(
        self,
        *,
        model: str,
        system_prompt: str,
        input_payload: dict[str, Any],
        output_schema: dict[str, Any],
        temperature: float,
        max_tokens: int | None,
    ) -> ProviderCallResult: ...


ProviderFactory = Callable[[ProviderConfig], ModelProvider]


class ProviderRegistry:
    def __init__(self, providers: dict[str, ModelProvider], *, default_provider_id: str) -> None:
        if not providers:
            raise ConfigurationError("At least one provider must be configured")
        if default_provider_id not in providers:
            raise ConfigurationError(f"Unknown default provider: {default_provider_id}")
        self._providers = dict(providers)
        self.default_provider_id = default_provider_id

    def get(self, provider_id: str | None) -> ModelProvider:
        resolved = (provider_id or self.default_provider_id).strip()
        try:
            return self._providers[resolved]
        except KeyError as exc:
            raise ConfigurationError(f"Unknown provider: {resolved}") from exc

    def ids(self) -> list[str]:
        return sorted(self._providers)


def load_provider_registry(
    config_path: str | Path,
    *,
    factories: dict[str, ProviderFactory],
) -> ProviderRegistry:
    path = Path(config_path).expanduser().resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Provider configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Invalid provider configuration JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("Provider configuration must be a JSON object")
    providers: dict[str, ModelProvider] = {}
    for provider_id, value in dict(raw.get("providers") or {}).items():
        if not isinstance(value, dict):
            raise ConfigurationError(f"Provider {provider_id} must be an object")
        config = _provider_config(str(provider_id), value)
        factory = factories.get(config.type)
        if not factory:
            raise ConfigurationError(f"Unsupported provider type: {config.type}")
        providers[config.id] = factory(config)
    default_id = str(raw.get("defaultProviderId") or next(iter(providers), ""))
    return ProviderRegistry(providers, default_provider_id=default_id)


def _provider_config(provider_id: str, value: dict[str, Any]) -> ProviderConfig:
    provider_type = str(value.get("type") or "").strip()
    base_url = str(value.get("baseUrl") or "").strip().rstrip("/")
    if provider_type == "openai_compatible":
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError(f"Provider {provider_id} baseUrl must be an HTTP(S) URL")
    timeout = int(value.get("timeoutSeconds") or 60)
    retries = int(value.get("transportRetries") if value.get("transportRetries") is not None else 2)
    if not 1 <= timeout <= 600:
        raise ConfigurationError(f"Provider {provider_id} timeoutSeconds must be 1-600")
    if not 0 <= retries <= 5:
        raise ConfigurationError(f"Provider {provider_id} transportRetries must be 0-5")
    return ProviderConfig(
        id=provider_id,
        type=provider_type,
        default_model=str(value.get("defaultModel") or "").strip(),
        base_url=base_url,
        chat_completions_path=str(value.get("chatCompletionsPath") or "/chat/completions"),
        api_key_env=str(value.get("apiKeyEnv") or "").strip(),
        timeout_seconds=timeout,
        transport_retries=retries,
        cache_identity=str(value.get("cacheIdentity") or "1").strip(),
    )
