from __future__ import annotations

import hashlib
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from .exceptions import ConfigurationError
from .project_environment import discover_project_dir, load_project_environment


PACKAGE_PROVIDER_CONFIG = Path(__file__).with_name("default_providers.json")
DEFAULT_RUNTIME_DIRECTORY = ".model-processing-engine"


@dataclass(frozen=True)
class Settings:
    root: Path
    provider_config_path: Path
    data_dir: Path
    host: str
    port: int
    allow_remote: bool
    max_provider_concurrency: int
    max_request_bytes: int
    api_token: str = ""
    log_dir: Path | None = None
    run_dir: Path | None = None
    project_dir: Path | None = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "runtime.sqlite"

    @property
    def service_log_path(self) -> Path:
        return (self.log_dir or self.root / "logs") / "service.log"

    @property
    def service_record_path(self) -> Path:
        return (self.run_dir or self.root / "run") / "service.json"

    @property
    def service_lock_path(self) -> Path:
        return (self.run_dir or self.root / "run") / "manager.lock"

    @property
    def instance_id(self) -> str:
        source = str(self.root.resolve()).encode("utf-8")
        return hashlib.sha256(source).hexdigest()[:16]


def load_settings(root: str | Path | None = None) -> Settings:
    explicit_project_dir = os.environ.get("MPE_PROJECT_DIR")
    project_dir = discover_project_dir(
        explicit=explicit_project_dir,
    ) if explicit_project_dir or root is None else None
    load_project_environment(project_dir)
    resolved_root = _runtime_root(root)
    configured_provider_path = os.environ.get("MPE_PROVIDER_CONFIG")
    repository_provider_config = resolved_root / "config" / "providers.json"
    provider_config = (
        _resolved_path(configured_provider_path, root=resolved_root)
        if configured_provider_path
        else repository_provider_config.resolve()
        if repository_provider_config.is_file()
        else PACKAGE_PROVIDER_CONFIG.resolve()
    )
    data_dir = _resolved_path(
        os.environ.get("MPE_DATA_DIR", "data/runtime"),
        root=resolved_root,
    )
    log_dir = _resolved_path(
        os.environ.get("MPE_LOG_DIR", "logs"),
        root=resolved_root,
    )
    run_dir = _resolved_path(
        os.environ.get("MPE_RUN_DIR", "run"),
        root=resolved_root,
    )
    host = os.environ.get("MPE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    allow_remote = _environment_bool("MPE_ALLOW_REMOTE", default=False)
    if not allow_remote and not _is_loopback(host):
        raise ConfigurationError(
            "Remote binding is disabled; use a loopback host or explicitly set MPE_ALLOW_REMOTE=1"
        )
    api_token = os.environ.get("MPE_API_TOKEN", "").strip()
    if not _is_loopback(host) and not api_token:
        raise ConfigurationError("Remote binding requires MPE_API_TOKEN")
    port = _bounded_int("MPE_PORT", default=8787, minimum=1, maximum=65535)
    max_concurrency = _bounded_int(
        "MPE_MAX_PROVIDER_CONCURRENCY",
        default=8,
        minimum=1,
        maximum=64,
    )
    max_request_bytes = _bounded_int(
        "MPE_MAX_REQUEST_BYTES",
        default=2 * 1024 * 1024,
        minimum=1024,
        maximum=64 * 1024 * 1024,
    )
    return Settings(
        root=resolved_root,
        provider_config_path=provider_config,
        data_dir=data_dir,
        host=host,
        port=port,
        allow_remote=allow_remote,
        max_provider_concurrency=max_concurrency,
        max_request_bytes=max_request_bytes,
        api_token=api_token,
        log_dir=log_dir,
        run_dir=run_dir,
        project_dir=project_dir,
    )


def _runtime_root(root: str | Path | None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve()
    configured = os.environ.get("MPE_HOME", "").strip()
    base = (
        Path(configured).expanduser()
        if configured
        else Path.home() / DEFAULT_RUNTIME_DIRECTORY
    )
    return base.resolve()


def _resolved_path(value: str, *, root: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _environment_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _bounded_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
