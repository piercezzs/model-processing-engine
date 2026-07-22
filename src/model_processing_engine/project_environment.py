from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Mapping

from .exceptions import ConfigurationError
from .file_store import atomic_write_text


ENV_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
ASSIGNMENT_PATTERN = re.compile(
    r"^(?:export[ \t]+)?([A-Z_][A-Z0-9_]{0,127})[ \t]*=(.*)$"
)
PROJECT_NAME_PATTERN = re.compile(
    r"(?m)^name[ \t]*=[ \t]*[\"']model-processing-engine[\"'][ \t]*$"
)
MAX_ENV_BYTES = 128 * 1024


def discover_project_dir(*, explicit: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    selected = explicit or os.environ.get("MPE_PROJECT_DIR")
    if selected:
        candidates.append(Path(selected).expanduser())
    else:
        candidates.append(Path.cwd())
        executable = Path(sys.executable).resolve()
        if len(executable.parents) >= 3:
            candidates.append(executable.parents[2])
        candidates.extend(Path(__file__).resolve().parents)

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if _is_mpe_project(resolved):
            return resolved
    if selected:
        raise ConfigurationError(f"MPE_PROJECT_DIR is not an MPE project: {selected}")
    return None


def load_project_environment(project_dir: Path | None) -> dict[str, str]:
    if project_dir is None:
        return {}
    values = read_env_file(project_dir / ".env")
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def read_env_file(path: str | Path) -> dict[str, str]:
    target = Path(path)
    if not target.exists():
        return {}
    if target.is_symlink():
        raise ConfigurationError(f"Refusing to read a symbolic-link environment file: {target}")
    try:
        if target.stat().st_size > MAX_ENV_BYTES:
            raise ConfigurationError(f"Environment file is too large: {target}")
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(f"Could not read environment file: {target}") from exc
    return _parse_lines(lines, label=str(target))


def update_project_environment(project_dir: Path, updates: Mapping[str, str]) -> dict[str, str]:
    target = project_dir / ".env"
    for key, value in updates.items():
        if not ENV_KEY_PATTERN.fullmatch(key) or not key.startswith("MPE_"):
            raise ConfigurationError(f"Unsupported MPE environment key: {key}")
        if not isinstance(value, str) or len(value) > 16_384:
            raise ConfigurationError(f"Invalid environment value for {key}")

    existing_lines: list[str] = []
    if target.exists():
        if target.is_symlink():
            raise ConfigurationError(f"Refusing to replace a symbolic-link environment file: {target}")
        try:
            if target.stat().st_size > MAX_ENV_BYTES:
                raise ConfigurationError(f"Environment file is too large: {target}")
            existing_lines = target.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError(f"Could not read environment file: {target}") from exc
        _parse_lines(existing_lines, label=str(target))

    remaining = dict(updates)
    rendered: list[str] = []
    for line in existing_lines:
        match = ASSIGNMENT_PATTERN.match(line.strip())
        if match and match.group(1) in remaining:
            key = match.group(1)
            rendered.append(_assignment(key, remaining.pop(key)))
        else:
            rendered.append(line)
    if rendered and rendered[-1].strip():
        rendered.append("")
    rendered.extend(_assignment(key, value) for key, value in remaining.items())
    content = "\n".join(rendered).rstrip() + "\n"
    if len(content.encode("utf-8")) > MAX_ENV_BYTES:
        raise ConfigurationError("Updated MPE environment file would be too large")
    atomic_write_text(target, content, mode=0o600)
    return read_env_file(target)


def _parse_lines(lines: list[str], *, label: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ASSIGNMENT_PATTERN.match(line)
        if not match:
            raise ConfigurationError(f"Invalid environment assignment at {label}:{index}")
        key, raw_value = match.groups()
        if key in values:
            raise ConfigurationError(f"Duplicate environment key at {label}:{index}: {key}")
        values[key] = _decode_value(raw_value.strip(), label=label, line=index)
    return values


def _decode_value(raw_value: str, *, label: str, line: int) -> str:
    if not raw_value:
        return ""
    if raw_value.startswith('"'):
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"Invalid quoted value at {label}:{line}") from exc
        if not isinstance(value, str):
            raise ConfigurationError(f"Environment value must be a string at {label}:{line}")
        return value
    if raw_value.startswith("'"):
        if len(raw_value) < 2 or not raw_value.endswith("'"):
            raise ConfigurationError(f"Invalid quoted value at {label}:{line}")
        return raw_value[1:-1]
    return raw_value


def _assignment(key: str, value: str) -> str:
    return f"{key}={json.dumps(value, ensure_ascii=False)}"


def _is_mpe_project(path: Path) -> bool:
    manifest = path / "pyproject.toml"
    try:
        if not manifest.is_file() or manifest.stat().st_size > 128 * 1024:
            return False
        return bool(PROJECT_NAME_PATTERN.search(manifest.read_text(encoding="utf-8")))
    except (OSError, UnicodeError):
        return False
