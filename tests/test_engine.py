from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tests.helpers import engine_with_mock, execution_request, task_definition


def summary_responder(payload, _schema):
    return {"summary": payload["input"].get("text", "")}


class EngineTests(unittest.TestCase):
    def test_exact_cache_avoids_second_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir), responder=summary_responder)
            request = execution_request(task_definition(), {"text": "hello"})
            first = engine.execute(request)
            second = engine.execute(request)
            self.assertEqual(first.status, "succeeded")
            self.assertFalse(first.cache["hit"])
            self.assertTrue(second.cache["hit"])
            self.assertEqual(provider.call_count, 1)

    def test_force_refresh_bypasses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir), responder=summary_responder)
            task = task_definition()
            engine.execute(execution_request(task))
            refreshed = engine.execute(execution_request(task, runtime={"forceRefresh": True}))
            self.assertFalse(refreshed.cache["hit"])
            self.assertEqual(provider.call_count, 2)

    def test_prompt_and_model_are_mandatory_cache_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir), responder=summary_responder)
            engine.execute(execution_request(task_definition(prompt="Prompt one")))
            engine.execute(execution_request(task_definition(prompt="Prompt two")))
            engine.execute(
                execution_request(
                    task_definition(prompt="Prompt two"),
                    runtime={"model": "mock-v2"},
                )
            )
            self.assertEqual(provider.call_count, 3)

    def test_namespace_is_mandatory_cache_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir), responder=summary_responder)
            engine.execute(execution_request(task_definition(namespace="project-one")))
            engine.execute(execution_request(task_definition(namespace="project-two")))
            self.assertEqual(provider.call_count, 2)

    def test_semantic_cache_uses_project_identity_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir), responder=summary_responder)
            task = task_definition(
                cache_policy={
                    "mode": "semantic",
                    "semanticVersion": "entity-v1",
                    "identityFields": ["entity.id", "content"],
                },
                input_schema={
                    "type": "object",
                    "required": ["entity", "content", "requestedAt"],
                    "properties": {
                        "entity": {
                            "type": "object",
                            "required": ["id"],
                            "properties": {"id": {"type": "string"}},
                        },
                        "content": {"type": "string"},
                        "requestedAt": {"type": "string"},
                    },
                },
            )
            first_input = {"entity": {"id": "a"}, "content": "same", "requestedAt": "one"}
            second_input = {"entity": {"id": "a"}, "content": "same", "requestedAt": "two"}
            engine.execute(execution_request(task, first_input))
            second = engine.execute(execution_request(task, second_input))
            self.assertTrue(second.cache["hit"])
            self.assertEqual(provider.call_count, 1)

    def test_semantic_cache_rejects_missing_identity_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir), responder=summary_responder)
            task = task_definition(
                cache_policy={
                    "mode": "semantic",
                    "semanticVersion": "entity-v1",
                    "identityFields": ["entity.id"],
                }
            )
            result = engine.execute(execution_request(task, {"text": "hello"}))
            self.assertEqual(result.status, "failed")
            self.assertIn("identity field is missing", result.error)
            self.assertEqual(provider.call_count, 0)

    def test_input_and_output_contract_failures_are_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(
                Path(temp_dir),
                responder=lambda _payload, _schema: {"wrong": True},
            )
            task = task_definition()
            invalid_input = engine.execute(execution_request(task, {"other": "value"}))
            invalid_output = engine.execute(execution_request(task, {"text": "hello"}))
            self.assertEqual(invalid_input.status, "failed")
            self.assertEqual(invalid_output.status, "failed")
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(engine.store.cache_count(), 0)

    def test_sensitive_result_is_returned_but_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, _provider = engine_with_mock(Path(temp_dir), responder=summary_responder)
            task = task_definition(cache_policy={"mode": "disabled", "sensitive": True})
            result = engine.execute(execution_request(task, {"text": "private"}))
            persisted = engine.get_execution(result.execution_id)
            self.assertEqual(result.result, {"summary": "private"})
            self.assertIsNone(persisted.result)
            self.assertIn("Sensitive result omitted", persisted.warnings[0])
            self.assertEqual(engine.store.cache_count(), 0)

    def test_single_flight_deduplicates_concurrent_identical_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(
                Path(temp_dir),
                responder=summary_responder,
                delay_seconds=0.05,
            )
            request = execution_request(task_definition(), {"text": "same"})
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(lambda _: engine.execute(request), range(8)))
            self.assertTrue(all(result.status == "succeeded" for result in results))
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(sum(bool(result.cache["hit"]) for result in results), 7)

    def test_batch_execution_merges_items_in_input_order(self) -> None:
        def responder(payload, _schema):
            values = payload["input"]["items"]
            return {"items": [{"id": item["id"], "label": item["value"].upper()} for item in values]}

        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir), responder=responder)
            task = task_definition(
                batch_policy={
                    "enabled": True,
                    "inputField": "items",
                    "outputField": "items",
                    "chunkSize": 2,
                    "concurrency": 2,
                },
                input_schema={
                    "type": "object",
                    "required": ["items"],
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["id", "value"],
                                "properties": {
                                    "id": {"type": "integer"},
                                    "value": {"type": "string"},
                                },
                            },
                        }
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["items"],
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["id", "label"],
                                "properties": {
                                    "id": {"type": "integer"},
                                    "label": {"type": "string"},
                                },
                            },
                        }
                    },
                },
            )
            values = [{"id": index, "value": str(index)} for index in range(5)]
            result = engine.execute(execution_request(task, {"items": values}))
            self.assertEqual(result.status, "succeeded")
            self.assertEqual([item["id"] for item in result.result["items"]], list(range(5)))
            self.assertEqual(provider.call_count, 3)

    def test_empty_batch_returns_empty_output_without_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir))
            task = task_definition(
                batch_policy={
                    "enabled": True,
                    "inputField": "items",
                    "outputField": "items",
                    "chunkSize": 20,
                    "concurrency": 2,
                },
                input_schema={
                    "type": "object",
                    "required": ["items"],
                    "properties": {"items": {"type": "array"}},
                },
                output_schema={
                    "type": "object",
                    "required": ["items"],
                    "properties": {"items": {"type": "array"}},
                },
            )
            result = engine.execute(execution_request(task, {"items": []}))
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.result, {"items": []})
            self.assertEqual(provider.call_count, 0)


if __name__ == "__main__":
    unittest.main()
