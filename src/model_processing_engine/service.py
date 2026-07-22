from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse

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
from .exceptions import ConfigurationError, ExecutionNotFoundError, ProviderError
from .factory import build_default_engine
from .settings import Settings, load_settings


def create_app(
    *,
    engine: ModelProcessingEngine | None = None,
    settings: Settings | None = None,
    restart_scheduler: Callable[[Settings], None] | None = None,
) -> FastAPI:
    runtime = settings or load_settings()
    task_engine = engine or build_default_engine(settings=runtime, recover_incomplete=True)
    app = FastAPI(
        title="Model Processing Engine",
        version=VERSION,
        description="Business-neutral runtime for externally defined model tasks.",
    )
    background_threads: set[threading.Thread] = set()
    background_lock = threading.Lock()
    admin_manager = AdminConfigManager(runtime.project_dir) if runtime.project_dir else None
    admin_csrf_token = secrets.token_urlsafe(32)
    restart_service_later = restart_scheduler or schedule_managed_restart
    admin_assets = Path(__file__).with_name("admin_ui")

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
        }

    @app.get(f"/{API_VERSION}/providers")
    def providers() -> dict[str, Any]:
        return {
            "providers": task_engine.providers.ids(),
            "providerDetails": task_engine.providers.descriptors(),
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

    @app.post(f"/{API_VERSION}/admin/providers/test")
    def admin_test_provider(draft: ProviderDraft) -> dict[str, Any]:
        assert admin_manager is not None
        try:
            return admin_manager.test_provider(draft)
        except ConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

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

    @app.post(f"/{API_VERSION}/executions")
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
    app.state.background_threads = background_threads
    app.state.admin_manager = admin_manager
    return app


def _is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/") or path.startswith(f"/{API_VERSION}/admin/")


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
