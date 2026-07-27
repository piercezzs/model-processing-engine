from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .exceptions import ConfigurationError


PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_directory(path: str | Path) -> Path:
    target = Path(path)
    try:
        target.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(target, PRIVATE_DIRECTORY_MODE)
    except OSError as exc:
        raise ConfigurationError(f"Could not secure private directory: {target}") from exc
    return target


def ensure_private_file(path: str | Path) -> Path:
    target = Path(path)
    ensure_private_directory(target.parent)
    if target.is_symlink():
        raise ConfigurationError(f"Refusing to use a symbolic link as a private file: {target}")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, PRIVATE_FILE_MODE)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, PRIVATE_FILE_MODE)
        finally:
            os.close(descriptor)
        if os.name != "nt":
            os.chmod(target, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise ConfigurationError(f"Could not secure private file: {target}") from exc
    return target


def atomic_write_text(path: str | Path, content: str, *, mode: int = 0o600) -> None:
    target = Path(path)
    if target.is_symlink():
        raise ConfigurationError(f"Refusing to replace a symbolic link: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, mode)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
