from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from model_processing_engine.cli import main
from model_processing_engine.service import create_app
from model_processing_engine.settings import Settings

from tests.helpers import engine_with_mock, execution_request, task_definition


class ServiceAndCliTests(unittest.TestCase):
    def test_sync_and_async_execution_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            engine, _provider = engine_with_mock(root)
            settings = Settings(
                root=root,
                provider_config_path=root / "providers.json",
                data_dir=root,
                host="127.0.0.1",
                port=8787,
                allow_remote=False,
                max_provider_concurrency=8,
                max_request_bytes=2 * 1024 * 1024,
            )
            client = TestClient(create_app(engine=engine, settings=settings))
            task = task_definition()
            sync_payload = execution_request(task).model_dump(by_alias=True)
            response = client.post("/v1/executions", json=sync_payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "succeeded")
            self.assertEqual(client.get("/v1/executions").status_code, 405)
            async_payload = execution_request(
                task_definition(task_id="async-task"),
                async_mode=True,
            ).model_dump(by_alias=True)
            queued = client.post("/v1/executions", json=async_payload).json()
            self.assertEqual(queued["status"], "queued")
            for _ in range(50):
                record = client.get(f"/v1/executions/{queued['executionId']}").json()
                if record["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)
            self.assertEqual(record["status"], "succeeded")

    def test_unknown_execution_returns_404(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            engine, _provider = engine_with_mock(root)
            settings = Settings(
                root=root,
                provider_config_path=root / "providers.json",
                data_dir=root,
                host="127.0.0.1",
                port=8787,
                allow_remote=False,
                max_provider_concurrency=8,
                max_request_bytes=2 * 1024 * 1024,
            )
            client = TestClient(create_app(engine=engine, settings=settings))
            self.assertEqual(client.get("/v1/executions/missing").status_code, 404)

    def test_api_token_protects_non_health_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            engine, _provider = engine_with_mock(root)
            settings = Settings(
                root=root,
                provider_config_path=root / "providers.json",
                data_dir=root,
                host="127.0.0.1",
                port=8787,
                allow_remote=False,
                max_provider_concurrency=8,
                max_request_bytes=2 * 1024 * 1024,
                api_token="local-secret",
            )
            client = TestClient(create_app(engine=engine, settings=settings))
            self.assertEqual(client.get("/v1/health").status_code, 200)
            self.assertEqual(client.get("/v1/providers").status_code, 401)
            response = client.get(
                "/v1/providers",
                headers={"Authorization": "Bearer local-secret"},
            )
            self.assertEqual(response.status_code, 200)

    def test_remote_mode_protects_health_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            engine, _provider = engine_with_mock(root)
            settings = Settings(
                root=root,
                provider_config_path=root / "providers.json",
                data_dir=root,
                host="0.0.0.0",
                port=8787,
                allow_remote=True,
                max_provider_concurrency=8,
                max_request_bytes=2 * 1024 * 1024,
                api_token="remote-secret",
            )
            client = TestClient(create_app(engine=engine, settings=settings))
            self.assertEqual(client.get("/v1/health").status_code, 401)
            response = client.get(
                "/v1/health",
                headers={"Authorization": "Bearer remote-secret"},
            )
            self.assertEqual(response.status_code, 200)

    def test_cli_validates_example_task(self) -> None:
        task_dir = Path(__file__).parents[1] / "examples" / "tasks" / "generic_summary"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["task", "validate", "--task-dir", str(task_dir)])
        self.assertEqual(code, 0)
        self.assertIn('"status": "valid"', stdout.getvalue())

    def test_cli_serve_uses_requested_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "providers.json").write_text(
                '{"defaultProviderId":"mock","providers":'
                '{"mock":{"type":"mock","defaultModel":"mock-v1"}}}',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True), patch("uvicorn.run") as run:
                code = main(["serve", "--root", str(root)])
            self.assertEqual(code, 0)
            app = run.call_args.args[0]
            self.assertEqual(app.state.settings.root, root.resolve())

    def test_health_identifies_runtime_and_api_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            engine, _provider = engine_with_mock(root)
            settings = Settings(
                root=root,
                provider_config_path=root / "providers.json",
                data_dir=root,
                host="127.0.0.1",
                port=8787,
                allow_remote=False,
                max_provider_concurrency=8,
                max_request_bytes=2 * 1024 * 1024,
            )
            client = TestClient(create_app(engine=engine, settings=settings))
            health = client.get("/v1/health").json()
            self.assertEqual(health["app"], "model-processing-engine")
            self.assertEqual(health["apiVersion"], "v1")
            self.assertEqual(health["instanceId"], settings.instance_id)
            self.assertGreater(health["processId"], 0)

    def test_provider_endpoint_includes_configured_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            engine, _provider = engine_with_mock(root)
            settings = Settings(
                root=root,
                provider_config_path=root / "providers.json",
                data_dir=root,
                host="127.0.0.1",
                port=8787,
                allow_remote=False,
                max_provider_concurrency=8,
                max_request_bytes=2 * 1024 * 1024,
            )
            client = TestClient(create_app(engine=engine, settings=settings))
            payload = client.get("/v1/providers").json()
            self.assertEqual(payload["providers"], ["mock"])
            self.assertEqual(payload["providerDetails"][0]["id"], "mock")
            self.assertEqual(payload["providerDetails"][0]["maxConcurrency"], 8)

    def test_cli_status_prints_manager_result(self) -> None:
        stdout = io.StringIO()
        with patch(
            "model_processing_engine.cli.service_status",
            return_value={"status": "stopped"},
        ), redirect_stdout(stdout):
            code = main(["status", "--root", "/tmp/mpe-test-root"])
        self.assertEqual(code, 0)
        self.assertIn('"status": "stopped"', stdout.getvalue())

    def test_cli_admin_can_print_without_opening_a_browser(self) -> None:
        stdout = io.StringIO()
        with patch(
            "model_processing_engine.cli.service_status",
            return_value={"status": "running", "url": "http://127.0.0.1:8787"},
        ), patch("webbrowser.open") as open_browser, redirect_stdout(stdout):
            code = main(["admin", "--print-only", "--root", "/tmp/mpe-test-root"])
        self.assertEqual(code, 0)
        open_browser.assert_not_called()
        self.assertIn('"adminUrl": "http://127.0.0.1:8787/admin"', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
