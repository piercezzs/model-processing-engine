from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from .exceptions import ConfigurationError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PROVIDER_CONFIG = Path(__file__).with_name("default_providers.json")


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

    @property
    def database_path(self) -> Path:
        return self.data_dir / "runtime.sqlite"


def load_settings(root: str | Path | None = None) -> Settings:
    resolved_root = Path(root or PROJECT_ROOT).expanduser().resolve()
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
    )


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
