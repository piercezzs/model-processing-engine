from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable

from .base import ProviderCallResult, ProviderConfig


MockResponder = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class MockProvider:
    def __init__(
        self,
        config: ProviderConfig,
        *,
        responder: MockResponder | None = None,
        delay_seconds: float = 0,
    ) -> None:
        self.config = config
        self._responder = responder
        self._delay_seconds = max(0, delay_seconds)
        self._lock = threading.Lock()
        self.call_count = 0

    def call_json(
        self,
        *,
        model: str,
        system_prompt: str,
        input_payload: dict[str, Any],
        output_schema: dict[str, Any],
        temperature: float,
        max_tokens: int | None,
    ) -> ProviderCallResult:
        del model, system_prompt, temperature, max_tokens
        started = time.perf_counter()
        with self._lock:
            self.call_count += 1
        if self._delay_seconds:
            time.sleep(self._delay_seconds)
        if self._responder:
            content = self._responder(copy.deepcopy(input_payload), copy.deepcopy(output_schema))
        else:
            content = sample_from_schema(output_schema)
        return ProviderCallResult(
            content=content,
            usage={
                "available": False,
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
                "source": "mock_provider",
            },
            attempts=1,
            elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
        )


def sample_from_schema(schema: dict[str, Any]) -> Any:
    if "const" in schema:
        return copy.deepcopy(schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return copy.deepcopy(enum[0])
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), "null")
    if schema_type == "object" or (schema_type is None and "properties" in schema):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = set(schema.get("required") or [])
        return {
            key: sample_from_schema(value)
            for key, value in properties.items()
            if key in required and isinstance(value, dict)
        }
    if schema_type == "array":
        minimum = int(schema.get("minItems") or 0)
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        return [sample_from_schema(item_schema) for _ in range(minimum)]
    if schema_type == "string":
        minimum = int(schema.get("minLength") or 0)
        return "sample" if minimum else ""
    if schema_type == "integer":
        return int(schema.get("minimum") or 0)
    if schema_type == "number":
        return float(schema.get("minimum") or 0)
    if schema_type == "boolean":
        return False
    if schema_type == "null":
        return None
    return {}
