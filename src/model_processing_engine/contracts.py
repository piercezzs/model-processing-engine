from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical import digest_json


MAX_PROMPT_CHARACTERS = 200_000
MAX_SERIALIZED_INPUT_BYTES = 2 * 1024 * 1024
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class CachePolicy(StrictModel):
    mode: Literal["disabled", "exact", "semantic"] = "exact"
    semantic_version: str = Field(default="1", alias="semanticVersion", max_length=128)
    identity_fields: list[str] = Field(default_factory=list, alias="identityFields", max_length=128)
    ttl_seconds: int | None = Field(default=None, alias="ttlSeconds", ge=1, le=31_536_000)
    sensitive: bool = False

    @model_validator(mode="after")
    def validate_policy(self) -> "CachePolicy":
        if self.sensitive and self.mode != "disabled":
            raise ValueError("sensitive tasks must set cachePolicy.mode to disabled")
        if self.mode == "semantic" and not self.identity_fields:
            raise ValueError("semantic cache mode requires identityFields")
        if self.mode != "semantic" and self.identity_fields:
            raise ValueError("identityFields are allowed only for semantic cache mode")
        return self


class RuntimeDefaults(StrictModel):
    provider_id: str | None = Field(default=None, alias="providerId", max_length=128)
    model: str | None = Field(default=None, max_length=256)
    temperature: float = Field(default=0.1, ge=0, le=2)
    max_tokens: int | None = Field(default=None, alias="maxTokens", ge=1, le=1_000_000)


class BatchPolicy(StrictModel):
    enabled: bool = False
    input_field: str = Field(default="items", alias="inputField", min_length=1, max_length=128)
    output_field: str = Field(default="items", alias="outputField", min_length=1, max_length=128)
    chunk_size: int = Field(default=20, alias="chunkSize", ge=1, le=500)
    concurrency: int = Field(default=1, ge=1, le=8)


class TaskDefinition(StrictModel):
    schema_version: int = Field(default=1, alias="schemaVersion", ge=1, le=1)
    namespace: str
    id: str
    version: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARACTERS)
    input_schema: dict[str, Any] = Field(alias="inputSchema")
    output_schema: dict[str, Any] = Field(alias="outputSchema")
    taxonomy: dict[str, Any] | None = None
    cache_policy: CachePolicy = Field(default_factory=CachePolicy, alias="cachePolicy")
    runtime_defaults: RuntimeDefaults = Field(default_factory=RuntimeDefaults, alias="runtimeDefaults")
    batch_policy: BatchPolicy = Field(default_factory=BatchPolicy, alias="batchPolicy")

    @field_validator("namespace", "id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        text = value.strip()
        if not IDENTIFIER_PATTERN.fullmatch(text):
            raise ValueError("must use letters, numbers, dot, underscore, or hyphen")
        return text

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_schemas(self) -> "TaskDefinition":
        if not self.input_schema:
            raise ValueError("inputSchema must not be empty")
        if not self.output_schema:
            raise ValueError("outputSchema must not be empty")
        return self

    def digest_payload(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "namespace": self.namespace,
            "id": self.id,
            "version": self.version,
            "prompt": self.prompt,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "taxonomy": self.taxonomy,
            "batchPolicy": self.batch_policy.model_dump(by_alias=True),
        }

    @property
    def digest(self) -> str:
        return digest_json(self.digest_payload())

    @property
    def component_hashes(self) -> dict[str, str]:
        return {
            "prompt": digest_json(self.prompt),
            "inputSchema": digest_json(self.input_schema),
            "outputSchema": digest_json(self.output_schema),
            "taxonomy": digest_json(self.taxonomy),
        }


class RuntimeOptions(StrictModel):
    provider_id: str | None = Field(default=None, alias="providerId", max_length=128)
    model: str | None = Field(default=None, max_length=256)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, alias="maxTokens", ge=1, le=1_000_000)
    force_refresh: bool = Field(default=False, alias="forceRefresh")


class ExecutionRequest(StrictModel):
    task: TaskDefinition
    input_payload: dict[str, Any] = Field(alias="input")
    runtime: RuntimeOptions = Field(default_factory=RuntimeOptions)
    async_mode: bool = Field(default=False, alias="asyncMode")

    @field_validator("input_payload")
    @classmethod
    def validate_input_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(serialized) > MAX_SERIALIZED_INPUT_BYTES:
            raise ValueError(f"serialized input exceeds {MAX_SERIALIZED_INPUT_BYTES} bytes")
        return value

    @model_validator(mode="after")
    def validate_sensitive_execution(self) -> "ExecutionRequest":
        if self.task.cache_policy.sensitive and self.async_mode:
            raise ValueError("sensitive tasks must use synchronous execution")
        return self


class ResultEnvelope(StrictModel):
    schema_version: int = Field(default=1, alias="schemaVersion")
    execution_id: str = Field(alias="executionId")
    status: Literal["queued", "running", "succeeded", "failed"]
    task: dict[str, Any]
    provider: dict[str, Any] = Field(default_factory=dict)
    cache: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    timing: dict[str, Any] = Field(default_factory=dict)
    progress: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
