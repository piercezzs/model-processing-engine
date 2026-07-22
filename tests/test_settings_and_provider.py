from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_processing_engine.exceptions import ConfigurationError
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
                },
            }
        )
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "secret"}, clear=True):
            with patch("urllib.request.urlopen", return_value=response):
                result = provider.call_json(
                    model="",
                    system_prompt="Return JSON",
                    input_payload={"input": {"text": "hello"}},
                    output_schema={"type": "object"},
                    temperature=0,
                    max_tokens=None,
                )
        self.assertEqual(result.content, {"answer": True})
        self.assertEqual(result.usage["totalTokens"], 5)

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
        self.assertNotIn("apiKeyEnv", descriptor)


if __name__ == "__main__":
    unittest.main()
