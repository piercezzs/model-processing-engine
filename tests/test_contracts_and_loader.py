from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from model_processing_engine.contracts import CachePolicy, ExecutionRequest
from model_processing_engine.exceptions import ContractValidationError
from model_processing_engine.task_loader import load_task_pack, validate_json_schema

from tests.helpers import task_definition


class ContractAndLoaderTests(unittest.TestCase):
    def test_sensitive_task_must_disable_cache(self) -> None:
        with self.assertRaises(ValidationError):
            CachePolicy.model_validate({"mode": "exact", "sensitive": True})

    def test_semantic_cache_requires_identity_fields(self) -> None:
        with self.assertRaises(ValidationError):
            CachePolicy.model_validate({"mode": "semantic"})

    def test_execution_request_rejects_extra_fields(self) -> None:
        task = task_definition()
        with self.assertRaises(ValidationError):
            ExecutionRequest.model_validate(
                {
                    "task": task.model_dump(by_alias=True),
                    "input": {"text": "hello"},
                    "unexpected": True,
                }
            )

    def test_runtime_reasoning_effort_uses_api_native_values(self) -> None:
        task = task_definition()
        request = ExecutionRequest.model_validate(
            {
                "task": task.model_dump(by_alias=True),
                "input": {"text": "hello"},
                "runtime": {"reasoningEffort": "xhigh"},
            }
        )
        self.assertEqual(request.runtime.reasoning_effort, "xhigh")
        with self.assertRaises(ValidationError):
            ExecutionRequest.model_validate(
                {
                    "task": task.model_dump(by_alias=True),
                    "input": {"text": "hello"},
                    "runtime": {"reasoningEffort": "ultra"},
                }
            )

    def test_sensitive_task_rejects_async_execution(self) -> None:
        task = task_definition(cache_policy={"mode": "disabled", "sensitive": True})
        with self.assertRaises(ValidationError):
            ExecutionRequest.model_validate(
                {
                    "task": task.model_dump(by_alias=True),
                    "input": {"text": "hello"},
                    "asyncMode": True,
                }
            )

    def test_text_stream_requires_disabled_cache_and_string_result_field(self) -> None:
        with self.assertRaises(ValidationError):
            task_definition(
                stream_policy={"mode": "text_field", "resultField": "reply"},
            )
        with self.assertRaises(ValidationError):
            task_definition(
                cache_policy={"mode": "disabled"},
                stream_policy={"mode": "text_field", "resultField": "reply"},
                output_schema={
                    "type": "object",
                    "required": ["reply"],
                    "properties": {"reply": {"type": "number"}},
                },
            )

    def test_text_stream_rejects_async_mode_and_other_required_fields(self) -> None:
        with self.assertRaises(ValidationError):
            task_definition(
                cache_policy={"mode": "disabled"},
                stream_policy={"mode": "text_field", "resultField": "reply"},
                output_schema={
                    "type": "object",
                    "required": ["reply", "title"],
                    "properties": {
                        "reply": {"type": "string"},
                        "title": {"type": "string"},
                    },
                },
            )
        task = task_definition(
            cache_policy={"mode": "disabled"},
            stream_policy={"mode": "text_field", "resultField": "reply"},
            output_schema={
                "type": "object",
                "required": ["reply"],
                "properties": {"reply": {"type": "string"}},
            },
        )
        with self.assertRaises(ValidationError):
            ExecutionRequest.model_validate(
                {
                    "task": task.model_dump(by_alias=True),
                    "input": {"text": "hello"},
                    "asyncMode": True,
                }
            )

    def test_load_task_pack_and_hash_components(self) -> None:
        task = load_task_pack(Path(__file__).parents[1] / "examples" / "tasks" / "generic_summary")
        self.assertEqual(task.namespace, "example-project")
        self.assertTrue(task.digest.startswith("sha256:"))
        self.assertEqual(set(task.component_hashes), {
            "prompt",
            "inputSchema",
            "outputSchema",
            "taxonomy",
        })

    def test_loader_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "task"
            root.mkdir()
            (root / "task.json").write_text(
                json.dumps(
                    {
                        "namespace": "test",
                        "id": "escape",
                        "version": "1",
                        "title": "Escape",
                        "promptPath": "../prompt.md",
                        "inputSchemaPath": "input.json",
                        "outputSchemaPath": "output.json",
                    }
                ),
                encoding="utf-8",
            )
            (base / "prompt.md").write_text("prompt", encoding="utf-8")
            (root / "input.json").write_text('{"type":"object"}', encoding="utf-8")
            (root / "output.json").write_text('{"type":"object"}', encoding="utf-8")
            with self.assertRaises(ContractValidationError):
                load_task_pack(root)

    def test_schema_rejects_external_references(self) -> None:
        with self.assertRaises(ContractValidationError):
            validate_json_schema(
                {"type": "object", "$ref": "https://example.invalid/schema.json"},
                label="inputSchema",
            )


if __name__ == "__main__":
    unittest.main()
