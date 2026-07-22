from __future__ import annotations

import hmac
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response

from .contracts import ExecutionRequest
from .engine import ModelProcessingEngine
from .exceptions import ExecutionNotFoundError
from .factory import build_default_engine
from .settings import Settings, load_settings


def create_app(
    *,
    engine: ModelProcessingEngine | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    runtime = settings or load_settings()
    task_engine = engine or build_default_engine(settings=runtime, recover_incomplete=True)
    app = FastAPI(
        title="Model Processing Engine",
        version="0.1.0",
        description="Business-neutral runtime for externally defined model tasks.",
    )
    background_threads: set[threading.Thread] = set()
    background_lock = threading.Lock()

    @app.middleware("http")
    async def enforce_request_size(request: Request, call_next: Any) -> Response:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared = int(content_length)
            except ValueError:
                declared = runtime.max_request_bytes + 1
            if declared > runtime.max_request_bytes:
                return Response(status_code=413, content="Request body too large")
        return await call_next(request)

    @app.middleware("http")
    async def enforce_api_token(request: Request, call_next: Any) -> Response:
        if runtime.api_token and request.url.path != "/v1/health":
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {runtime.api_token}"
            if not hmac.compare_digest(supplied, expected):
                return Response(status_code=401, content="Unauthorized")
        return await call_next(request)

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "app": "model-processing-engine",
            "version": "0.1.0",
            "providers": task_engine.providers.ids(),
            "cacheEntries": task_engine.store.cache_count(),
        }

    @app.get("/v1/providers")
    def providers() -> dict[str, Any]:
        return {
            "providers": task_engine.providers.ids(),
            "defaultProviderId": task_engine.providers.default_provider_id,
        }

    @app.post("/v1/executions")
    def execute(request: ExecutionRequest) -> dict[str, Any]:
        if not request.async_mode:
            return task_engine.execute(request).model_dump(by_alias=True)
        reserved = task_engine.reserve(request)

        def run() -> None:
            try:
                task_engine.execute(request, execution_id=reserved.execution_id)
            finally:
                with background_lock:
                    background_threads.discard(threading.current_thread())

        thread = threading.Thread(
            target=run,
            name=f"mpe-{reserved.execution_id[:12]}",
            daemon=True,
        )
        with background_lock:
            background_threads.add(thread)
        thread.start()
        return reserved.model_dump(by_alias=True)

    @app.get("/v1/executions/{execution_id}")
    def execution(execution_id: str) -> dict[str, Any]:
        try:
            return task_engine.get_execution(execution_id).model_dump(by_alias=True)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/cache/cleanup")
    def cleanup_cache() -> dict[str, int]:
        return {"removed": task_engine.store.cleanup_expired()}

    app.state.engine = task_engine
    app.state.settings = runtime
    app.state.background_threads = background_threads
    return app
