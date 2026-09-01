from __future__ import annotations

import tempfile
import threading
import time
import unittest
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import PropertyMock, patch

from model_processing_engine.cache import SQLiteRuntimeStore
from model_processing_engine.contracts import TaskDefinition
from model_processing_engine.engine import ENGINE_CACHE_SCHEMA, MAX_BATCH_CHUNKS
from model_processing_engine.exceptions import ProviderNonJsonContentError
from model_processing_engine.providers.base import ProviderCallResult

from tests.helpers import engine_with_mock, execution_request, task_definition


def summary_responder(payload, _schema):
    return {"summary": payload["input"].get("text", "")}


class EngineTests(unittest.TestCase):
    @staticmethod
    def _stream_task(*, minimum_length: int = 1, sensitive: bool = False):
        return task_definition(
            cache_policy={"mode": "disabled", "sensitive": sensitive},
            stream_policy={"mode": "text_field", "resultField": "reply"},
            output_schema={
                "type": "object",
                "required": ["reply"],
                "properties": {
                    "reply": {"type": "string", "minLength": minimum_length}
                },
                "additionalProperties": False,
            },
        )

    def test_text_stream_emits_deltas_and_canonical_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(
                Path(temp_dir),
                text_responder=lambda payload, _feedback: payload["input"]["text"],
                text_chunk_size=2,
            )
            request = execution_request(
                self._stream_task(sensitive=True),
                {"text": "hello"},
            )
            events = list(engine.execute_stream(request))

            self.assertEqual(
                [item.event for item in events],
                [
                    "execution.started",
                    "attempt.started",
                    "content.delta",
                    "content.delta",
                    "content.delta",
                    "validation.started",
                    "execution.completed",
                ],
            )
            self.assertEqual(
                "".join(item.delta or "" for item in events),
                "hello",
            )
            completed = events[-1].envelope
            self.assertIsNotNone(completed)
            assert completed is not None
            self.assertEqual(completed.result, {"reply": "hello"})
            persisted = engine.get_execution(completed.execution_id)
            self.assertEqual(persisted.status, "succeeded")
            self.assertIsNone(persisted.result)
            self.assertEqual(provider.call_count, 1)

    def test_text_stream_resets_before_contract_repair(self) -> None:
        def responder(_payload, feedback):
            return "x" if feedback is None else "fixed"

        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(
                Path(temp_dir),
                text_responder=responder,
                text_chunk_size=20,
            )
            events = list(
                engine.execute_stream(
                    execution_request(self._stream_task(minimum_length=2))
                )
            )

            names = [item.event for item in events]
            self.assertEqual(names.count("content.reset"), 1)
            self.assertLess(names.index("content.reset"), names.index("execution.completed"))
            self.assertEqual(events[-1].envelope.result, {"reply": "fixed"})
            self.assertEqual(events[-1].envelope.timing["contractRepairs"], 1)
            self.assertEqual(provider.call_count, 2)

    def test_text_stream_close_marks_execution_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, _provider = engine_with_mock(
                Path(temp_dir),
                text_responder=lambda _payload, _feedback: "long answer",
                text_chunk_size=2,
            )
            stream = engine.execute_stream(execution_request(self._stream_task()))
            started = next(stream)
            next(stream)
            next(stream)
            stream.close()

            persisted = engine.get_execution(started.execution_id)
            self.assertEqual(persisted.status, "cancelled")
            self.assertEqual(persisted.progress["event"], "cancelled")
            self.assertEqual(
                engine.store.execution_history()["summary"]["cancelled"],
                1,
            )

    def test_text_stream_task_is_rejected_by_non_streaming_execute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir))
            result = engine.execute(execution_request(self._stream_task()))

            self.assertEqual(result.status, "failed")
            self.assertIn("/v1/executions/stream", result.error)
            self.assertEqual(provider.call_count, 0)

    def test_engine_removes_result_cache_from_older_wire_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SQLiteRuntimeStore(root / "runtime.sqlite")
            common = {
                "namespace": "test-project",
                "task_id": "summary",
                "task_version": "1",
                "provider_id": "mock",
                "model": "mock-v1",
                "result": {"summary": "cached"},
                "ttl_seconds": None,
            }
            store.put_cache(
                **common,
                key="legacy",
                metadata={"cacheSchema": "mpe-cache-v1"},
            )
            store.put_cache(
                **common,
                key="current",
                metadata={"cacheSchema": ENGINE_CACHE_SCHEMA},
            )

            engine, _provider = engine_with_mock(root)

            self.assertEqual(engine.store.cache_count(), 1)
            self.assertIsNone(engine.store.get_cache("test-project", "legacy"))
            self.assertIsNotNone(engine.store.get_cache("test-project", "current"))

    def test_provider_payload_places_stable_contract_before_variable_input(self) -> None:
        captured_keys: list[list[str]] = []

        def responder(payload, _schema):
            captured_keys.append(list(payload))
            return {"summary": payload["input"]["text"]}

        with tempfile.TemporaryDirectory() as temp_dir:
            engine, _provider = engine_with_mock(
                Path(temp_dir),
                responder=responder,
            )
            task = task_definition().model_copy(
                update={"taxonomy": {"types": {"book": {"sections": []}}}}
            )
            result = engine.execute(execution_request(task, {"text": "hello"}))

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(captured_keys, [["outputSchema", "taxonomy", "input"]])

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

    def test_task_fingerprints_are_computed_once_per_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir), responder=summary_responder)
            request = execution_request(task_definition(), {"text": "hello"})
            with (
                patch.object(
                    TaskDefinition,
                    "digest",
                    new_callable=PropertyMock,
                    return_value="sha256:stable-task",
                ) as task_digest,
                patch.object(
                    TaskDefinition,
                    "component_hashes",
                    new_callable=PropertyMock,
                    return_value={"prompt": "sha256:stable-component"},
                ) as component_hashes,
            ):
                first = engine.execute(request)
                second = engine.execute(request)

            self.assertEqual(first.status, "succeeded")
            self.assertTrue(second.cache["hit"])
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(task_digest.call_count, 2)
            self.assertEqual(component_hashes.call_count, 2)

    def test_force_refresh_bypasses_cache(self) -> None:
        responses = iter(("original", "refreshed"))

        def responder(_payload, _schema):
            return {"summary": next(responses)}

        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir), responder=responder)
            task = task_definition()
            engine.execute(execution_request(task))
            refreshed = engine.execute(execution_request(task, runtime={"forceRefresh": True}))
            cached = engine.execute(execution_request(task))
            self.assertFalse(refreshed.cache["hit"])
            self.assertEqual(refreshed.result, {"summary": "refreshed"})
            self.assertTrue(cached.cache["hit"])
            self.assertEqual(cached.result, {"summary": "refreshed"})
            self.assertEqual(provider.call_count, 2)

    def test_failed_force_refresh_preserves_previous_cache(self) -> None:
        responses = iter(
            ({"summary": "original"}, {"invalid": True}, {"invalid": True})
        )

        def responder(_payload, _schema):
            return next(responses)

        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir), responder=responder)
            task = task_definition()
            engine.execute(execution_request(task))
            refreshed = engine.execute(execution_request(task, runtime={"forceRefresh": True}))
            cached = engine.execute(execution_request(task))
            self.assertEqual(refreshed.status, "failed")
            self.assertTrue(cached.cache["hit"])
            self.assertEqual(cached.result, {"summary": "original"})
            self.assertEqual(provider.call_count, 3)

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

    def test_reasoning_effort_is_cache_identity_and_execution_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            engine, provider = engine_with_mock(
                root,
                responder=summary_responder,
                provider_type="openai_compatible",
                default_model="gpt-5.6-sol",
            )
            task = task_definition()
            low = execution_request(
                task,
                runtime={"model": "gpt-5.6-sol", "reasoningEffort": "low"},
            )
            high = execution_request(
                task,
                runtime={"model": "gpt-5.6-sol", "reasoningEffort": "high"},
            )

            first = engine.execute(low)
            second = engine.execute(high)
            cached = engine.execute(low)

            self.assertEqual(provider.call_count, 2)
            self.assertFalse(first.cache["hit"])
            self.assertFalse(second.cache["hit"])
            self.assertTrue(cached.cache["hit"])
            self.assertEqual(first.provider["reasoning"]["effective"], "low")
            self.assertEqual(first.provider["reasoning"]["source"], "runtime")
            history = engine.store.execution_history()
            low_history = next(
                item
                for item in history["items"]
                if item["executionId"] == first.execution_id
            )
            self.assertEqual(low_history["provider"]["reasoning"]["effective"], "low")
            with sqlite3.connect(root / "runtime.sqlite") as connection:
                efforts = [
                    row[0]
                    for row in connection.execute(
                        "SELECT reasoning_effort FROM provider_call_records ORDER BY created_at"
                    )
                ]
            self.assertEqual(efforts, ["low", "high"])

    def test_unsupported_reasoning_effort_fails_before_provider_io(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(
                Path(temp_dir),
                provider_type="openai_compatible",
                default_model="gpt-5.5",
            )
            result = engine.execute(
                execution_request(
                    task_definition(),
                    runtime={"model": "gpt-5.5", "reasoningEffort": "max"},
                )
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("does not support reasoning effort", result.error)
            self.assertEqual(provider.call_count, 0)

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
            self.assertEqual(provider.call_count, 2)
            self.assertEqual(engine.store.cache_count(), 0)

    def test_provider_usage_is_audited_before_output_schema_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir))
            provider.call_json = lambda **_kwargs: ProviderCallResult(
                content={"wrong": True},
                usage={
                    "available": True,
                    "inputTokens": 8,
                    "outputTokens": 5,
                    "totalTokens": 13,
                    "cacheReadInputTokens": 0,
                },
                attempts=2,
                elapsed_ms=12,
            )

            result = engine.execute(execution_request(task_definition()))
            history = engine.store.execution_history()

            self.assertEqual(result.status, "failed")
            self.assertEqual(history["summary"]["providerCallCount"], 2)
            self.assertEqual(history["summary"]["transportRetries"], 2)
            self.assertEqual(history["summary"]["contractRepairs"], 1)
            self.assertEqual(history["summary"]["usage"]["totalTokens"], 26)
            self.assertEqual(history["items"][0]["error"], "ContractValidationError")
            self.assertNotIn("result", history["items"][0])

    def test_output_contract_failure_is_repaired_once(self) -> None:
        responses = iter(({"wrong": True}, {"summary": "repaired"}))

        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(
                Path(temp_dir),
                responder=lambda _payload, _schema: next(responses),
            )

            result = engine.execute(execution_request(task_definition()))
            history = engine.store.execution_history()

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.result, {"summary": "repaired"})
            self.assertEqual(result.timing["providerCallCount"], 2)
            self.assertEqual(result.timing["contractRepairs"], 1)
            self.assertEqual(provider.call_count, 2)
            self.assertEqual(history["summary"]["contractRepairs"], 1)

    def test_non_json_output_is_repaired_once_with_bounded_previous_content(self) -> None:
        attempts = []

        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir))

            def call_json(**kwargs):
                attempts.append(kwargs)
                if len(attempts) == 1:
                    raise ProviderNonJsonContentError(
                        "Provider returned non-JSON content",
                        content="plain provider response",
                        usage={
                            "available": True,
                            "inputTokens": 7,
                            "outputTokens": 3,
                            "totalTokens": 10,
                        },
                        attempts=1,
                        elapsed_ms=4,
                        audit_message="Provider returned non-JSON content",
                    )
                return ProviderCallResult(
                    content={"summary": "repaired"},
                    usage={
                        "available": True,
                        "inputTokens": 9,
                        "outputTokens": 4,
                        "totalTokens": 13,
                    },
                    attempts=1,
                    elapsed_ms=5,
                )

            provider.call_json = call_json
            result = engine.execute(execution_request(task_definition()))
            history = engine.store.execution_history()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.result, {"summary": "repaired"})
        self.assertEqual(result.timing["providerCallCount"], 2)
        self.assertEqual(result.timing["contractRepairs"], 1)
        self.assertEqual(result.usage["totalTokens"], 23)
        self.assertEqual(attempts[1]["previous_output"], "plain provider response")
        self.assertIn("not a valid JSON object", attempts[1]["repair_feedback"])
        self.assertEqual(history["summary"]["providerCallCount"], 2)
        self.assertEqual(history["summary"]["contractRepairs"], 1)

    def test_runtime_can_disable_non_json_contract_repair(self) -> None:
        attempts = []

        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir))

            def call_json(**kwargs):
                attempts.append(kwargs)
                raise ProviderNonJsonContentError(
                    "Provider returned non-JSON content",
                    content="plain provider response",
                    attempts=1,
                    audit_message="Provider returned non-JSON content",
                )

            provider.call_json = call_json
            result = engine.execute(
                execution_request(
                    task_definition(),
                    runtime={"contractRetries": 0},
                )
            )
            history = engine.store.execution_history()

        self.assertEqual(result.status, "failed")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(history["summary"]["providerCallCount"], 1)
        self.assertEqual(history["summary"]["contractRepairs"], 0)

    def test_runtime_can_disable_contract_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(
                Path(temp_dir),
                responder=lambda _payload, _schema: {"wrong": True},
            )

            result = engine.execute(
                execution_request(
                    task_definition(),
                    runtime={"contractRetries": 0},
                )
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(provider.call_count, 1)

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
            self.assertEqual(
                sorted(
                    int(result.cache["hitCount"])
                    for result in results
                    if result.cache["hit"]
                ),
                list(range(1, 8)),
            )

    def test_provider_concurrency_is_bounded_by_provider_configuration(self) -> None:
        active = 0
        peak = 0
        lock = threading.Lock()

        def responder(payload, _schema):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return {"summary": payload["input"]["text"]}

        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(
                Path(temp_dir),
                responder=responder,
                provider_max_concurrency=2,
            )
            task = task_definition(cache_policy={"mode": "disabled"})
            requests = [
                execution_request(task, {"text": str(index)})
                for index in range(6)
            ]
            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(executor.map(engine.execute, requests))

            self.assertTrue(all(result.status == "succeeded" for result in results))
            self.assertEqual(provider.call_count, 6)
            self.assertEqual(peak, 2)
            self.assertEqual(engine.provider_descriptors()[0]["maxConcurrency"], 2)

    def test_service_concurrency_ceiling_reduces_effective_provider_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, _provider = engine_with_mock(
                Path(temp_dir),
                provider_max_concurrency=6,
                service_max_concurrency=3,
            )
            descriptor = engine.provider_descriptors()[0]
            self.assertEqual(descriptor["configuredMaxConcurrency"], 6)
            self.assertEqual(descriptor["maxConcurrency"], 3)

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

    def test_batch_submission_window_and_input_key_order_are_preserved(self) -> None:
        submitted = 0
        submit_lock = threading.Lock()
        first_window_submitted = threading.Event()
        release_provider = threading.Event()
        input_key_orders: list[list[str]] = []

        class CountingExecutor(ThreadPoolExecutor):
            def submit(self, *args, **kwargs):
                nonlocal submitted
                with submit_lock:
                    submitted += 1
                    if submitted == 2:
                        first_window_submitted.set()
                return super().submit(*args, **kwargs)

        def responder(payload, _schema):
            input_key_orders.append(list(payload["input"]))
            release_provider.wait(timeout=2)
            return {"items": list(payload["input"]["items"])}

        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir), responder=responder)
            task = task_definition(
                cache_policy={"mode": "disabled"},
                batch_policy={
                    "enabled": True,
                    "inputField": "items",
                    "outputField": "items",
                    "chunkSize": 1,
                    "concurrency": 2,
                },
                input_schema={
                    "type": "object",
                    "required": ["before", "items", "after"],
                    "properties": {
                        "before": {"type": "string"},
                        "items": {"type": "array", "items": {"type": "integer"}},
                        "after": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "required": ["items"],
                    "properties": {
                        "items": {"type": "array", "items": {"type": "integer"}}
                    },
                    "additionalProperties": False,
                },
            )
            request = execution_request(
                task,
                {"before": "a", "items": list(range(6)), "after": "z"},
            )
            with (
                patch("model_processing_engine.engine.ThreadPoolExecutor", CountingExecutor),
                ThreadPoolExecutor(max_workers=1) as caller,
            ):
                result_future = caller.submit(engine.execute, request)
                self.assertTrue(first_window_submitted.wait(timeout=1))
                time.sleep(0.05)
                self.assertEqual(submitted, 2)
                release_provider.set()
                result = result_future.result(timeout=3)

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.result, {"items": list(range(6))})
            self.assertEqual(provider.call_count, 6)
            self.assertTrue(input_key_orders)
            self.assertTrue(
                all(order == ["before", "items", "after"] for order in input_key_orders)
            )

    def test_batch_chunk_limit_rejects_before_provider_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir))
            task = task_definition(
                cache_policy={"mode": "disabled"},
                batch_policy={
                    "enabled": True,
                    "inputField": "items",
                    "outputField": "items",
                    "chunkSize": 1,
                    "concurrency": 8,
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
            result = engine.execute(
                execution_request(task, {"items": [None] * (MAX_BATCH_CHUNKS + 1)})
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("exceeding the limit", result.error)
            self.assertEqual(provider.call_count, 0)

    def test_batch_chunk_limit_accepts_exact_boundary(self) -> None:
        def fast_provider_call(**kwargs):
            call = ProviderCallResult(
                content={"items": list(kwargs["input_payload"]["items"])},
                usage={
                    "available": False,
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "totalTokens": 0,
                },
                attempts=1,
                elapsed_ms=0,
            )
            return call, [call]

        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir))
            task = task_definition(
                cache_policy={"mode": "disabled"},
                batch_policy={
                    "enabled": True,
                    "inputField": "items",
                    "outputField": "items",
                    "chunkSize": 1,
                    "concurrency": 8,
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
            with patch.object(
                engine,
                "_provider_call_with_contract_repair",
                side_effect=fast_provider_call,
            ):
                result = engine.execute(
                    execution_request(task, {"items": [None] * MAX_BATCH_CHUNKS})
                )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(len(result.result["items"]), MAX_BATCH_CHUNKS)
            self.assertEqual(provider.call_count, 0)

    def test_fast_batch_progress_callbacks_do_not_amplify_execution_writes(self) -> None:
        callback_events: list[str] = []

        def responder(payload, _schema):
            return {"items": list(payload["input"]["items"])}

        with tempfile.TemporaryDirectory() as temp_dir:
            engine, provider = engine_with_mock(Path(temp_dir), responder=responder)
            task = task_definition(
                cache_policy={"mode": "disabled"},
                batch_policy={
                    "enabled": True,
                    "inputField": "items",
                    "outputField": "items",
                    "chunkSize": 1,
                    "concurrency": 8,
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
            with (
                patch.object(
                    engine.store,
                    "save_execution",
                    wraps=engine.store.save_execution,
                ) as save_execution,
                patch(
                    "model_processing_engine.engine.PROGRESS_PERSIST_INTERVAL_SECONDS",
                    3600.0,
                ),
            ):
                result = engine.execute(
                    execution_request(task, {"items": list(range(20))}),
                    progress_callback=lambda payload: callback_events.append(payload["event"]),
                )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(provider.call_count, 20)
            self.assertEqual(callback_events.count("provider_chunk_completed"), 20)
            self.assertEqual(save_execution.call_count, 2)

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
