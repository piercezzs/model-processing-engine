from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_processing_engine.exceptions import ConfigurationError, ProviderEmptyContentError
from model_processing_engine.providers.base import ProviderConfig, ProviderRegistry
from model_processing_engine.providers.openai_compatible import OpenAICompatibleProvider
from model_processing_engine.settings import PACKAGE_PROVIDER_CONFIG, load_settings


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class SettingsAndProviderTests(unittest.TestCase):
    def test_default_runtime_root_uses_mpe_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"MPE_HOME": temp_dir}, clear=True):
                settings = load_settings()
            self.assertEqual(settings.root, Path(temp_dir).resolve())
            self.assertEqual(settings.log_dir, settings.root / "logs")
            self.assertEqual(settings.run_dir, settings.root / "run")
            self.assertEqual(len(settings.instance_id), 16)

    def test_installed_mode_uses_packaged_mock_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            settings = load_settings(temp_dir)
            self.assertEqual(settings.provider_config_path, PACKAGE_PROVIDER_CONFIG.resolve())

    def test_remote_binding_requires_opt_in_and_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"MPE_HOST": "0.0.0.0"}, clear=True):
                with self.assertRaises(ConfigurationError):
                    load_settings(temp_dir)
            with patch.dict(
                os.environ,
                {"MPE_HOST": "0.0.0.0", "MPE_ALLOW_REMOTE": "1"},
                clear=True,
            ):
                with self.assertRaises(ConfigurationError):
                    load_settings(temp_dir)
            with patch.dict(
                os.environ,
                {
                    "MPE_HOST": "0.0.0.0",
                    "MPE_ALLOW_REMOTE": "1",
                    "MPE_API_TOKEN": "secret",
                },
                clear=True,
            ):
                settings = load_settings(temp_dir)
                self.assertEqual(settings.api_token, "secret")

    def test_async_queue_limits_are_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "MPE_ASYNC_WORKERS": "3",
                "MPE_ASYNC_QUEUE_CAPACITY": "17",
            },
            clear=True,
        ):
            settings = load_settings(temp_dir)

        self.assertEqual(settings.async_worker_count, 3)
        self.assertEqual(settings.async_queue_capacity, 17)

    def test_openai_compatible_provider_parses_json_and_usage(self) -> None:
        config = ProviderConfig(
            id="test",
            type="openai_compatible",
            default_model="test-model",
            base_url="https://example.invalid/v1",
            api_key_env="TEST_PROVIDER_KEY",
        )
        provider = OpenAICompatibleProvider(config)
        response = _Response(
            {
                "choices": [{"message": {"content": "```json\n{\"answer\": true}\n```"}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 2},
                    "prompt_cache_hit_tokens": 1,
                },
            }
        )
        model_payload = {
            "outputSchema": {"type": "object"},
            "taxonomy": {"types": ["book"]},
            "input": {"text": "hello"},
        }
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "secret"}, clear=True):
            with patch(
                "model_processing_engine.providers.openai_compatible._open_without_redirects",
                return_value=response,
            ) as urlopen:
                result = provider.call_json(
                    model="",
                    system_prompt="Return JSON",
                    input_payload=model_payload,
                    output_schema={"type": "object"},
                    temperature=0,
                    max_tokens=None,
                )
        self.assertEqual(result.content, {"answer": True})
        self.assertEqual(result.usage["totalTokens"], 5)
        self.assertEqual(result.usage["cacheReadInputTokens"], 2)
        request_payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        serialized_model_payload = json.loads(
            request_payload["messages"][1]["content"]
        )
        self.assertEqual(
            list(serialized_model_payload),
            ["outputSchema", "taxonomy", "input"],
        )

    def test_openai_compatible_provider_parses_deepseek_prompt_cache_usage(self) -> None:
        provider = OpenAICompatibleProvider(
            ProviderConfig(
                id="test",
                type="openai_compatible",
                default_model="test-model",
                base_url="https://example.invalid/v1",
                api_key_env="TEST_PROVIDER_KEY",
            )
        )
        response = _Response(
            {
                "choices": [{"message": {"content": '{"answer":true}'}}],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "total_tokens": 10,
                    "prompt_cache_hit_tokens": 6,
                    "prompt_cache_miss_tokens": 2,
                },
            }
        )
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "secret"}, clear=True):
            with patch(
                "model_processing_engine.providers.openai_compatible._open_without_redirects",
                return_value=response,
            ):
                result = provider.call_json(
                    model="",
                    system_prompt="Return JSON",
                    input_payload={"input": {"text": "hello"}},
                    output_schema={"type": "object"},
                    temperature=0,
                    max_tokens=None,
                )

        self.assertEqual(result.usage["cacheReadInputTokens"], 6)

    def test_native_json_schema_capability_uses_provider_schema_mode(self) -> None:
        provider = OpenAICompatibleProvider(
            ProviderConfig(
                id="test",
                type="openai_compatible",
                default_model="test-model",
                base_url="https://example.invalid/v1",
                api_key_env="TEST_PROVIDER_KEY",
                capabilities=("structured_json", "native_json_schema"),
            )
        )
        response = _Response(
            {"choices": [{"message": {"content": '{"answer":true}'}}]}
        )
        output_schema = {
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "boolean"}},
            "additionalProperties": False,
        }
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "secret"}, clear=True):
            with patch(
                "model_processing_engine.providers.openai_compatible._open_without_redirects",
                return_value=response,
            ) as urlopen:
                provider.call_json(
                    model="",
                    system_prompt="Return JSON",
                    input_payload={"input": {"text": "hello"}},
                    output_schema=output_schema,
                    temperature=0,
                    max_tokens=None,
                )

        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(
            payload["response_format"]["json_schema"]["schema"],
            output_schema,
        )

    def test_contract_repair_appends_previous_output_and_feedback(self) -> None:
        provider = OpenAICompatibleProvider(
            ProviderConfig(
                id="test",
                type="openai_compatible",
                default_model="test-model",
                base_url="https://example.invalid/v1",
                api_key_env="TEST_PROVIDER_KEY",
            )
        )
        response = _Response(
            {"choices": [{"message": {"content": '{"summary":"fixed"}'}}]}
        )
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "secret"}, clear=True):
            with patch(
                "model_processing_engine.providers.openai_compatible._open_without_redirects",
                return_value=response,
            ) as urlopen:
                provider.call_json(
                    model="",
                    system_prompt="Return JSON",
                    input_payload={"input": {"text": "hello"}},
                    output_schema={"type": "object"},
                    temperature=0,
                    max_tokens=None,
                    repair_feedback="missing required property summary",
                    previous_output={"wrong": True},
                )

        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual([message["role"] for message in payload["messages"]], [
            "system",
            "user",
            "assistant",
            "user",
        ])
        self.assertIn("missing required property", payload["messages"][-1]["content"])

    def test_empty_provider_content_preserves_usage_for_audit(self) -> None:
        provider = OpenAICompatibleProvider(
            ProviderConfig(
                id="test",
                type="openai_compatible",
                default_model="test-model",
                base_url="https://example.invalid/v1",
                api_key_env="TEST_PROVIDER_KEY",
            )
        )
        response = _Response(
            {
                "choices": [{"message": {"content": ""}}],
                "usage": {
                    "prompt_tokens": 6,
                    "completion_tokens": 1,
                    "total_tokens": 7,
                    "prompt_cache_hit_tokens": 4,
                },
            }
        )
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "secret"}, clear=True):
            with patch(
                "model_processing_engine.providers.openai_compatible._open_without_redirects",
                return_value=response,
            ):
                with self.assertRaises(ProviderEmptyContentError) as raised:
                    provider.call_json(
                        model="",
                        system_prompt="Return JSON",
                        input_payload={},
                        output_schema={"type": "object"},
                        temperature=0,
                        max_tokens=32,
                    )

        self.assertEqual(raised.exception.usage["totalTokens"], 7)
        self.assertEqual(raised.exception.usage["cacheReadInputTokens"], 4)
        self.assertEqual(raised.exception.attempts, 1)

    def test_openai_compatible_provider_adds_non_conflicting_extra_body(self) -> None:
        provider = OpenAICompatibleProvider(
            ProviderConfig(
                id="test",
                type="openai_compatible",
                default_model="test-model",
                base_url="https://example.invalid/v1",
                api_key_env="TEST_PROVIDER_KEY",
            )
        )
        response = _Response(
            {"choices": [{"message": {"content": '{"status":"ok"}'}}]}
        )
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "secret"}, clear=True):
            with patch(
                "model_processing_engine.providers.openai_compatible._open_without_redirects",
                return_value=response,
            ) as urlopen:
                provider.call_json(
                    model="",
                    system_prompt="Return JSON",
                    input_payload={},
                    output_schema={"type": "object"},
                    temperature=0,
                    max_tokens=256,
                    extra_body={"thinking": {"type": "disabled"}},
                )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["max_tokens"], 256)
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_openai_compatible_provider_requires_environment_secret(self) -> None:
        provider = OpenAICompatibleProvider(
            ProviderConfig(
                id="test",
                type="openai_compatible",
                default_model="test-model",
                base_url="https://example.invalid/v1",
                api_key_env="TEST_PROVIDER_KEY",
            )
        )
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError):
                provider.call_json(
                    model="",
                    system_prompt="Return JSON",
                    input_payload={},
                    output_schema={"type": "object"},
                    temperature=0,
                    max_tokens=None,
                )

    def test_openai_compatible_provider_lists_unique_models(self) -> None:
        provider = OpenAICompatibleProvider(
            ProviderConfig(
                id="test",
                type="openai_compatible",
                default_model="",
                base_url="https://example.invalid/v1",
                api_key_env="TEST_PROVIDER_KEY",
            )
        )
        response = _Response(
            {
                "object": "list",
                "data": [
                    {"id": "model-two"},
                    {"id": "model-one"},
                    {"id": "model-two"},
                    {"object": "model"},
                ],
            }
        )
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "secret"}, clear=True):
            with patch(
                "model_processing_engine.providers.openai_compatible._open_without_redirects",
                return_value=response,
            ) as urlopen:
                result = provider.list_models(models_path="/models")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.invalid/v1/models")
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(result["models"], ["model-two", "model-one"])
        self.assertEqual(result["attempts"], 1)

    def test_provider_cache_identity_changes_provider_digest(self) -> None:
        first = ProviderConfig(
            id="test",
            type="mock",
            default_model="mock-v1",
            cache_identity="account-one",
        )
        second = ProviderConfig(
            id="test",
            type="mock",
            default_model="mock-v1",
            cache_identity="account-two",
        )
        self.assertNotEqual(first.digest, second.digest)

    def test_provider_descriptor_exposes_models_without_secrets(self) -> None:
        config = ProviderConfig(
            id="test",
            type="openai_compatible",
            default_model="model-one",
            base_url="https://example.invalid/v1",
            api_key_env="SECRET_KEY",
            available_models=("model-one",),
            capabilities=("structured_json",),
        )
        provider = OpenAICompatibleProvider(config)
        registry = ProviderRegistry({"test": provider}, default_provider_id="test")
        descriptor = registry.descriptors()[0]
        self.assertEqual(descriptor["availableModels"], ["model-one"])
        self.assertEqual(descriptor["maxConcurrency"], 8)
        self.assertNotIn("apiKeyEnv", descriptor)


if __name__ == "__main__":
    unittest.main()
