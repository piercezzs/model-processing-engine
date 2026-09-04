from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from model_processing_engine.exceptions import ProviderError
from model_processing_engine.service import create_app
from model_processing_engine.settings import Settings

from tests.helpers import engine_with_mock


class AdminServiceTests(unittest.TestCase):
    def _runtime(self, root: Path, *, allow_remote: bool = False) -> tuple[Settings, object]:
        project = root / "project"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            '[project]\nname = "model-processing-engine"\n',
            encoding="utf-8",
        )
        config_dir = project / "config"
        config_dir.mkdir()
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
        data = root / "runtime"
        engine, _provider = engine_with_mock(data)
        settings = Settings(
            root=data,
            provider_config_path=config_dir / "providers.json",
            data_dir=data,
            host="0.0.0.0" if allow_remote else "127.0.0.1",
            port=8787,
            allow_remote=allow_remote,
            max_provider_concurrency=8,
            max_request_bytes=2 * 1024 * 1024,
            api_token="remote-secret" if allow_remote else "",
            project_dir=project,
        )
        return settings, engine

    def test_admin_page_has_security_headers_and_never_exposes_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings, engine = self._runtime(Path(temp_dir))
            app = create_app(engine=engine, settings=settings, restart_scheduler=lambda _settings: None)
            client = TestClient(app, client=("127.0.0.1", 50000))

            page = client.get("/admin", headers={"Host": "127.0.0.1:8787"})
            self.assertEqual(page.status_code, 200)
            self.assertIn("default-src 'self'", page.headers["content-security-policy"])
            config = client.get("/v1/admin/config", headers={"Host": "127.0.0.1:8787"})
            self.assertEqual(config.status_code, 200)
            self.assertNotIn("apiKey", config.text)
            self.assertIn("csrfToken", config.json())

    def test_admin_config_accepts_codex_sdk_as_read_only_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings, engine = self._runtime(Path(temp_dir))
            settings.provider_config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "defaultProviderId": "codex-local",
                        "providers": {
                            "codex-local": {
                                "type": "codex_sdk",
                                "defaultModel": "gpt-5.6-sol",
                                "availableModels": ["gpt-5.6-sol"],
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
            client = TestClient(
                create_app(engine=engine, settings=settings, restart_scheduler=lambda _settings: None),
                client=("127.0.0.1", 50000),
            )

            response = client.get("/v1/admin/config", headers={"Host": "127.0.0.1:8787"})

            self.assertEqual(response.status_code, 200)
            provider = response.json()["config"]["providers"][0]
            self.assertEqual(provider["id"], "codex-local")
            self.assertEqual(provider["type"], "codex_sdk")
            self.assertFalse(provider["editable"])

    def test_admin_mutation_requires_same_origin_and_csrf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings, engine = self._runtime(Path(temp_dir))
            client = TestClient(
                create_app(engine=engine, settings=settings, restart_scheduler=lambda _settings: None),
                client=("127.0.0.1", 50000),
            )
            payload = {"providerId": "mock", "type": "mock", "model": "schema-sample-v1"}
            headers = {"Host": "127.0.0.1:8787"}
            self.assertEqual(
                client.post("/v1/admin/providers/test", json=payload, headers=headers).status_code,
                403,
            )
            config = client.get("/v1/admin/config", headers=headers).json()
            headers.update(
                {
                    "Origin": "http://127.0.0.1:8787",
                    "X-MPE-CSRF": config["csrfToken"],
                }
            )
            self.assertEqual(
                client.post("/v1/admin/providers/test", json=payload, headers=headers).status_code,
                200,
            )

    def test_admin_delete_requires_csrf_protects_active_and_schedules_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            settings, engine = self._runtime(Path(temp_dir))
            settings.provider_config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "defaultProviderId": "mock",
                        "providers": {
                            "mock": {"type": "mock", "defaultModel": "schema-sample-v1"},
                            "unused": {
                                "type": "openai_compatible",
                                "baseUrl": "https://models.example/v1",
                                "apiKeyEnv": "MPE_PROVIDER_UNUSED_API_KEY",
                                "defaultModel": "model-one",
                                "availableModels": ["model-one"],
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            (settings.project_dir / ".env").write_text(
                'MPE_PROVIDER_UNUSED_API_KEY="file-secret"\n',
                encoding="utf-8",
            )
            scheduled: list[Settings] = []
            client = TestClient(
                create_app(
                    engine=engine,
                    settings=settings,
                    restart_scheduler=scheduled.append,
                ),
                client=("127.0.0.1", 50000),
            )
            headers = {"Host": "127.0.0.1:8787"}

            self.assertEqual(
                client.delete("/v1/admin/providers/unused", headers=headers).status_code,
                403,
            )
            csrf = client.get("/v1/admin/config", headers=headers).json()["csrfToken"]
            mutation_headers = {
                **headers,
                "Origin": "http://127.0.0.1:8787",
                "X-MPE-CSRF": csrf,
            }
            self.assertEqual(
                client.delete("/v1/admin/providers/mock", headers=mutation_headers).status_code,
                409,
            )
            self.assertEqual(
                client.delete("/v1/admin/providers/missing", headers=mutation_headers).status_code,
                404,
            )

            deleted = client.delete(
                "/v1/admin/providers/unused",
                headers=mutation_headers,
            )

            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(deleted.json()["status"], "deleted")
            self.assertEqual(scheduled, [settings])
            local = json.loads(
                (settings.project_dir / "config" / "providers.local.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("unused", local["providers"])
            self.assertNotIn(
                "MPE_PROVIDER_UNUSED_API_KEY",
                (settings.project_dir / ".env").read_text(encoding="utf-8"),
            )

    def test_verified_apply_schedules_restart_and_writes_project_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            settings, engine = self._runtime(Path(temp_dir))
            scheduled: list[Settings] = []
            client = TestClient(
                create_app(
                    engine=engine,
                    settings=settings,
                    restart_scheduler=scheduled.append,
                ),
                client=("127.0.0.1", 50000),
            )
            headers = {"Host": "127.0.0.1:8787"}
            csrf = client.get("/v1/admin/config", headers=headers).json()["csrfToken"]
            headers.update(
                {
                    "Origin": "http://127.0.0.1:8787",
                    "X-MPE-CSRF": csrf,
                }
            )
            draft = {"providerId": "mock", "type": "mock", "model": "schema-sample-v1"}
            tested = client.post(
                "/v1/admin/providers/test",
                json=draft,
                headers=headers,
            ).json()
            applied = client.post(
                "/v1/admin/providers/apply",
                json={**draft, "verificationToken": tested["verificationToken"]},
                headers=headers,
            )
            self.assertEqual(applied.status_code, 200)
            self.assertTrue((settings.project_dir / ".env").is_file())
            self.assertEqual(scheduled, [settings])

    def test_admin_can_discover_mock_models(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings, engine = self._runtime(Path(temp_dir))
            client = TestClient(
                create_app(engine=engine, settings=settings, restart_scheduler=lambda _settings: None),
                client=("127.0.0.1", 50000),
            )
            headers = {"Host": "127.0.0.1:8787"}
            csrf = client.get("/v1/admin/config", headers=headers).json()["csrfToken"]
            headers.update(
                {
                    "Origin": "http://127.0.0.1:8787",
                    "X-MPE-CSRF": csrf,
                }
            )

            response = client.post(
                "/v1/admin/providers/models",
                json={"providerId": "mock", "presetId": "mock", "type": "mock"},
                headers=headers,
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["models"], ["schema-sample-v1"])

    def test_admin_provider_test_is_written_to_redacted_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings, engine = self._runtime(Path(temp_dir))
            client = TestClient(
                create_app(engine=engine, settings=settings, restart_scheduler=lambda _settings: None),
                client=("127.0.0.1", 50000),
            )
            headers = {"Host": "127.0.0.1:8787"}
            csrf = client.get("/v1/admin/config", headers=headers).json()["csrfToken"]
            mutation_headers = {
                **headers,
                "Origin": "http://127.0.0.1:8787",
                "X-MPE-CSRF": csrf,
            }

            tested = client.post(
                "/v1/admin/providers/test",
                json={"providerId": "mock", "type": "mock", "model": "schema-sample-v1"},
                headers=mutation_headers,
            )
            history = client.get("/v1/admin/executions", headers=headers)
            page_only = client.get(
                "/v1/admin/executions",
                params={"includeSummary": "false"},
                headers=headers,
            )

            self.assertEqual(tested.status_code, 200)
            self.assertTrue(tested.json()["auditExecutionId"].startswith("probe_"))
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json()["summary"]["providerTests"], 1)
            self.assertEqual(history.json()["items"][0]["kind"], "provider_test")
            self.assertNotIn("result", history.text)
            self.assertEqual(page_only.status_code, 200)
            self.assertIsNone(page_only.json()["summary"])
            self.assertEqual(len(page_only.json()["items"]), 1)

    def test_failed_admin_provider_test_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings, engine = self._runtime(Path(temp_dir))
            client = TestClient(
                create_app(engine=engine, settings=settings, restart_scheduler=lambda _settings: None),
                client=("127.0.0.1", 50000),
            )
            headers = {"Host": "127.0.0.1:8787"}
            csrf = client.get("/v1/admin/config", headers=headers).json()["csrfToken"]
            mutation_headers = {
                **headers,
                "Origin": "http://127.0.0.1:8787",
                "X-MPE-CSRF": csrf,
            }
            with patch(
                "model_processing_engine.admin_config.AdminConfigManager.test_provider",
                side_effect=ProviderError("probe failed"),
            ):
                failed = client.post(
                    "/v1/admin/providers/test",
                    json={"providerId": "mock", "type": "mock", "model": "schema-sample-v1"},
                    headers=mutation_headers,
                )
            history = client.get("/v1/admin/executions", headers=headers).json()

            self.assertEqual(failed.status_code, 502)
            self.assertEqual(history["summary"]["failed"], 1)
            self.assertEqual(history["items"][0]["error"], "probe failed")

    def test_admin_execution_statistics_support_period_and_model_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings, engine = self._runtime(Path(temp_dir))
            client = TestClient(
                create_app(
                    engine=engine,
                    settings=settings,
                    restart_scheduler=lambda _settings: None,
                ),
                client=("127.0.0.1", 50000),
            )
            headers = {"Host": "127.0.0.1:8787"}
            csrf = client.get("/v1/admin/config", headers=headers).json()["csrfToken"]
            mutation_headers = {
                **headers,
                "Origin": "http://127.0.0.1:8787",
                "X-MPE-CSRF": csrf,
            }
            client.post(
                "/v1/admin/providers/test",
                json={
                    "providerId": "mock",
                    "type": "mock",
                    "model": "schema-sample-v1",
                },
                headers=mutation_headers,
            )
            year = datetime.now(timezone.utc).strftime("%Y")

            response = client.get(
                "/v1/admin/execution-stats",
                params={
                    "period": "year",
                    "anchor": year,
                    "timezone": "UTC",
                    "kind": "provider_test",
                    "providerId": "mock",
                    "model": "schema-sample-v1",
                },
                headers=headers,
            )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["summary"]["total"], 1)
            self.assertEqual(payload["summary"]["providerTests"], 1)
            self.assertEqual(payload["models"][0]["providerId"], "mock")
            self.assertEqual(len(payload["series"]), 12)
            self.assertEqual(
                client.get(
                    "/v1/admin/execution-stats",
                    params={
                        "period": "day",
                        "anchor": "not-a-date",
                        "timezone": "UTC",
                    },
                    headers=headers,
                ).status_code,
                400,
            )

    def test_admin_is_disabled_in_remote_mode_and_for_foreign_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings, engine = self._runtime(Path(temp_dir), allow_remote=True)
            remote = TestClient(
                create_app(engine=engine, settings=settings, restart_scheduler=lambda _settings: None)
            )
            self.assertEqual(
                remote.get(
                    "/admin",
                    headers={"Host": "127.0.0.1:8787", "Authorization": "Bearer remote-secret"},
                ).status_code,
                404,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings, engine = self._runtime(Path(temp_dir))
            local = TestClient(
                create_app(engine=engine, settings=settings, restart_scheduler=lambda _settings: None)
            )
            self.assertEqual(local.get("/admin", headers={"Host": "example.com"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
