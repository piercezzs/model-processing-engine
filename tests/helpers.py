from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from model_processing_engine.cache import SQLiteRuntimeStore
from model_processing_engine.contracts import ExecutionRequest, TaskDefinition
from model_processing_engine.engine import ModelProcessingEngine
from model_processing_engine.providers.base import ProviderConfig, ProviderRegistry
from model_processing_engine.providers.mock import MockProvider


def task_definition(
    *,
    namespace: str = "test-project",
    task_id: str = "summary",
    version: str = "1",
    prompt: str = "Return JSON.",
    cache_policy: dict[str, Any] | None = None,
    batch_policy: dict[str, Any] | None = None,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> TaskDefinition:
    return TaskDefinition.model_validate(
        {
            "namespace": namespace,
            "id": task_id,
            "version": version,
            "title": "Test Task",
            "prompt": prompt,
            "inputSchema": input_schema
            or {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
                "additionalProperties": True,
            },
            "outputSchema": output_schema
            or {
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            "cachePolicy": cache_policy or {"mode": "exact"},
            "batchPolicy": batch_policy or {"enabled": False},
            "runtimeDefaults": {
                "providerId": "mock",
                "model": "mock-v1",
            },
        }
    )


def execution_request(
    task: TaskDefinition,
    input_payload: dict[str, Any] | None = None,
    *,
    runtime: dict[str, Any] | None = None,
    async_mode: bool = False,
) -> ExecutionRequest:
    return ExecutionRequest.model_validate(
        {
            "task": task.model_dump(by_alias=True),
            "input": input_payload or {"text": "hello"},
            "runtime": runtime or {},
            "asyncMode": async_mode,
        }
    )


def engine_with_mock(
    root: Path,
    *,
    responder: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    delay_seconds: float = 0,
    provider_max_concurrency: int = 8,
    service_max_concurrency: int = 64,
) -> tuple[ModelProcessingEngine, MockProvider]:
    config = ProviderConfig(
        id="mock",
        type="mock",
        default_model="mock-v1",
        max_concurrency=provider_max_concurrency,
    )
    provider = MockProvider(config, responder=responder, delay_seconds=delay_seconds)
    registry = ProviderRegistry({"mock": provider}, default_provider_id="mock")
    engine = ModelProcessingEngine(
        providers=registry,
        store=SQLiteRuntimeStore(root / "runtime.sqlite"),
        max_provider_concurrency=service_max_concurrency,
    )
    return engine, provider
