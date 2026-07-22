from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .contracts import TaskDefinition
from .exceptions import ContractValidationError


MAX_TASK_FILE_BYTES = 2 * 1024 * 1024


def load_task_pack(task_dir: str | Path) -> TaskDefinition:
    root = Path(task_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Task pack directory not found: {root}")
    manifest_path = root / "task.json"
    manifest = _read_json(manifest_path)
    prompt = _read_text(_safe_child(root, manifest.pop("promptPath", "prompt.md")))
    input_schema = _read_json(_safe_child(root, manifest.pop("inputSchemaPath", "input.schema.json")))
    output_schema = _read_json(_safe_child(root, manifest.pop("outputSchemaPath", "output.schema.json")))
    taxonomy_path = manifest.pop("taxonomyPath", None)
    taxonomy = _read_json(_safe_child(root, taxonomy_path)) if taxonomy_path else None
    task = TaskDefinition.model_validate(
        {
            **manifest,
            "prompt": prompt,
            "inputSchema": input_schema,
            "outputSchema": output_schema,
            "taxonomy": taxonomy,
        }
    )
    validate_json_schema(task.input_schema, label="inputSchema")
    validate_json_schema(task.output_schema, label="outputSchema")
    return task


def validate_json_schema(schema: dict[str, Any], *, label: str) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ContractValidationError(f"{label} is not a valid JSON Schema: {exc}") from exc
    external_reference = _first_external_reference(schema)
    if external_reference:
        raise ContractValidationError(
            f"{label} contains an external $ref, which is not allowed: {external_reference}"
        )


def _first_external_reference(value: Any) -> str | None:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#"):
            return reference
        for child in value.values():
            found = _first_external_reference(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_external_reference(child)
            if found:
                return found
    return None


def _safe_child(root: Path, value: Any) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ContractValidationError("Task pack file path must not be empty")
    path = (root / text).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ContractValidationError(f"Task pack path escapes its directory: {text}") from exc
    if not path.is_file():
        raise FileNotFoundError(f"Task pack file not found: {path}")
    if path.stat().st_size > MAX_TASK_FILE_BYTES:
        raise ContractValidationError(f"Task pack file exceeds {MAX_TASK_FILE_BYTES} bytes: {path.name}")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(_read_text(path))
    if not isinstance(data, dict):
        raise ContractValidationError(f"Expected a JSON object: {path}")
    return data


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size > MAX_TASK_FILE_BYTES:
        raise ContractValidationError(f"Task pack file exceeds {MAX_TASK_FILE_BYTES} bytes: {path.name}")
    return path.read_text(encoding="utf-8")
