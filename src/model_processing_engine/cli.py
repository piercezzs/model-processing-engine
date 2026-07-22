from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .contracts import ExecutionRequest
from .factory import build_default_engine
from .settings import load_settings
from .task_loader import load_task_pack


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "task" and args.task_command == "validate":
        task = load_task_pack(args.task_dir)
        _print_json(
            {
                "status": "valid",
                "namespace": task.namespace,
                "taskId": task.id,
                "version": task.version,
                "digest": task.digest,
                "componentHashes": task.component_hashes,
            }
        )
        return 0
    if args.command == "execute":
        task = load_task_pack(args.task_dir)
        payload = _read_json_object(Path(args.input))
        request = ExecutionRequest.model_validate(
            {
                "task": task.model_dump(by_alias=True),
                "input": payload,
                "runtime": {
                    "providerId": args.provider,
                    "model": args.model,
                    "forceRefresh": args.force_refresh,
                },
            }
        )
        result = build_default_engine(root=args.root).execute(request)
        _print_json(result.model_dump(by_alias=True))
        return 0 if result.status == "succeeded" else 1
    if args.command == "cache" and args.cache_command == "cleanup":
        engine = build_default_engine(root=args.root)
        _print_json({"removed": engine.store.cleanup_expired()})
        return 0
    if args.command == "serve":
        import uvicorn

        from .service import create_app

        settings = load_settings(args.root)
        uvicorn.run(
            create_app(settings=settings),
            host=settings.host,
            port=settings.port,
            reload=False,
        )
        return 0
    parser.error("Unknown command")
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mpe", description="Model Processing Engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    task_parser = subparsers.add_parser("task", help="Task-pack operations")
    task_subparsers = task_parser.add_subparsers(dest="task_command", required=True)
    validate = task_subparsers.add_parser("validate", help="Validate a task-pack directory")
    validate.add_argument("--task-dir", required=True)

    execute = subparsers.add_parser("execute", help="Execute a task pack with a JSON input file")
    execute.add_argument("--task-dir", required=True)
    execute.add_argument("--input", required=True)
    execute.add_argument("--provider")
    execute.add_argument("--model")
    execute.add_argument("--force-refresh", action="store_true")
    execute.add_argument("--root", default=".")

    cache_parser = subparsers.add_parser("cache", help="Cache maintenance")
    cache_subparsers = cache_parser.add_subparsers(dest="cache_command", required=True)
    cleanup = cache_subparsers.add_parser("cleanup", help="Remove expired entries")
    cleanup.add_argument("--root", default=".")

    serve = subparsers.add_parser("serve", help="Start the loopback HTTP service")
    serve.add_argument("--root", default=".")
    return parser


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
