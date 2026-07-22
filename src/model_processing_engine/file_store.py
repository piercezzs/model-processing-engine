from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .exceptions import ConfigurationError


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
