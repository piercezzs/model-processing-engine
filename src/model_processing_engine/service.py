from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .async_queue import PersistentAsyncExecutor
from .admin_config import (
    AdminConfigManager,
    ApplyProviderRequest,
    ModelDiscoveryRequest,
    ProviderDraft,
)
from .admin_restart import schedule_managed_restart
from .contracts import ExecutionRequest
from .constants import API_VERSION, APP_ID, VERSION
from .engine import ModelProcessingEngine
from .exceptions import (
    AsyncQueueFullError,
    ConfigurationError,
    ContractValidationError,
    ExecutionNotFoundError,
    ProviderConfigurationNotFoundError,
    ProviderError,
)
from .factory import build_default_engine
from .settings import Settings, load_settings


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, maximum_bytes: int) -> None:
        self.app = app
        self.maximum_bytes = maximum_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _declared_content_length(scope)
        if declared is not None and (declared < 0 or declared > self.maximum_bytes):
            await _request_too_large_response(scope, receive, send)
            return

        messages: list[Message] = []
        received = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            received += len(message.get("body", b""))
            if received > self.maximum_bytes:
                await _request_too_large_response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive() -> Message:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)


def _declared_content_length(scope: Scope) -> int | None:
    values = [
        value
        for name, value in scope.get("headers", [])
        if name.lower() == b"content-length"
    ]
    if not values:
        return None
    if len(values) != 1:
        return -1
    try:
        value = int(values[0])
    except ValueError:
        return -1
    return value if value >= 0 else -1


async def _request_too_large_response(
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    response = Response(status_code=413, content="Request body too large")
    await response(scope, receive, send)


def create_app(
    *,
    engine: ModelProcessingEngine | None = None,
    settings: Settings | None = None,
    restart_scheduler: Callable[[Settings], None] | None = None,
) -> FastAPI:
    runtime = settings or load_settings()
    task_engine = engine or build_default_engine(settings=runtime)
    async_executor = PersistentAsyncExecutor(
        engine=task_engine,
        store=task_engine.store,
        worker_count=runtime.async_worker_count,
        capacity=runtime.async_queue_capacity,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        async_executor.start()
        try:
            yield
        finally:
            async_executor.stop()

    app = FastAPI(
        title="Model Processing Engine",
        version=VERSION,
        description="Business-neutral runtime for externally defined model tasks.",
        lifespan=lifespan,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        maximum_bytes=runtime.max_request_bytes,
    )
    admin_manager = AdminConfigManager(runtime.project_dir) if runtime.project_dir else None
    admin_csrf_token = secrets.token_urlsafe(32)
    restart_service_later = restart_scheduler or schedule_managed_restart
    admin_assets = Path(__file__).with_name("admin_ui")

    @app.middleware("http")
    async def enforce_api_token(request: Request, call_next: Any) -> Response:
        health_path = f"/{API_VERSION}/health"
        health_is_public = request.url.path == health_path and not runtime.allow_remote
        admin_is_local = _is_admin_path(request.url.path) and not runtime.allow_remote
        if runtime.api_token and not health_is_public and not admin_is_local:
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {runtime.api_token}"
            if not hmac.compare_digest(supplied, expected):
                return Response(status_code=401, content="Unauthorized")
        return await call_next(request)

    @app.middleware("http")
    async def enforce_admin_boundary(request: Request, call_next: Any) -> Response:
        if not _is_admin_path(request.url.path):
            return await call_next(request)
        if admin_manager is None or runtime.allow_remote or not _loopback_host(runtime.host):
            return Response(status_code=404, content="Not found")
        host_header = request.headers.get("host", "")
        if not _loopback_host_header(host_header):
            return Response(status_code=403, content="Invalid admin host")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not _same_origin(request.headers.get("origin", ""), request.url.scheme, host_header):
                return Response(status_code=403, content="Invalid admin origin")
            supplied_csrf = request.headers.get("x-mpe-csrf", "")
            if not hmac.compare_digest(supplied_csrf, admin_csrf_token):
                return Response(status_code=403, content="Invalid admin CSRF token")
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get(f"/{API_VERSION}/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "app": APP_ID,
            "version": VERSION,
            "apiVersion": API_VERSION,
            "instanceId": runtime.instance_id,
            "processId": os.getpid(),
            "providers": task_engine.providers.ids(),
            "cacheEntries": task_engine.store.cache_count(),
            "asyncQueue": async_executor.stats(),
        }

    @app.get(f"/{API_VERSION}/providers")
    def providers() -> dict[str, Any]:
        return {
            "providers": task_engine.providers.ids(),
            "providerDetails": task_engine.provider_descriptors(),
            "defaultProviderId": task_engine.providers.default_provider_id,
        }

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_page() -> FileResponse:
        return FileResponse(admin_assets / "index.html", media_type="text/html")

    @app.get("/admin/app.js", include_in_schema=False)
    def admin_script() -> FileResponse:
        return FileResponse(admin_assets / "app.js", media_type="text/javascript")

    @app.get("/admin/styles.css", include_in_schema=False)
    def admin_styles() -> FileResponse:
        return FileResponse(admin_assets / "styles.css", media_type="text/css")

    @app.get(f"/{API_VERSION}/admin/config")
    def admin_config() -> dict[str, Any]:
        assert admin_manager is not None
        return {
            "status": "success",
            "csrfToken": admin_csrf_token,
            "service": {
                "app": APP_ID,
                "version": VERSION,
                "apiVersion": API_VERSION,
                "processId": os.getpid(),
            },
            "config": admin_manager.snapshot(),
        }

    def history_payload(
        *,
        limit: int,
        offset: int,
        namespace: str | None,
        task_id: str | None,
        status: str | None,
        kind: str | None,
        provider_id: str | None,
        model: str | None,
        created_from: float | None,
        created_to: float | None,
        include_summary: bool,
    ) -> dict[str, Any]:
        return task_engine.store.execution_history(
            limit=limit,
            offset=offset,
            namespace=namespace,
            task_id=task_id,
            status=status,
            kind=kind,
            provider_id=provider_id,
            model=model,
            created_from=created_from,
            created_to=created_to,
            include_summary=include_summary,
        )

    @app.get(f"/{API_VERSION}/admin/executions")
    def admin_execution_history(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        namespace: str | None = Query(default=None, max_length=256),
        task_id: str | None = Query(default=None, alias="taskId", max_length=256),
        status: str | None = Query(
            default=None,
            pattern="^(queued|running|succeeded|failed|cancelled)$",
        ),
        kind: str | None = Query(default=None, pattern="^(task|provider_test)$"),
        provider_id: str | None = Query(
            default=None,
            alias="providerId",
            max_length=256,
        ),
        model: str | None = Query(default=None, max_length=256),
        created_from: float | None = Query(default=None, alias="createdFrom"),
        created_to: float | None = Query(default=None, alias="createdTo"),
        include_summary: bool = Query(default=True, alias="includeSummary"),
    ) -> dict[str, Any]:
        return history_payload(
            limit=limit,
            offset=offset,
            namespace=namespace,
            task_id=task_id,
            status=status,
            kind=kind,
            provider_id=provider_id,
            model=model,
            created_from=created_from,
            created_to=created_to,
            include_summary=include_summary,
        )

    @app.get(f"/{API_VERSION}/admin/execution-stats")
    def admin_execution_statistics(
        period: str = Query(default="day", pattern="^(day|month|year)$"),
        anchor: str = Query(min_length=4, max_length=10),
        timezone_name: str = Query(
            default="UTC",
            alias="timezone",
            min_length=1,
            max_length=128,
        ),
        namespace: str | None = Query(default=None, max_length=256),
        task_id: str | None = Query(default=None, alias="taskId", max_length=256),
        status: str | None = Query(
            default=None,
            pattern="^(queued|running|succeeded|failed|cancelled)$",
        ),
        kind: str = Query(default="task", pattern="^(task|provider_test|all)$"),
        provider_id: str | None = Query(
            default=None,
            alias="providerId",
            max_length=256,
        ),
        model: str | None = Query(default=None, max_length=256),
    ) -> dict[str, Any]:
        try:
            return task_engine.store.execution_statistics(
                period=period,
                anchor=anchor,
                timezone_name=timezone_name,
                namespace=namespace,
                task_id=task_id,
                status=status,
                kind=None if kind == "all" else kind,
                provider_id=provider_id,
                model=model,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(f"/{API_VERSION}/admin/providers/test")
    def admin_test_provider(draft: ProviderDraft) -> dict[str, Any]:
        assert admin_manager is not None
        try:
            result = admin_manager.test_provider(draft)
        except ConfigurationError as exc:
            _record_provider_test(task_engine, draft, error=exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ProviderError as exc:
            _record_provider_test(task_engine, draft, error=exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        audit_execution_id = _record_provider_test(task_engine, draft, result=result)
        public_result = {
            key: value
            for key, value in result.items()
            if key != "providerCalls"
        }
        return {**public_result, "auditExecutionId": audit_execution_id}

    @app.post(f"/{API_VERSION}/admin/providers/models")
    def admin_discover_models(draft: ModelDiscoveryRequest) -> dict[str, Any]:
        assert admin_manager is not None
        try:
            return admin_manager.discover_models(draft)
        except ConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post(f"/{API_VERSION}/admin/providers/apply")
    def admin_apply_provider(
        request: ApplyProviderRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        assert admin_manager is not None
        try:
            result = admin_manager.apply_provider(request)
        except ConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        background_tasks.add_task(restart_service_later, runtime)
        return result

    @app.delete(f"/{API_VERSION}/admin/providers/{{provider_id}}")
    def admin_delete_provider(
        provider_id: str,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        assert admin_manager is not None
        try:
            result = admin_manager.delete_provider(provider_id)
        except ProviderConfigurationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConfigurationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        background_tasks.add_task(restart_service_later, runtime)
        return result

    @app.post(f"/{API_VERSION}/executions")
    def execute(request: ExecutionRequest) -> dict[str, Any]:
        if not request.async_mode:
            return task_engine.execute(request).model_dump(by_alias=True)
        try:
            reserved = async_executor.enqueue(request)
        except AsyncQueueFullError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return reserved.model_dump(by_alias=True)

    @app.post(f"/{API_VERSION}/executions/stream")
    def execute_stream(request: ExecutionRequest) -> StreamingResponse:
        try:
            task_engine.validate_stream_request(request)
        except (ConfigurationError, ContractValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def stream_events() -> Any:
            for stream_event in task_engine.execute_stream(request):
                payload = stream_event.model_dump(
                    by_alias=True,
                    exclude_none=True,
                )
                yield (
                    f"id: {stream_event.sequence}\n"
                    f"event: {stream_event.event}\n"
                    f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
                )

        return StreamingResponse(
            stream_events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get(f"/{API_VERSION}/executions/{{execution_id}}")
    def execution(execution_id: str) -> dict[str, Any]:
        try:
            return task_engine.get_execution(execution_id).model_dump(by_alias=True)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(f"/{API_VERSION}/cache/cleanup")
    def cleanup_cache() -> dict[str, int]:
        return {"removed": task_engine.store.cleanup_expired()}

    app.state.engine = task_engine
    app.state.settings = runtime
    app.state.async_executor = async_executor
    app.state.admin_manager = admin_manager
    return app


def _is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/") or path.startswith(f"/{API_VERSION}/admin/")


def _record_provider_test(
    engine: ModelProcessingEngine,
    draft: ProviderDraft,
    *,
    result: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> str:
    now = datetime.now(timezone.utc).isoformat()
    provider_calls = list((result or {}).get("providerCalls") or [])
    if isinstance(error, ProviderError):
        provider_calls = list(error.audit_calls or [{
            "status": "failed",
            "usage": dict(error.usage),
            "attempts": error.attempts,
            "elapsedMs": error.elapsed_ms,
            "error": error.audit_message,
        }])
    usage = dict((result or {}).get("usage") or {})
    if provider_calls:
        usage_fields = (
            "inputTokens",
            "outputTokens",
            "totalTokens",
            "cacheReadInputTokens",
        )
        usage = {
            "available": any(
                bool(call.get("usage", {}).get("available"))
                for call in provider_calls
            ),
            **{
                field: sum(
                    max(0, int(call.get("usage", {}).get(field) or 0))
                    for call in provider_calls
                )
                for field in usage_fields
            },
            "source": "provider_call_audit",
        }
    usage.setdefault("available", False)
    usage.setdefault("inputTokens", 0)
    usage.setdefault("outputTokens", 0)
    usage.setdefault("totalTokens", 0)
    usage.setdefault("cacheReadInputTokens", 0)
    usage.setdefault("source", "provider_response_without_usage")
    execution_id = "probe_" + uuid.uuid4().hex
    elapsed_ms = (
        sum(max(0, int(call.get("elapsedMs") or 0)) for call in provider_calls)
        if provider_calls
        else max(0, int((result or {}).get("elapsedMs") or 0))
    )
    provider_call_count = (
        len(provider_calls)
        if provider_calls
        else max(0, int((result or {}).get("providerCallCount") or 0))
    )
    transport_retries = (
        sum(
            max(0, int(call.get("attempts") or 0) - 1)
            for call in provider_calls
        )
        if provider_calls
        else max(0, int((result or {}).get("transportRetries") or 0))
    )
    audit_error = (
        error.audit_message
        if isinstance(error, ProviderError)
        else error.__class__.__name__
        if error
        else ""
    )
    engine.store.save_execution(
        {
            "schemaVersion": 1,
            "executionId": execution_id,
            "status": "failed" if error else "succeeded",
            "task": {
                "namespace": "_mpe",
                "id": "provider_connection_test",
                "version": "1",
                "kind": "provider_test",
            },
            "provider": {
                "id": draft.provider_id,
                "model": draft.model or "schema-sample-v1",
                "reasoning": {
                    "requested": draft.default_reasoning_effort,
                    "effective": (
                        None
                        if draft.default_reasoning_effort == "auto"
                        else draft.default_reasoning_effort
                    ),
                    "source": "provider_test",
                },
            },
            "cache": {"mode": "disabled", "hit": False},
            "usage": usage,
            "timing": {
                "createdAt": now,
                "completedAt": now,
                "elapsedMs": elapsed_ms,
                "providerElapsedMs": elapsed_ms,
                "providerCallCount": provider_call_count,
                "transportRetries": transport_retries,
            },
            "progress": {"event": "failed" if error else "completed"},
            "result": None,
            "warnings": [],
            "error": audit_error or None,
        }
    )
    for index, call in enumerate(provider_calls, start=1):
        engine.store.save_provider_call(
            {
                "callId": uuid.uuid4().hex,
                "executionId": execution_id,
                "purpose": "provider_test",
                "sequence": index,
                "providerId": draft.provider_id,
                "model": draft.model or "schema-sample-v1",
                "reasoningEffort": draft.default_reasoning_effort,
                "status": str(call.get("status") or "failed"),
                "usage": dict(call.get("usage") or {}),
                "attempts": int(call.get("attempts") or 0),
                "elapsedMs": int(call.get("elapsedMs") or 0),
                "error": str(call.get("error") or ""),
            }
        )
    return execution_id


def _loopback_host(value: str) -> bool:
    if value.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _loopback_host_header(value: str) -> bool:
    try:
        parsed = urlparse(f"//{value}")
        return _loopback_host(parsed.hostname or "")
    except ValueError:
        return False


def _same_origin(origin: str, scheme: str, host_header: str) -> bool:
    try:
        parsed = urlparse(origin)
        expected = urlparse(f"{scheme}://{host_header}")
        return (
            parsed.scheme.casefold() == expected.scheme.casefold()
            and (parsed.hostname or "").casefold() == (expected.hostname or "").casefold()
            and parsed.port == expected.port
        )
    except ValueError:
        return False
