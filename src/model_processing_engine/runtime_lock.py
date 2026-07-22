from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from .exceptions import ServiceManagerError
from .settings import Settings


@contextmanager
def exclusive_runtime_lock(settings: Settings) -> Iterator[None]:
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = settings.data_dir / "service.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(descriptor, "r+b", buffering=0)
    locked = False
    try:
        _lock_stream(stream, path)
        locked = True
        yield
    finally:
        if locked:
            _unlock_stream(stream)
        stream.close()


def _lock_stream(stream: BinaryIO, path: Path) -> None:
    if os.name == "nt":
        import msvcrt

        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ServiceManagerError(
                f"Another MPE service owns the runtime data directory: {path.parent}"
            ) from exc
        return

    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise ServiceManagerError(
            f"Another MPE service owns the runtime data directory: {path.parent}"
        ) from exc


def _unlock_stream(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        try:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            return
        return

    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        return
