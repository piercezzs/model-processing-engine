from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .exceptions import (
    ConfigurationError,
    ProviderConfigurationNotFoundError,
    ProviderEmptyContentError,
    ProviderError,
)
from .file_store import atomic_write_text
from .project_environment import read_env_file, update_project_environment
from .providers import default_provider_factories
from .providers.base import ProviderCallResult, ProviderConfig, load_provider_registry_data
from .providers.mock import MockProvider
from .providers.openai_compatible import OpenAICompatibleProvider
from .reasoning import (
    ReasoningEffortSetting,
    reasoning_capabilities,
    reasoning_capability,
    resolve_reasoning_effort,
)


PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TEST_TOKEN_TTL_SECONDS = 300
MAX_PROVIDER_CONFIG_BYTES = 256 * 1024
ADMIN_EDITABLE_PROVIDER_TYPES = frozenset({"mock", "openai_compatible"})
ProviderPresetId = Literal["openai", "deepseek", "custom", "mock"]

PROVIDER_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "openai",
        "label": "OpenAI",
        "type": "openai_compatible",
        "providerId": "openai",
        "baseUrl": "https://api.openai.com/v1",
        "chatCompletionsPath": "/chat/completions",
        "modelsPath": "/models",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "type": "openai_compatible",
        "providerId": "deepseek",
        "baseUrl": "https://api.deepseek.com",
        "chatCompletionsPath": "/chat/completions",
        "modelsPath": "/models",
    },
    {
        "id": "custom",
        "label": "自定义 OpenAI-compatible",
        "type": "openai_compatible",
        "providerId": "custom-provider",
        "baseUrl": "",
        "chatCompletionsPath": "/chat/completions",
        "modelsPath": "/models",
    },
    {
        "id": "mock",
        "label": "Mock（离线测试）",
        "type": "mock",
        "providerId": "mock",
        "baseUrl": "",
        "chatCompletionsPath": "/chat/completions",
        "modelsPath": "/models",
    },
)


class ProviderConnectionDraft(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    provider_id: str = Field(alias="providerId", min_length=1, max_length=64)
    type: Literal["mock", "openai_compatible"]
    preset_id: ProviderPresetId = Field(default="custom", alias="presetId")
    base_url: str = Field(default="", alias="baseUrl", max_length=2048)
    chat_completions_path: str = Field(
        default="/chat/completions",
        alias="chatCompletionsPath",
        max_length=512,
    )
    models_path: str = Field(default="/models", alias="modelsPath", max_length=512)
    api_key: SecretStr | None = Field(default=None, alias="apiKey", max_length=16_384)
    timeout_seconds: int = Field(default=60, alias="timeoutSeconds", ge=1, le=600)
    transport_retries: int = Field(default=2, alias="transportRetries", ge=0, le=5)
    max_concurrency: int = Field(default=8, alias="maxConcurrency", ge=1, le=64)
    native_json_schema: bool = Field(default=False, alias="nativeJsonSchema")

    @field_validator("provider_id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        normalized = value.strip()
        if not PROVIDER_ID_PATTERN.fullmatch(normalized):
            raise ValueError("providerId contains unsupported characters")
        return normalized

    @field_validator("base_url", "chat_completions_path", "models_path")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_provider_fields(self) -> "ProviderConnectionDraft":
        preset = next((item for item in PROVIDER_PRESETS if item["id"] == self.preset_id), None)
        if preset and self.preset_id != "custom" and preset["type"] != self.type:
            raise ValueError("presetId does not match the selected provider type")
        if self.type == "openai_compatible":
            if not self.base_url:
                raise ValueError("baseUrl is required for an OpenAI-compatible provider")
            parsed_base_url = urlparse(self.base_url)
            if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
                raise ValueError("baseUrl must be an absolute HTTP(S) URL")
            if parsed_base_url.username or parsed_base_url.password:
                raise ValueError("baseUrl must not contain embedded credentials")
            if parsed_base_url.query or parsed_base_url.fragment:
                raise ValueError("baseUrl must not contain a query or fragment")
            if not self.chat_completions_path.startswith("/"):
                raise ValueError("chatCompletionsPath must start with /")
            if not self.models_path.startswith("/"):
                raise ValueError("modelsPath must start with /")
        return self


class ModelDiscoveryRequest(ProviderConnectionDraft):
    pass


class ProviderDraft(ProviderConnectionDraft):
    model: str = Field(default="", max_length=256)
    available_models: list[str] = Field(
        default_factory=list,
        alias="availableModels",
        max_length=512,
    )
    default_reasoning_effort: ReasoningEffortSetting = Field(
        default="auto",
        alias="defaultReasoningEffort",
    )

    @field_validator("model")
    @classmethod
    def strip_model(cls, value: str) -> str:
        return value.strip()

    @field_validator("available_models")
    @classmethod
    def validate_available_models(cls, values: list[str]) -> list[str]:
        normalized = [str(value).strip() for value in values]
        if any(not value or len(value) > 256 for value in normalized):
            raise ValueError("availableModels contains an invalid model ID")
        if len(set(normalized)) != len(normalized):
            raise ValueError("availableModels must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_model(self) -> "ProviderDraft":
        if self.type == "openai_compatible" and not self.model:
            raise ValueError("model is required for an OpenAI-compatible provider")
        capability = reasoning_capability(self.type, self.model or "schema-sample-v1")
        if (
            self.default_reasoning_effort != "auto"
            and self.default_reasoning_effort not in capability.supported_efforts
        ):
            supported = ", ".join(("auto", *capability.supported_efforts))
            raise ValueError(
                f"defaultReasoningEffort is not supported by {self.model!r}; "
                f"supported values: {supported}"
            )
        return self


class ApplyProviderRequest(ProviderDraft):
    verification_token: str = Field(alias="verificationToken", min_length=32, max_length=256)


class AdminConfigManager:
    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir.resolve()
        self.env_path = self.project_dir / ".env"
        self.template_path = self.project_dir / "config" / "providers.json"
        self.local_provider_path = self.project_dir / "config" / "providers.local.json"
        self._lock = threading.Lock()
        self._verification_digest_key = secrets.token_bytes(32)
        self._verified: dict[str, tuple[str, float]] = {}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            raw = self._read_provider_config()
            env_values = read_env_file(self.env_path)
            providers: list[dict[str, Any]] = []
            default_id = str(raw.get("defaultProviderId") or "")
            for provider_id, value in sorted(dict(raw.get("providers") or {}).items()):
                if not isinstance(value, dict):
                    continue
                provider_type = str(value.get("type") or "")
                credential_env = str(value.get("apiKeyEnv") or "")
                providers.append(
                    {
                        "id": str(provider_id),
                        "type": provider_type,
                        "editable": provider_type in ADMIN_EDITABLE_PROVIDER_TYPES,
                        "presetId": str(
                            value.get("presetId")
                            or _infer_preset_id(
                                provider_type,
                                str(value.get("baseUrl") or ""),
                            )
                        ),
                        "baseUrl": str(value.get("baseUrl") or ""),
                        "chatCompletionsPath": str(
                            value.get("chatCompletionsPath") or "/chat/completions"
                        ),
                        "modelsPath": str(value.get("modelsPath") or "/models"),
                        "defaultModel": str(value.get("defaultModel") or ""),
                        "availableModels": [
                            str(item) for item in value.get("availableModels", [])
                        ],
                        "defaultReasoningEffort": str(
                            value.get("defaultReasoningEffort") or "auto"
                        ),
                        "modelReasoningCapabilities": reasoning_capabilities(
                            provider_type,
                            [
                                str(item) for item in value.get("availableModels", [])
                                if str(item).strip()
                            ]
                            + [str(value.get("defaultModel") or "")],
                        ),
                        "timeoutSeconds": int(
                            value.get("timeoutSeconds")
                            if value.get("timeoutSeconds") is not None
                            else 60
                        ),
                        "transportRetries": int(
                            value.get("transportRetries")
                            if value.get("transportRetries") is not None
                            else 2
                        ),
                        "maxConcurrency": int(value.get("maxConcurrency") or 8),
                        "nativeJsonSchema": (
                            "native_json_schema"
                            in {
                                str(item)
                                for item in value.get("capabilities", [])
                            }
                        ),
                        "capabilities": [
                            str(item) for item in value.get("capabilities", [])
                        ],
                        "webSearch": str(value.get("webSearch") or "disabled"),
                        "credentialConfigured": bool(
                            credential_env and env_values.get(credential_env, "").strip()
                        ),
                        "active": str(provider_id) == default_id,
                    }
                )
            return {
                "projectDir": str(self.project_dir),
                "envPath": str(self.env_path),
                "providerConfigPath": str(self.local_provider_path),
                "activeProviderId": default_id,
                "activeModel": str(env_values.get("MPE_ACTIVE_MODEL") or ""),
                "providerPresets": [dict(preset) for preset in PROVIDER_PRESETS],
                "providers": providers,
            }

    def discover_models(self, draft: ModelDiscoveryRequest) -> dict[str, Any]:
        with self._lock:
            if draft.type == "mock":
                models = ["schema-sample-v1"]
                return {
                    "status": "ok",
                    "providerId": draft.provider_id,
                    "models": models,
                    "modelReasoningCapabilities": reasoning_capabilities(
                        draft.type,
                        models,
                    ),
                    "elapsedMs": 0,
                    "attempts": 1,
                }
            api_key = self._resolved_api_key(draft)
            config = ProviderConfig(
                id=draft.provider_id,
                type=draft.type,
                default_model="",
                base_url=draft.base_url.rstrip("/"),
                chat_completions_path=draft.chat_completions_path,
                api_key_env=self._credential_env(draft.provider_id),
                timeout_seconds=draft.timeout_seconds,
                transport_retries=draft.transport_retries,
                max_concurrency=draft.max_concurrency,
                cache_identity=f"{draft.provider_id}-models",
            )
            provider = OpenAICompatibleProvider(
                config,
                credential_resolver=lambda _name: api_key,
            )
            result = provider.list_models(models_path=draft.models_path)
            models = [str(item) for item in result.get("models", [])]
            return {
                "status": "ok",
                "providerId": draft.provider_id,
                **result,
                "modelReasoningCapabilities": reasoning_capabilities(
                    draft.type,
                    models,
                ),
            }

    def test_provider(self, draft: ProviderDraft) -> dict[str, Any]:
        with self._lock:
            api_key = self._resolved_api_key(draft)
            result = self._call_provider(draft, api_key=api_key)
            digest = self._draft_digest(draft, api_key=api_key)
            token = secrets.token_urlsafe(32)
            self._discard_expired_tokens()
            self._verified[token] = (digest, time.monotonic() + TEST_TOKEN_TTL_SECONDS)
            return {
                "status": "ok",
                "providerId": draft.provider_id,
                "model": draft.model or "schema-sample-v1",
                "reasoningEffort": draft.default_reasoning_effort,
                "elapsedMs": result["elapsedMs"],
                "usage": dict(result.get("usage") or {}),
                "providerCallCount": int(result.get("providerCallCount") or 0),
                "transportRetries": int(result.get("transportRetries") or 0),
                "verificationToken": token,
                "expiresInSeconds": TEST_TOKEN_TTL_SECONDS,
            }

    def apply_provider(self, request: ApplyProviderRequest) -> dict[str, Any]:
        draft = ProviderDraft.model_validate(
            request.model_dump(by_alias=True, exclude={"verification_token"})
        )
        with self._lock:
            api_key = self._resolved_api_key(draft)
            digest = self._draft_digest(draft, api_key=api_key)
            self._discard_expired_tokens()
            verified = self._verified.pop(request.verification_token, None)
            if (
                verified is None
                or verified[1] < time.monotonic()
                or not hmac.compare_digest(verified[0], digest)
            ):
                raise ConfigurationError("Provider verification expired or no longer matches")

            raw = self._read_provider_config()
            providers = dict(raw.get("providers") or {})
            existing_provider = providers.get(draft.provider_id)
            existing_credential_env = (
                str(existing_provider.get("apiKeyEnv") or "").strip()
                if isinstance(existing_provider, dict)
                else ""
            )
            credential_env = (
                existing_credential_env or self._credential_env(draft.provider_id)
                if draft.type != "mock"
                else ""
            )
            model = draft.model or "schema-sample-v1"
            available_models = list(draft.available_models)
            if model not in available_models:
                available_models.append(model)
            providers[draft.provider_id] = {
                "type": draft.type,
                "presetId": "mock" if draft.type == "mock" else draft.preset_id,
                "defaultModel": model,
                "defaultReasoningEffort": draft.default_reasoning_effort,
                # Every successful activation starts a fresh cache namespace. This
                # avoids sharing cached responses after an account or key change
                # without persisting a secret-derived identifier.
                "cacheIdentity": f"{draft.provider_id}-local-{secrets.token_hex(8)}",
                "availableModels": available_models,
                "capabilities": [
                    "structured_json",
                    "text_stream",
                    *(
                        ["native_json_schema"]
                        if draft.native_json_schema
                        else []
                    ),
                ],
                "maxConcurrency": draft.max_concurrency,
                **(
                    {
                        "baseUrl": draft.base_url.rstrip("/"),
                        "chatCompletionsPath": draft.chat_completions_path,
                        "modelsPath": draft.models_path,
                        "apiKeyEnv": credential_env,
                        "timeoutSeconds": draft.timeout_seconds,
                        "transportRetries": draft.transport_retries,
                    }
                    if draft.type == "openai_compatible"
                    else {}
                ),
            }
            updated = {
                "version": 1,
                "defaultProviderId": draft.provider_id,
                "providers": providers,
            }
            self._validate_provider_config(updated)
            serialized = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
            if len(serialized.encode("utf-8")) > MAX_PROVIDER_CONFIG_BYTES:
                raise ConfigurationError("Provider configuration is too large")
            atomic_write_text(self.local_provider_path, serialized, mode=0o600)

            env_updates = {
                "MPE_PROJECT_DIR": str(self.project_dir),
                "MPE_PROVIDER_CONFIG": str(self.local_provider_path),
                "MPE_ACTIVE_PROVIDER": draft.provider_id,
                "MPE_ACTIVE_MODEL": model,
            }
            submitted_key = draft.api_key.get_secret_value() if draft.api_key else ""
            if credential_env and submitted_key:
                env_updates[credential_env] = submitted_key
            updated_env = update_project_environment(self.project_dir, env_updates)
            # Carry only the values managed by this activation into the detached
            # restart. Unrelated values in .env must not override a shell-level
            # environment setting merely because the admin page was used.
            for key in env_updates:
                os.environ[key] = updated_env[key]
            if credential_env:
                os.environ[credential_env] = api_key
            return {
                "status": "saved",
                "providerId": draft.provider_id,
                "model": model,
                "reasoningEffort": draft.default_reasoning_effort,
                "restartRequired": True,
            }

    def delete_provider(self, provider_id: str) -> dict[str, Any]:
        normalized_id = provider_id.strip()
        if not PROVIDER_ID_PATTERN.fullmatch(normalized_id):
            raise ConfigurationError("providerId contains unsupported characters")

        with self._lock:
            raw = self._read_provider_config()
            providers = dict(raw.get("providers") or {})
            existing_provider = providers.get(normalized_id)
            if not isinstance(existing_provider, dict):
                raise ProviderConfigurationNotFoundError(
                    f"Provider configuration does not exist: {normalized_id}"
                )
            default_id = str(raw.get("defaultProviderId") or "")
            if normalized_id == default_id:
                raise ConfigurationError(
                    "The active Provider cannot be deleted; activate another Provider first"
                )
            if len(providers) <= 1:
                raise ConfigurationError("At least one Provider configuration must remain")

            credential_env = str(existing_provider.get("apiKeyEnv") or "").strip()
            del providers[normalized_id]
            credential_is_shared = bool(
                credential_env
                and any(
                    isinstance(provider, dict)
                    and str(provider.get("apiKeyEnv") or "").strip() == credential_env
                    for provider in providers.values()
                )
            )
            env_values = read_env_file(self.env_path)
            remove_credential_from_file = bool(
                credential_env
                and not credential_is_shared
                and credential_env in env_values
            )
            credential_removed = bool(
                credential_env
                and not credential_is_shared
                and (remove_credential_from_file or credential_env in os.environ)
            )

            updated = {
                "version": 1,
                "defaultProviderId": default_id,
                "providers": providers,
            }
            self._validate_provider_config(updated)
            serialized = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
            if len(serialized.encode("utf-8")) > MAX_PROVIDER_CONFIG_BYTES:
                raise ConfigurationError("Provider configuration is too large")
            original_serialized = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
            atomic_write_text(self.local_provider_path, serialized, mode=0o600)
            try:
                if remove_credential_from_file:
                    update_project_environment(
                        self.project_dir,
                        {},
                        removals=(credential_env,),
                    )
            except Exception:
                atomic_write_text(
                    self.local_provider_path,
                    original_serialized,
                    mode=0o600,
                )
                raise

            if credential_removed:
                os.environ.pop(credential_env, None)
            self._verified.clear()
            return {
                "status": "deleted",
                "providerId": normalized_id,
                "credentialRemoved": credential_removed,
                "restartRequired": True,
            }

    def _read_provider_config(self) -> dict[str, Any]:
        path = self.local_provider_path if self.local_provider_path.is_file() else self.template_path
        try:
            if path.stat().st_size > MAX_PROVIDER_CONFIG_BYTES:
                raise ConfigurationError(f"Provider configuration is too large: {path}")
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(f"Provider configuration not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"Invalid provider configuration JSON: {path}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("Provider configuration must be a JSON object")
        self._validate_provider_config(raw)
        return raw

    def _validate_provider_config(self, raw: dict[str, Any]) -> None:
        load_provider_registry_data(
            raw,
            factories=default_provider_factories(),
        )

    def _resolved_api_key(self, draft: ProviderConnectionDraft) -> str:
        if draft.type == "mock":
            return ""
        submitted = draft.api_key.get_secret_value().strip() if draft.api_key else ""
        if submitted:
            return submitted
        credential_env = self._existing_credential_env(draft.provider_id)
        configured = read_env_file(self.env_path).get(credential_env, "")
        if not configured.strip():
            raise ConfigurationError("An API key is required before testing this provider")
        return configured.strip()

    def _existing_credential_env(self, provider_id: str) -> str:
        raw = self._read_provider_config()
        provider = dict(raw.get("providers") or {}).get(provider_id)
        if isinstance(provider, dict):
            configured = str(provider.get("apiKeyEnv") or "").strip()
            if configured:
                return configured
        return self._credential_env(provider_id)

    @staticmethod
    def _call_provider(draft: ProviderDraft, *, api_key: str) -> dict[str, Any]:
        model = draft.model or "schema-sample-v1"
        config = ProviderConfig(
            id=draft.provider_id,
            type=draft.type,
            default_model=model,
            base_url=draft.base_url.rstrip("/"),
            chat_completions_path=draft.chat_completions_path,
            api_key_env=AdminConfigManager._credential_env(draft.provider_id),
            timeout_seconds=draft.timeout_seconds,
            transport_retries=draft.transport_retries,
            max_concurrency=draft.max_concurrency,
            cache_identity=f"{draft.provider_id}-test",
            available_models=(model,),
            capabilities=(
                "structured_json",
                "text_stream",
                *(
                    ("native_json_schema",)
                    if draft.native_json_schema
                    else ()
                ),
            ),
            default_reasoning_effort=draft.default_reasoning_effort,
        )
        reasoning = resolve_reasoning_effort(
            provider_type=draft.type,
            model=model,
            runtime_setting=None,
            task_setting=None,
            provider_setting=draft.default_reasoning_effort,
        )
        call_arguments = {
            "model": model,
            "system_prompt": 'Return one JSON object with exactly {"status":"ok"}.',
            "input_payload": {"operation": "mpe_provider_connection_test"},
            "output_schema": {
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
                "additionalProperties": False,
            },
            "temperature": 0,
            "max_tokens": 256,
            "reasoning_effort": reasoning.effective,
        }
        provider_calls: list[dict[str, Any]] = []
        if draft.type == "mock":
            try:
                call = MockProvider(config).call_json(**call_arguments)
            except ProviderError as exc:
                exc.audit_calls = [_probe_error_record(exc)]
                raise
        else:
            provider = OpenAICompatibleProvider(
                config,
                credential_resolver=lambda _name: api_key,
            )
            extra_body = (
                {"thinking": {"type": "disabled"}}
                if draft.preset_id == "deepseek"
                else None
            )
            for attempt in range(2):
                try:
                    call = provider.call_json(
                        **call_arguments,
                        extra_body=extra_body,
                    )
                    break
                except ProviderEmptyContentError as exc:
                    provider_calls.append(_probe_error_record(exc))
                    if attempt == 1:
                        exc.audit_calls = list(provider_calls)
                        raise
                except ProviderError as exc:
                    provider_calls.extend(exc.audit_calls or [_probe_error_record(exc)])
                    exc.audit_calls = list(provider_calls)
                    raise
        if not isinstance(call.content, dict):
            raise ConfigurationError("Provider test did not return a JSON object")
        provider_calls.append(_probe_success_record(call))
        usage_fields = (
            "inputTokens",
            "outputTokens",
            "totalTokens",
            "cacheReadInputTokens",
        )
        usage = {
            "available": any(bool(item["usage"].get("available")) for item in provider_calls),
            **{
                field: sum(int(item["usage"].get(field) or 0) for item in provider_calls)
                for field in usage_fields
            },
            "source": "provider_probe_calls",
        }
        return {
            "elapsedMs": sum(int(item["elapsedMs"]) for item in provider_calls),
            "usage": usage,
            "providerCallCount": len(provider_calls),
            "transportRetries": sum(
                max(0, int(item["attempts"]) - 1) for item in provider_calls
            ),
            "providerCalls": provider_calls,
        }

    @staticmethod
    def _credential_env(provider_id: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9]", "_", provider_id).upper()
        suffix = hashlib.sha256(provider_id.encode("utf-8")).hexdigest()[:8].upper()
        return f"MPE_PROVIDER_{normalized}_{suffix}_API_KEY"

    def _draft_digest(self, draft: ProviderDraft, *, api_key: str) -> str:
        payload = draft.model_dump(by_alias=True, exclude={"api_key"})
        payload["apiKey"] = api_key
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hmac.new(
            self._verification_digest_key,
            serialized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _discard_expired_tokens(self) -> None:
        now = time.monotonic()
        expired = [token for token, value in self._verified.items() if value[1] < now]
        for token in expired:
            self._verified.pop(token, None)


def _infer_preset_id(provider_type: str, base_url: str) -> ProviderPresetId:
    if provider_type == "mock":
        return "mock"
    normalized = base_url.strip().rstrip("/").casefold()
    for preset in PROVIDER_PRESETS:
        if preset["id"] in {"custom", "mock"}:
            continue
        if normalized == str(preset["baseUrl"]).rstrip("/").casefold():
            return preset["id"]
    return "custom"


def _probe_success_record(call: ProviderCallResult) -> dict[str, Any]:
    return {
        "status": "succeeded",
        "usage": dict(call.usage),
        "attempts": max(0, int(call.attempts)),
        "elapsedMs": max(0, int(call.elapsed_ms)),
        "error": "",
    }


def _probe_error_record(error: ProviderError) -> dict[str, Any]:
    return {
        "status": "failed",
        "usage": dict(error.usage),
        "attempts": max(0, int(error.attempts)),
        "elapsedMs": max(0, int(error.elapsed_ms)),
        "error": error.audit_message,
    }
