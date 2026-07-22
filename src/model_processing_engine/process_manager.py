from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator
from uuid import uuid4

from .constants import API_VERSION, APP_ID
from .exceptions import ServiceManagerError
from .settings import Settings


DEFAULT_CONTROL_TIMEOUT_SECONDS = 15.0
LOCK_STALE_SECONDS = 60.0


@dataclass(frozen=True)
class ServiceRecord:
    pid: int
    instance_id: str
    root: str
    host: str
    port: int
    started_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "instanceId": self.instance_id,
            "root": self.root,
            "host": self.host,
            "port": self.port,
            "startedAt": self.started_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "ServiceRecord":
        try:
            pid = int(value["pid"])
            port = int(value["port"])
            instance_id = str(value["instanceId"])
            root = str(value["root"])
            host = str(value["host"])
            started_at = str(value["startedAt"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceManagerError("Service record is malformed") from exc
        if pid <= 0 or not 1 <= port <= 65535 or not instance_id or not root or not host:
            raise ServiceManagerError("Service record contains invalid values")
        return cls(
            pid=pid,
            instance_id=instance_id,
            root=root,
            host=host,
            port=port,
            started_at=started_at,
        )


@dataclass(frozen=True)
class HealthProbe:
    reachable: bool
    payload: dict[str, object] | None = None
    error: str | None = None


def service_status(settings: Settings) -> dict[str, object]:
    record = _read_service_record(settings.service_record_path)
    probe = _probe_health(settings)
    base = _status_base(settings)
    if record is None:
        if _health_matches(settings, probe.payload):
            return {
                **base,
                "status": "running_unmanaged",
                "managed": False,
                "pid": _health_pid(probe.payload),
                "health": probe.payload,
                "detail": "A matching service is running without this runtime's service record",
            }
        if probe.reachable or _port_is_open(settings):
            return {
                **base,
                "status": "conflict",
                "managed": False,
                "detail": "The configured port is occupied by another or invalid service",
            }
        return {**base, "status": "stopped", "managed": False}

    if not _record_matches(settings, record):
        return {
            **base,
            "status": "conflict",
            "managed": True,
            "pid": record.pid,
            "detail": "The service record belongs to a different runtime configuration",
        }

    process_alive = _process_exists(record.pid)
    if process_alive and _health_matches(settings, probe.payload, expected_pid=record.pid):
        return {
            **base,
            "status": "running",
            "managed": True,
            "pid": record.pid,
            "startedAt": record.started_at,
            "health": probe.payload,
        }
    if not process_alive:
        if _health_matches(settings, probe.payload):
            return {
                **base,
                "status": "conflict",
                "managed": True,
                "pid": record.pid,
                "detail": "A matching service is running with a different process ID",
            }
        return {
            **base,
            "status": "stale",
            "managed": True,
            "pid": record.pid,
            "detail": "The recorded service process no longer exists",
        }
    return {
        **base,
        "status": "unresponsive",
        "managed": True,
        "pid": record.pid,
        "detail": probe.error or "The recorded process is alive but health verification failed",
    }


def start_service(
    settings: Settings,
    *,
    timeout_seconds: float = DEFAULT_CONTROL_TIMEOUT_SECONDS,
) -> dict[str, object]:
    _validate_timeout(timeout_seconds)
    with _control_lock(settings):
        return _start_locked(settings, timeout_seconds=timeout_seconds)


def stop_service(
    settings: Settings,
    *,
    timeout_seconds: float = DEFAULT_CONTROL_TIMEOUT_SECONDS,
) -> dict[str, object]:
    _validate_timeout(timeout_seconds)
    with _control_lock(settings):
        return _stop_locked(settings, timeout_seconds=timeout_seconds)


def restart_service(
    settings: Settings,
    *,
    timeout_seconds: float = DEFAULT_CONTROL_TIMEOUT_SECONDS,
) -> dict[str, object]:
    _validate_timeout(timeout_seconds)
    with _control_lock(settings):
        stopped = _stop_locked(settings, timeout_seconds=timeout_seconds)
        started = _start_locked(settings, timeout_seconds=timeout_seconds)
        return {**started, "restart": {"previousStatus": stopped["status"]}}


def _start_locked(settings: Settings, *, timeout_seconds: float) -> dict[str, object]:
    current = service_status(settings)
    status = str(current["status"])
    if status == "running":
        return {**current, "alreadyRunning": True}
    if status == "stale":
        _remove_service_record(settings.service_record_path)
    elif status != "stopped":
        raise ServiceManagerError(str(current.get("detail") or f"Cannot start from {status}"))

    _prepare_runtime_directories(settings)
    log_stream = _open_service_log(settings.service_log_path)
    try:
        process = _spawn_service(settings, log_stream)
    finally:
        log_stream.close()
    record = ServiceRecord(
        pid=process.pid,
        instance_id=settings.instance_id,
        root=str(settings.root),
        host=settings.host,
        port=settings.port,
        started_at=_utc_now(),
    )
    try:
        _write_service_record(settings.service_record_path, record)
    except OSError as exc:
        _terminate_spawned_process(process)
        raise ServiceManagerError(
            f"Could not persist service record: {settings.service_record_path}"
        ) from exc
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            _remove_service_record(settings.service_record_path)
            raise ServiceManagerError(
                f"Service exited during startup with code {exit_code}; "
                f"see {settings.service_log_path}"
            )
        probe = _probe_health(settings)
        if _health_matches(settings, probe.payload, expected_pid=process.pid):
            return service_status(settings)
        time.sleep(0.1)

    _terminate_spawned_process(process)
    _remove_service_record(settings.service_record_path)
    raise ServiceManagerError(
        f"Service did not become healthy within {timeout_seconds:g} seconds; "
        f"see {settings.service_log_path}"
    )


def _stop_locked(settings: Settings, *, timeout_seconds: float) -> dict[str, object]:
    current = service_status(settings)
    status = str(current["status"])
    if status == "stopped":
        return current
    if status == "stale":
        _remove_service_record(settings.service_record_path)
        return {
            **_status_base(settings),
            "status": "stopped",
            "managed": False,
            "staleCleaned": True,
        }
    if status != "running":
        raise ServiceManagerError(str(current.get("detail") or f"Cannot stop from {status}"))

    pid = int(current["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_service_record(settings.service_record_path)
        return {**_status_base(settings), "status": "stopped", "managed": False}
    except PermissionError as exc:
        raise ServiceManagerError(f"Permission denied while stopping process {pid}") from exc

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and _process_exists(pid):
        time.sleep(0.1)
    if _process_exists(pid):
        _kill_verified_process(pid)
        force_deadline = time.monotonic() + min(2.0, timeout_seconds)
        while time.monotonic() < force_deadline and _process_exists(pid):
            time.sleep(0.1)
    if _process_exists(pid):
        raise ServiceManagerError(f"Verified service process {pid} did not stop")
    _remove_service_record(settings.service_record_path)
    return {**_status_base(settings), "status": "stopped", "managed": False, "pid": pid}


def _status_base(settings: Settings) -> dict[str, object]:
    return {
        "app": APP_ID,
        "instanceId": settings.instance_id,
        "url": _service_url(settings),
        "root": str(settings.root),
        "dataDir": str(settings.data_dir),
        "logPath": str(settings.service_log_path),
        "recordPath": str(settings.service_record_path),
    }


def _probe_health(settings: Settings, *, timeout_seconds: float = 0.5) -> HealthProbe:
    headers = {"Accept": "application/json"}
    if settings.api_token:
        headers["Authorization"] = f"Bearer {settings.api_token}"
    request = urllib.request.Request(
        f"{_service_url(settings)}/{API_VERSION}/health",
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        finally:
            exc.close()
        return HealthProbe(reachable=True, error=f"Health endpoint returned HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return HealthProbe(reachable=False, error=exc.__class__.__name__)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return HealthProbe(reachable=True, error="Health endpoint returned invalid JSON")
    if not isinstance(parsed, dict):
        return HealthProbe(reachable=True, error="Health endpoint did not return an object")
    return HealthProbe(reachable=True, payload={str(key): value for key, value in parsed.items()})


def _health_matches(
    settings: Settings,
    payload: dict[str, object] | None,
    *,
    expected_pid: int | None = None,
) -> bool:
    if not payload:
        return False
    if payload.get("status") != "ok" or payload.get("app") != APP_ID:
        return False
    if payload.get("apiVersion") != API_VERSION:
        return False
    if payload.get("instanceId") != settings.instance_id:
        return False
    return expected_pid is None or _health_pid(payload) == expected_pid


def _health_pid(payload: dict[str, object] | None) -> int | None:
    if not payload:
        return None
    try:
        pid = int(payload.get("processId", 0))
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _record_matches(settings: Settings, record: ServiceRecord) -> bool:
    return (
        record.instance_id == settings.instance_id
        and Path(record.root).resolve() == settings.root.resolve()
        and record.host == settings.host
        and record.port == settings.port
    )


def _port_is_open(settings: Settings) -> bool:
    try:
        with socket.create_connection((_connect_host(settings.host), settings.port), timeout=0.25):
            return True
    except OSError:
        return False


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _spawn_service(settings: Settings, log_stream: BinaryIO) -> subprocess.Popen[bytes]:
    command = [
        sys.executable,
        "-m",
        "model_processing_engine.cli",
        "serve",
        "--root",
        str(settings.root),
    ]
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    common = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_stream,
        "stderr": subprocess.STDOUT,
        "cwd": settings.root,
        "env": environment,
        "close_fds": True,
    }
    if os.name == "nt":
        return subprocess.Popen(
            command,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            **common,
        )
    return subprocess.Popen(command, start_new_session=True, **common)


def _terminate_spawned_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _kill_verified_process(pid: int) -> None:
    force_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        os.kill(pid, force_signal)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise ServiceManagerError(f"Permission denied while force-stopping process {pid}") from exc


def _prepare_runtime_directories(settings: Settings) -> None:
    for path in (
        settings.root,
        settings.data_dir,
        settings.service_log_path.parent,
        settings.service_record_path.parent,
    ):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _open_service_log(path: Path) -> BinaryIO:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(descriptor, "ab", buffering=0)


def _read_service_record(path: Path) -> ServiceRecord | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceManagerError(f"Cannot read service record: {path}") from exc
    if not isinstance(value, dict):
        raise ServiceManagerError(f"Service record is not a JSON object: {path}")
    return ServiceRecord.from_dict({str(key): item for key, item in value.items()})


def _write_service_record(path: Path, record: ServiceRecord) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_json_write(path, record.as_dict())


def _remove_service_record(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


@contextmanager
def _control_lock(settings: Settings) -> Iterator[None]:
    path = settings.service_lock_path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = uuid4().hex
    payload: dict[str, object] = {
        "pid": os.getpid(),
        "token": token,
        "createdAt": _utc_now(),
    }
    for attempt in range(2):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            if attempt == 0 and _stale_control_lock(path):
                path.unlink(missing_ok=True)
                continue
            raise ServiceManagerError(
                f"Another service control operation is active: {path}"
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        break
    else:
        raise ServiceManagerError(f"Could not acquire service control lock: {path}")
    try:
        yield
    finally:
        if _lock_has_token(path, token):
            path.unlink(missing_ok=True)


def _stale_control_lock(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        pid = int(value.get("pid", 0)) if isinstance(value, dict) else 0
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        try:
            return time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS
        except OSError:
            return False
    if pid > 0:
        return not _process_exists(pid)
    try:
        return time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS
    except OSError:
        return False


def _lock_has_token(path: Path, token: str) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("token") == token


def _atomic_json_write(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _service_url(settings: Settings) -> str:
    host = _connect_host(settings.host)
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{display_host}:{settings.port}"


def _connect_host(host: str) -> str:
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def _validate_timeout(value: float) -> None:
    if not 0.5 <= value <= 120:
        raise ServiceManagerError("Service control timeout must be between 0.5 and 120 seconds")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
