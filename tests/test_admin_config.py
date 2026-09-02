from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_processing_engine.admin_config import (
    AdminConfigManager,
    ApplyProviderRequest,
    ModelDiscoveryRequest,
    ProviderDraft,
)
from model_processing_engine.exceptions import (
    ConfigurationError,
    ProviderEmptyContentError,
    ProviderError,
)
from model_processing_engine.providers.base import ProviderCallResult


class AdminConfigManagerTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        config_dir = root / "config"
        config_dir.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "model-processing-engine"\n',
            encoding="utf-8",
        )
        (config_dir / "providers.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "defaultProviderId": "mock",
                    "providers": {
                        "mock": {"type": "mock", "defaultModel": "schema-sample-v1"}
                    },
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_mock_provider_test_then_apply_writes_project_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            project = self._project(Path(temp_dir))
            manager = AdminConfigManager(project)
            draft = ProviderDraft.model_validate(
                {
                    "providerId": "mock",
                    "type": "mock",
                    "model": "schema-sample-v1",
                    "maxConcurrency": 3,
                }
            )
            tested = manager.test_provider(draft)
            request = ApplyProviderRequest.model_validate(
                {
                    **draft.model_dump(by_alias=True),
                    "verificationToken": tested["verificationToken"],
                }
            )
            applied = manager.apply_provider(request)

            self.assertEqual(applied["status"], "saved")
            env_text = (project / ".env").read_text(encoding="utf-8")
            self.assertIn('MPE_ACTIVE_PROVIDER="mock"', env_text)
            self.assertNotIn("apiKey", env_text)
            local = json.loads(
                (project / "config" / "providers.local.json").read_text(encoding="utf-8")
            )
            self.assertEqual(local["defaultProviderId"], "mock")
            self.assertEqual(local["providers"]["mock"]["maxConcurrency"], 3)
            self.assertTrue(manager.snapshot()["providers"][0]["active"])
            self.assertEqual(manager.snapshot()["providers"][0]["maxConcurrency"], 3)

    def test_openai_key_is_written_only_after_verified_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            project = self._project(Path(temp_dir))
            manager = AdminConfigManager(project)
            draft = ProviderDraft.model_validate(
                {
                    "providerId": "private-model",
                    "type": "openai_compatible",
                    "baseUrl": "https://models.example/v1",
                    "model": "model-one",
                    "presetId": "custom",
                    "modelsPath": "/models",
                    "availableModels": ["model-one", "model-two"],
                    "nativeJsonSchema": True,
                    "apiKey": "local-secret",
                }
            )
            with patch.object(
                manager,
                "_call_provider",
                return_value={"elapsedMs": 12},
            ):
                tested = manager.test_provider(draft)

            self.assertFalse((project / ".env").exists())
            request = ApplyProviderRequest.model_validate(
                {
                    **draft.model_dump(by_alias=True),
                    "verificationToken": tested["verificationToken"],
                }
            )
            manager.apply_provider(request)

            env_text = (project / ".env").read_text(encoding="utf-8")
            self.assertIn("local-secret", env_text)
            snapshot = manager.snapshot()
            selected = next(item for item in snapshot["providers"] if item["id"] == "private-model")
            self.assertTrue(selected["credentialConfigured"])
            self.assertEqual(selected["availableModels"], ["model-one", "model-two"])
            self.assertEqual(selected["modelsPath"], "/models")
            self.assertEqual(selected["maxConcurrency"], 8)
            self.assertTrue(selected["nativeJsonSchema"])
            self.assertEqual(
                selected["modelReasoningCapabilities"]["model-one"]["options"],
                ["auto"],
            )
            local = json.loads(
                (project / "config" / "providers.local.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "native_json_schema",
                local["providers"]["private-model"]["capabilities"],
            )

    def test_reasoning_effort_is_tested_and_persisted_for_supported_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            project = self._project(Path(temp_dir))
            manager = AdminConfigManager(project)
            draft = ProviderDraft.model_validate(
                {
                    "providerId": "sub2api",
                    "type": "openai_compatible",
                    "baseUrl": "http://127.0.0.1:8080/v1",
                    "model": "gpt-5.6-sol",
                    "availableModels": ["gpt-5.6-sol"],
                    "defaultReasoningEffort": "xhigh",
                    "apiKey": "local-secret",
                }
            )
            with patch.object(
                manager,
                "_call_provider",
                return_value={"elapsedMs": 12},
            ):
                tested = manager.test_provider(draft)
            request = ApplyProviderRequest.model_validate(
                {
                    **draft.model_dump(by_alias=True),
                    "verificationToken": tested["verificationToken"],
                }
            )
            manager.apply_provider(request)

            local = json.loads(
                (project / "config" / "providers.local.json").read_text(encoding="utf-8")
            )
            self.assertEqual(tested["reasoningEffort"], "xhigh")
            self.assertEqual(
                local["providers"]["sub2api"]["defaultReasoningEffort"],
                "xhigh",
            )
            snapshot = manager.snapshot()["providers"]
            selected = next(item for item in snapshot if item["id"] == "sub2api")
            self.assertIn(
                "max",
                selected["modelReasoningCapabilities"]["gpt-5.6-sol"]["options"],
            )

    def test_model_discovery_uses_key_without_persisting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            project = self._project(Path(temp_dir))
            manager = AdminConfigManager(project)
            request = ModelDiscoveryRequest.model_validate(
                {
                    "providerId": "deepseek",
                    "presetId": "deepseek",
                    "type": "openai_compatible",
                    "baseUrl": "https://api.deepseek.com",
                    "modelsPath": "/models",
                    "apiKey": "temporary-secret",
                }
            )
            with patch(
                "model_processing_engine.admin_config.OpenAICompatibleProvider.list_models",
                return_value={
                    "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
                    "elapsedMs": 8,
                    "attempts": 1,
                },
            ):
                result = manager.discover_models(request)

            self.assertEqual(result["models"], ["deepseek-v4-flash", "deepseek-v4-pro"])
            self.assertFalse((project / ".env").exists())

    def test_deepseek_probe_disables_thinking_and_retries_one_empty_response(self) -> None:
        draft = ProviderDraft.model_validate(
            {
                "providerId": "deepseek",
                "presetId": "deepseek",
                "type": "openai_compatible",
                "baseUrl": "https://api.deepseek.com",
                "model": "deepseek-v4-flash",
            }
        )
        successful = ProviderCallResult(
            content={"status": "ok"},
            usage={},
            attempts=1,
            elapsed_ms=9,
        )
        with patch(
            "model_processing_engine.admin_config.OpenAICompatibleProvider.call_json",
            side_effect=[ProviderEmptyContentError("empty"), successful],
        ) as call_json:
            result = AdminConfigManager._call_provider(draft, api_key="temporary-secret")

        self.assertEqual(result["elapsedMs"], 9)
        self.assertEqual(result["providerCallCount"], 2)
        self.assertEqual(result["transportRetries"], 0)
        self.assertEqual(call_json.call_count, 2)
        self.assertEqual(call_json.call_args.kwargs["max_tokens"], 256)
        self.assertEqual(
            call_json.call_args.kwargs["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    def test_provider_probe_does_not_retry_other_provider_errors(self) -> None:
        draft = ProviderDraft.model_validate(
            {
                "providerId": "custom-provider",
                "presetId": "custom",
                "type": "openai_compatible",
                "baseUrl": "https://models.example/v1",
                "model": "model-one",
            }
        )
        with patch(
            "model_processing_engine.admin_config.OpenAICompatibleProvider.call_json",
            side_effect=ProviderError("invalid response"),
        ) as call_json:
            with self.assertRaisesRegex(ProviderError, "invalid response"):
                AdminConfigManager._call_provider(draft, api_key="temporary-secret")

        self.assertEqual(call_json.call_count, 1)

    def test_existing_provider_keeps_its_credential_environment_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            project = self._project(Path(temp_dir))
            config_path = project / "config" / "providers.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "defaultProviderId": "openai",
                        "providers": {
                            "openai": {
                                "type": "openai_compatible",
                                "baseUrl": "https://api.openai.com/v1",
                                "apiKeyEnv": "MPE_AI_API_KEY",
                                "defaultModel": "model-one",
                                "availableModels": ["model-one"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            (project / ".env").write_text('MPE_AI_API_KEY="existing-secret"\n', encoding="utf-8")
            manager = AdminConfigManager(project)
            draft = ProviderDraft.model_validate(
                {
                    "providerId": "openai",
                    "presetId": "openai",
                    "type": "openai_compatible",
                    "baseUrl": "https://api.openai.com/v1",
                    "model": "model-one",
                    "availableModels": ["model-one"],
                }
            )
            with patch.object(manager, "_call_provider", return_value={"elapsedMs": 1}):
                tested = manager.test_provider(draft)
            manager.apply_provider(
                ApplyProviderRequest.model_validate(
                    {
                        **draft.model_dump(by_alias=True),
                        "verificationToken": tested["verificationToken"],
                    }
                )
            )

            local = json.loads(
                (project / "config" / "providers.local.json").read_text(encoding="utf-8")
            )
            self.assertEqual(local["providers"]["openai"]["apiKeyEnv"], "MPE_AI_API_KEY")
            self.assertEqual(os.environ["MPE_AI_API_KEY"], "existing-secret")

    def test_snapshot_infers_known_provider_preset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir))
            config_path = project / "config" / "providers.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "defaultProviderId": "deepseek",
                        "providers": {
                            "deepseek": {
                                "type": "openai_compatible",
                                "baseUrl": "https://api.deepseek.com",
                                "defaultModel": "deepseek-v4-flash",
                                "availableModels": ["deepseek-v4-flash"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            snapshot = AdminConfigManager(project).snapshot()

            self.assertEqual(snapshot["providers"][0]["presetId"], "deepseek")
            self.assertEqual(snapshot["providerPresets"][0]["id"], "openai")

    def test_snapshot_accepts_codex_sdk_as_read_only_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir))
            config_path = project / "config" / "providers.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "defaultProviderId": "codex-local",
                        "providers": {
                            "codex-local": {
                                "type": "codex_sdk",
                                "defaultModel": "gpt-5.6-sol",
                                "availableModels": ["gpt-5.6-sol"],
                                "defaultReasoningEffort": "medium",
                                "capabilities": ["structured_json", "web_search"],
                                "webSearch": "live",
                                "timeoutSeconds": 0,
                                "transportRetries": 0,
                                "maxConcurrency": 1,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            snapshot = AdminConfigManager(project).snapshot()

            self.assertEqual(snapshot["activeProviderId"], "codex-local")
            provider = snapshot["providers"][0]
            self.assertFalse(provider["editable"])
            self.assertTrue(provider["active"])
            self.assertEqual(provider["type"], "codex_sdk")
            self.assertEqual(provider["timeoutSeconds"], 0)
            self.assertEqual(provider["webSearch"], "live")
            self.assertIn("web_search", provider["capabilities"])

    def test_apply_rejects_a_changed_or_reused_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = AdminConfigManager(self._project(Path(temp_dir)))
            draft = ProviderDraft.model_validate(
                {"providerId": "mock", "type": "mock", "model": "schema-sample-v1"}
            )
            tested = manager.test_provider(draft)
            changed = ApplyProviderRequest.model_validate(
                {
                    "providerId": "mock",
                    "type": "mock",
                    "model": "different-model",
                    "verificationToken": tested["verificationToken"],
                }
            )
            with self.assertRaises(ConfigurationError):
                manager.apply_provider(changed)

    def test_verification_digest_is_keyed_per_manager_process(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            draft = ProviderDraft.model_validate(
                {
                    "providerId": "private-model",
                    "type": "openai_compatible",
                    "baseUrl": "https://models.example/v1",
                    "model": "model-one",
                }
            )
            first = AdminConfigManager(self._project(Path(first_dir)))
            second = AdminConfigManager(self._project(Path(second_dir)))

            first_digest = first._draft_digest(draft, api_key="same-secret")
            second_digest = second._draft_digest(draft, api_key="same-secret")

            self.assertNotEqual(first_digest, second_digest)

    def test_openai_provider_rejects_unsafe_or_non_http_base_urls(self) -> None:
        rejected = (
            "models.example/v1",
            "file:///tmp/model",
            "https://user:secret@models.example/v1",
            "https://models.example/v1?token=secret",
            "https://models.example/v1#fragment",
        )
        for base_url in rejected:
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                ProviderDraft.model_validate(
                    {
                        "providerId": "private-model",
                        "type": "openai_compatible",
                        "baseUrl": base_url,
                        "model": "model-one",
                    }
                )

    def test_credential_names_do_not_collide_after_normalization(self) -> None:
        dashed = AdminConfigManager._credential_env("provider-a")
        underscored = AdminConfigManager._credential_env("provider_a")

        self.assertNotEqual(dashed, underscored)
        self.assertTrue(dashed.startswith("MPE_PROVIDER_PROVIDER_A_"))
        self.assertTrue(dashed.endswith("_API_KEY"))

    def test_apply_does_not_override_unrelated_shell_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"MPE_PORT": "9998"},
            clear=True,
        ):
            project = self._project(Path(temp_dir))
            (project / ".env").write_text('MPE_PORT="7777"\n', encoding="utf-8")
            manager = AdminConfigManager(project)
            draft = ProviderDraft.model_validate(
                {"providerId": "mock", "type": "mock", "model": "schema-sample-v1"}
            )
            tested = manager.test_provider(draft)

            manager.apply_provider(
                ApplyProviderRequest.model_validate(
                    {
                        **draft.model_dump(by_alias=True),
                        "verificationToken": tested["verificationToken"],
                    }
                )
            )

            self.assertEqual(os.environ["MPE_PORT"], "9998")


if __name__ == "__main__":
    unittest.main()
