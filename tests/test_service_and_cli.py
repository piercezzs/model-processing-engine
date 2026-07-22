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


if __name__ == "__main__":
    unittest.main()
