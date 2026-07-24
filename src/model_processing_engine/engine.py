from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator
from uuid import uuid4

from jsonschema import Draft202012Validator

from .cache import CacheRecord, SQLiteRuntimeStore
from .canonical import digest_json, field_value
from .contracts import ExecutionRequest, ResultEnvelope, TaskDefinition
from .exceptions import ContractValidationError, ExecutionNotFoundError, ProviderError
from .providers.base import ModelProvider, ProviderCallResult, ProviderRegistry


ProgressCallback = Callable[[dict[str, Any]], None]
ENGINE_CACHE_SCHEMA = "mpe-cache-v2"


class _SingleFlightManager:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, tuple[threading.Lock, int]] = {}

    @contextmanager
    def acquire(self, key: str) -> Iterator[None]:
        with self._guard:
            lock, count = self._entries.get(key, (threading.Lock(), 0))
            self._entries[key] = (lock, count + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                current_lock, current_count = self._entries[key]
                if current_count <= 1:
                    self._entries.pop(key, None)
                else:
                    self._entries[key] = (current_lock, current_count - 1)


class ModelProcessingEngine:
    def __init__(
        self,
        *,
        providers: ProviderRegistry,
        store: SQLiteRuntimeStore,
        max_provider_concurrency: int = 8,
    ) -> None:
        self.providers = providers
        self.store = store
        self.store.purge_cache_schema_mismatches(ENGINE_CACHE_SCHEMA)
        service_limit = max(1, max_provider_concurrency)
        self._provider_limits = {
            provider_id: min(
                service_limit,
                self.providers.get(provider_id).config.max_concurrency,
            )
            for provider_id in self.providers.ids()
        }
        self._provider_slots = {
            provider_id: threading.BoundedSemaphore(limit)
            for provider_id, limit in self._provider_limits.items()
        }
        self._single_flight = _SingleFlightManager()

    def provider_descriptors(self) -> list[dict[str, Any]]:
        descriptors = self.providers.descriptors()
        for descriptor in descriptors:
            provider_id = str(descriptor["id"])
            configured = int(descriptor["maxConcurrency"])
            descriptor["configuredMaxConcurrency"] = configured
            descriptor["maxConcurrency"] = self._provider_limits[provider_id]
        return descriptors

    def reserve(
        self,
        request: ExecutionRequest,
        *,
        persist: bool = True,
    ) -> ResultEnvelope:
        execution_id = uuid4().hex
        envelope = self._base_envelope(request.task, execution_id=execution_id, status="queued")
        if persist:
            self._save_execution(request.task, envelope)
        return ResultEnvelope.model_validate(envelope)

    def execute(
        self,
        request: ExecutionRequest,
        *,
        execution_id: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ResultEnvelope:
        execution_id = execution_id or uuid4().hex
        started_timer = time.perf_counter()
        envelope = self._base_envelope(request.task, execution_id=execution_id, status="running")
        envelope["timing"]["startedAt"] = _utc_now()
        self._save_execution(request.task, envelope)

        progress_lock = threading.Lock()

        def progress(event: str, **fields: Any) -> None:
            with progress_lock:
                envelope["progress"] = {"event": event, **fields}
                self._save_execution(request.task, envelope)
            if progress_callback:
                progress_callback(copy.deepcopy(envelope["progress"]))

        try:
            validate_instance(request.input_payload, request.task.input_schema, label="input")
            provider = self.providers.get(
                request.runtime.provider_id or request.task.runtime_defaults.provider_id
            )
            model = (
                request.runtime.model
                or request.task.runtime_defaults.model
                or provider.config.default_model
            ).strip()
            temperature = (
                request.runtime.temperature
                if request.runtime.temperature is not None
                else request.task.runtime_defaults.temperature
            )
            max_tokens = (
                request.runtime.max_tokens
                if request.runtime.max_tokens is not None
                else request.task.runtime_defaults.max_tokens
            )
            contract_retries = (
                request.runtime.contract_retries
                if request.runtime.contract_retries is not None
                else request.task.runtime_defaults.contract_retries
            )
            envelope["provider"] = {
                "id": provider.config.id,
                "type": provider.config.type,
                "model": model,
                "identityDigest": provider.config.digest,
            }
            cache_key = self._cache_key(
                task=request.task,
                input_payload=request.input_payload,
                provider=provider,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            cache_allowed = request.task.cache_policy.mode != "disabled"
            progress("prepared", cacheMode=request.task.cache_policy.mode)

            if cache_allowed and not request.runtime.force_refresh:
                cached = self._valid_cached_result(request.task, cache_key)
                if cached:
                    return self._complete_from_cache(
                        request.task,
                        envelope,
                        cached,
                        started_timer=started_timer,
                    )

            if cache_allowed and not request.runtime.force_refresh:
                with self._single_flight.acquire(f"{request.task.namespace}:{cache_key}"):
                    cached = self._valid_cached_result(request.task, cache_key)
                    if cached:
                        return self._complete_from_cache(
                            request.task,
                            envelope,
                            cached,
                            started_timer=started_timer,
                        )
                    result, usage, provider_report = self._run_provider(
                        execution_id=execution_id,
                        task=request.task,
                        input_payload=request.input_payload,
                        provider=provider,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        contract_retries=contract_retries,
                        progress=progress,
                    )
                    self._write_cache(
                        task=request.task,
                        cache_key=cache_key,
                        provider=provider,
                        model=model,
                        result=result,
                    )
            else:
                result, usage, provider_report = self._run_provider(
                    execution_id=execution_id,
                    task=request.task,
                    input_payload=request.input_payload,
                    provider=provider,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    contract_retries=contract_retries,
                    progress=progress,
                )
                if cache_allowed:
                    self._write_cache(
                        task=request.task,
                        cache_key=cache_key,
                        provider=provider,
                        model=model,
                        result=result,
                    )

            envelope.update(
                {
                    "status": "succeeded",
                    "cache": {
                        "mode": request.task.cache_policy.mode,
                        "hit": False,
                        "key": cache_key if cache_allowed else None,
                        "forceRefresh": request.runtime.force_refresh,
                    },
                    "usage": usage,
                    "result": result,
                    "progress": {"event": "completed", "processed": provider_report["processed"]},
                }
            )
            envelope["timing"].update(
                {
                    "completedAt": _utc_now(),
                    "elapsedMs": _elapsed_ms(started_timer),
                    "providerElapsedMs": provider_report["providerElapsedMs"],
                    "providerCallCount": provider_report["providerCallCount"],
                    "transportRetries": provider_report["transportRetries"],
                    "contractRepairs": provider_report["contractRepairs"],
                }
            )
        except Exception as exc:
            envelope.update(
                {
                    "status": "failed",
                    "error": _safe_error(exc),
                    "progress": {"event": "failed"},
                }
            )
            envelope["timing"].update(
                {
                    "completedAt": _utc_now(),
                    "elapsedMs": _elapsed_ms(started_timer),
                }
            )
        self._save_execution(request.task, envelope)
        return ResultEnvelope.model_validate(envelope)

    def get_execution(self, execution_id: str) -> ResultEnvelope:
        record = self.store.get_execution(execution_id)
        if not record:
            raise ExecutionNotFoundError(f"Unknown execution: {execution_id}")
        return ResultEnvelope.model_validate(record)

    def _run_provider(
        self,
        *,
        execution_id: str,
        task: TaskDefinition,
        input_payload: dict[str, Any],
        provider: ModelProvider,
        model: str,
        temperature: float,
        max_tokens: int | None,
        contract_retries: int,
        progress: Callable[..., None],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
        batch = task.batch_policy
        if not batch.enabled:
            progress("provider_call_started", chunkIndex=1, chunkCount=1, processed=0)
            call, calls = self._provider_call_with_contract_repair(
                execution_id=execution_id,
                sequence_start=1,
                task=task,
                input_payload=input_payload,
                provider=provider,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                contract_retries=contract_retries,
                progress=progress,
                chunk_index=1,
                chunk_count=1,
            )
            progress("provider_call_completed", chunkIndex=1, chunkCount=1, processed=1)
            return call.content, _merge_usage(calls), _provider_report(
                calls,
                processed=1,
                contract_repairs=len(calls) - 1,
            )

        items = input_payload.get(batch.input_field)
        if not isinstance(items, list):
            raise ContractValidationError(
                f"batch input field {batch.input_field!r} must be an array"
            )
        chunks = [items[index : index + batch.chunk_size] for index in range(0, len(items), batch.chunk_size)]
        if not chunks:
            empty_result = {batch.output_field: []}
            validate_instance(empty_result, task.output_schema, label="output")
            progress("provider_call_completed", chunkIndex=0, chunkCount=0, processed=0)
            return empty_result, _merge_usage([]), _provider_report([], processed=0)
        results: dict[int, ProviderCallResult] = {}
        call_records: dict[int, list[ProviderCallResult]] = {}
        completed = 0
        progress("provider_call_started", chunkIndex=0, chunkCount=len(chunks), processed=0)

        def call_chunk(
            index: int,
            chunk: list[Any],
        ) -> tuple[int, ProviderCallResult, list[ProviderCallResult]]:
            chunk_input = copy.deepcopy(input_payload)
            chunk_input[batch.input_field] = chunk
            call, calls = self._provider_call_with_contract_repair(
                execution_id=execution_id,
                sequence_start=index * (contract_retries + 1) + 1,
                task=task,
                input_payload=chunk_input,
                provider=provider,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                contract_retries=contract_retries,
                progress=progress,
                chunk_index=index + 1,
                chunk_count=len(chunks),
            )
            return index, call, calls

        worker_count = min(
            len(chunks),
            batch.concurrency,
            self._provider_limits[provider.config.id],
        )
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(call_chunk, index, chunk): index
                for index, chunk in enumerate(chunks)
            }
            for future in as_completed(futures):
                index, call, calls = future.result()
                results[index] = call
                call_records[index] = calls
                completed += len(chunks[index])
                progress(
                    "provider_chunk_completed",
                    chunkIndex=len(results),
                    chunkCount=len(chunks),
                    processed=completed,
                )
        ordered = [results[index] for index in range(len(chunks))]
        ordered_calls = [
            call
            for index in range(len(chunks))
            for call in call_records[index]
        ]
        merged = _merge_batch_outputs(ordered, output_field=batch.output_field)
        validate_instance(merged, task.output_schema, label="output")
        return merged, _merge_usage(ordered_calls), _provider_report(
            ordered_calls,
            processed=len(items),
            contract_repairs=len(ordered_calls) - len(ordered),
        )

    def _provider_call_with_contract_repair(
        self,
        *,
        execution_id: str,
        sequence_start: int,
        task: TaskDefinition,
        input_payload: dict[str, Any],
        provider: ModelProvider,
        model: str,
        temperature: float,
        max_tokens: int | None,
        contract_retries: int,
        progress: Callable[..., None],
        chunk_index: int,
        chunk_count: int,
    ) -> tuple[ProviderCallResult, list[ProviderCallResult]]:
        calls: list[ProviderCallResult] = []
        repair_feedback: str | None = None
        previous_output: dict[str, Any] | None = None
        for repair_attempt in range(contract_retries + 1):
            call = self._provider_call(
                execution_id=execution_id,
                sequence=sequence_start + repair_attempt,
                purpose="task" if repair_attempt == 0 else "contract_repair",
                task=task,
                input_payload=input_payload,
                provider=provider,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                repair_feedback=repair_feedback,
                previous_output=previous_output,
            )
            calls.append(call)
            try:
                validate_instance(call.content, task.output_schema, label="output")
            except ContractValidationError as exc:
                if repair_attempt >= contract_retries:
                    raise
                repair_feedback = str(exc)
                previous_output = call.content
                progress(
                    "provider_contract_repair",
                    chunkIndex=chunk_index,
                    chunkCount=chunk_count,
                    repairAttempt=repair_attempt + 1,
                    repairLimit=contract_retries,
                )
                continue
            return call, calls
        raise AssertionError("contract repair loop exited without a result")

    def _provider_call(
        self,
        *,
        execution_id: str,
        sequence: int,
        purpose: str,
        task: TaskDefinition,
        input_payload: dict[str, Any],
        provider: ModelProvider,
        model: str,
        temperature: float,
        max_tokens: int | None,
        repair_feedback: str | None,
        previous_output: dict[str, Any] | None,
    ) -> ProviderCallResult:
        model_payload = {
            "outputSchema": task.output_schema,
        }
        if task.taxonomy is not None:
            model_payload["taxonomy"] = task.taxonomy
        model_payload["input"] = input_payload
        call_id = uuid4().hex
        try:
            with self._provider_slots[provider.config.id]:
                call = provider.call_json(
                    model=model,
                    system_prompt=task.prompt,
                    input_payload=model_payload,
                    output_schema=task.output_schema,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    repair_feedback=repair_feedback,
                    previous_output=previous_output,
                )
        except Exception as exc:
            usage = dict(exc.usage) if isinstance(exc, ProviderError) else {}
            attempts = exc.attempts if isinstance(exc, ProviderError) else 0
            elapsed_ms = exc.elapsed_ms if isinstance(exc, ProviderError) else 0
            audit_error = (
                exc.audit_message
                if isinstance(exc, ProviderError)
                else exc.__class__.__name__
            )
            self.store.save_provider_call(
                {
                    "callId": call_id,
                    "executionId": execution_id,
                    "purpose": purpose,
                    "sequence": sequence,
                    "providerId": provider.config.id,
                    "model": model,
                    "status": "failed",
                    "usage": usage,
                    "attempts": attempts,
                    "elapsedMs": elapsed_ms,
                    "error": audit_error,
                }
            )
            raise
        self.store.save_provider_call(
            {
                "callId": call_id,
                "executionId": execution_id,
                "purpose": purpose,
                "sequence": sequence,
                "providerId": provider.config.id,
                "model": model,
                "status": "succeeded",
                "usage": call.usage,
                "attempts": call.attempts,
                "elapsedMs": call.elapsed_ms,
                "error": "",
            }
        )
        return call

    def _cache_key(
        self,
        *,
        task: TaskDefinition,
        input_payload: dict[str, Any],
        provider: ModelProvider,
        model: str,
        temperature: float,
        max_tokens: int | None,
    ) -> str:
        policy = task.cache_policy
        if policy.mode == "semantic":
            missing = object()
            input_identity: dict[str, Any] = {}
            for path in policy.identity_fields:
                value = field_value(input_payload, path, default=missing)
                if value is missing:
                    raise ContractValidationError(
                        f"semantic cache identity field is missing: {path}"
                    )
                input_identity[path] = value
        else:
            input_identity = input_payload
        return digest_json(
            {
                "cacheSchema": ENGINE_CACHE_SCHEMA,
                "namespace": task.namespace,
                "taskDigest": task.digest,
                "semanticVersion": policy.semantic_version,
                "providerIdentity": provider.config.identity,
                "providerDigest": provider.config.digest,
                "model": model,
                "temperature": temperature,
                "maxTokens": max_tokens,
                "input": input_identity,
            }
        )

    def _valid_cached_result(self, task: TaskDefinition, cache_key: str) -> CacheRecord | None:
        cached = self.store.get_cache(task.namespace, cache_key)
        if not cached:
            return None
        try:
            validate_instance(cached.result, task.output_schema, label="cached output")
        except ContractValidationError:
            self.store.delete_cache(task.namespace, cache_key)
            return None
        return cached

    def _write_cache(
        self,
        *,
        task: TaskDefinition,
        cache_key: str,
        provider: ModelProvider,
        model: str,
        result: dict[str, Any],
    ) -> None:
        self.store.put_cache(
            namespace=task.namespace,
            key=cache_key,
            task_id=task.id,
            task_version=task.version,
            provider_id=provider.config.id,
            model=model,
            result=result,
            metadata={
                "cacheSchema": ENGINE_CACHE_SCHEMA,
                "taskDigest": task.digest,
                "componentHashes": task.component_hashes,
                "providerIdentityDigest": provider.config.digest,
            },
            ttl_seconds=task.cache_policy.ttl_seconds,
        )

    def _complete_from_cache(
        self,
        task: TaskDefinition,
        envelope: dict[str, Any],
        cached: CacheRecord,
        *,
        started_timer: float,
    ) -> ResultEnvelope:
        envelope.update(
            {
                "status": "succeeded",
                "cache": {
                    "mode": envelope["cache"]["mode"],
                    "source": "local_result",
                    "hit": True,
                    "key": cached.key,
                    "createdAt": _timestamp_iso(cached.created_at),
                    "ageSeconds": max(0, int(time.time() - cached.created_at)),
                    "hitCount": cached.hit_count,
                },
                "usage": {
                    "available": False,
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "totalTokens": 0,
                    "source": "local_result_cache",
                },
                "result": cached.result,
                "progress": {"event": "completed", "processed": 0},
            }
        )
        envelope["timing"].update(
            {
                "completedAt": _utc_now(),
                "elapsedMs": _elapsed_ms(started_timer),
                "providerElapsedMs": 0,
                "providerCallCount": 0,
                "transportRetries": 0,
                "contractRepairs": 0,
            }
        )
        self._save_execution(task, envelope)
        return ResultEnvelope.model_validate(envelope)

    def _save_execution(self, task: TaskDefinition, envelope: dict[str, Any]) -> None:
        if not task.cache_policy.sensitive:
            self.store.save_execution(envelope)
            return
        persisted = copy.deepcopy(envelope)
        persisted["result"] = None
        warning = "Sensitive result omitted from persistent execution record"
        warnings = persisted.get("warnings")
        persisted["warnings"] = [*warnings, warning] if isinstance(warnings, list) else [warning]
        self.store.save_execution(persisted)

    def _base_envelope(
        self,
        task: TaskDefinition,
        *,
        execution_id: str,
        status: str,
    ) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "executionId": execution_id,
            "status": status,
            "task": {
                "namespace": task.namespace,
                "id": task.id,
                "version": task.version,
                "digest": task.digest,
                "componentHashes": task.component_hashes,
            },
            "provider": {},
            "cache": {"mode": task.cache_policy.mode, "hit": False},
            "usage": {},
            "timing": {"createdAt": _utc_now()},
            "progress": {"event": status},
            "result": None,
            "warnings": [],
            "error": None,
        }


def validate_instance(value: Any, schema: dict[str, Any], *, label: str) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.path))
    if not errors:
        return
    error = errors[0]
    location = ".".join(str(part) for part in error.absolute_path) or "<root>"
    raise ContractValidationError(f"{label} validation failed at {location}: {error.message}")


def _merge_batch_outputs(calls: list[ProviderCallResult], *, output_field: str) -> dict[str, Any]:
    merged: dict[str, Any] | None = None
    combined: list[Any] = []
    for call in calls:
        content = call.content
        values = content.get(output_field)
        if not isinstance(values, list):
            raise ContractValidationError(
                f"batch output field {output_field!r} must be an array"
            )
        combined.extend(values)
        other = {key: value for key, value in content.items() if key != output_field}
        if merged is None:
            merged = other
        elif merged != other:
            raise ContractValidationError("batch outputs contain conflicting non-list fields")
    return {**(merged or {}), output_field: combined}


def _merge_usage(calls: list[ProviderCallResult]) -> dict[str, Any]:
    fields = ["inputTokens", "outputTokens", "totalTokens", "cacheReadInputTokens"]
    return {
        "available": any(bool(call.usage.get("available")) for call in calls),
        **{
            field: sum(int(call.usage.get(field) or 0) for call in calls)
            for field in fields
        },
        "source": "provider_response" if any(call.usage.get("available") for call in calls) else "provider_response_without_usage",
    }


def _provider_report(
    calls: list[ProviderCallResult],
    *,
    processed: int,
    contract_repairs: int = 0,
) -> dict[str, int]:
    return {
        "providerCallCount": len(calls),
        "transportRetries": sum(max(0, call.attempts - 1) for call in calls),
        "contractRepairs": max(0, contract_repairs),
        "providerElapsedMs": sum(call.elapsed_ms for call in calls),
        "processed": processed,
    }


def _safe_error(error: Exception) -> str:
    text = " ".join(str(error).split())
    return f"{error.__class__.__name__}: {text[:1000]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp_iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def _elapsed_ms(started_timer: float) -> int:
    return max(0, int((time.perf_counter() - started_timer) * 1000))
