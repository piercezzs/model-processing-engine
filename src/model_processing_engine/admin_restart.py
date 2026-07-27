from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .file_store import ensure_private_directory, ensure_private_file
from .process_manager import restart_service
from .settings import Settings, load_settings


def schedule_managed_restart(settings: Settings) -> None:
    command = [
        sys.executable,
        "-m",
        "model_processing_engine.admin_restart",
        "--root",
        str(settings.root),
    ]
    environment = dict(os.environ)
    if settings.project_dir:
        environment["MPE_PROJECT_DIR"] = str(settings.project_dir)
    ensure_private_directory(settings.service_log_path.parent)
    ensure_private_file(settings.service_log_path)
    descriptor = os.open(
        settings.service_log_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    log_stream = os.fdopen(descriptor, "ab", buffering=0)
    common = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_stream,
        "stderr": subprocess.STDOUT,
        "cwd": settings.project_dir or settings.root,
        "env": environment,
        "close_fds": True,
    }
    try:
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess,
                "CREATE_NO_WINDOW",
                0,
            )
            subprocess.Popen(command, creationflags=flags, **common)
        else:
            subprocess.Popen(command, start_new_session=True, **common)
    finally:
        log_stream.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restart MPE after an admin configuration update")
    parser.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    settings = load_settings(Path(args.root))
    result = restart_service(settings)
    print(json.dumps({"adminRestart": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
