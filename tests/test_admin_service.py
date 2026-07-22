from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

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
