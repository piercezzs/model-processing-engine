from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_TASK = PROJECT_ROOT / "examples" / "tasks" / "generic_summary"
ADMIN_SCRIPT_MARKERS = (
    "historyProviderCacheTokens",
    "historyTransportRetries",
    "execution-stats?",
    "renderModelBreakdown",
    "nativeJsonSchema",
)


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerificationStep:
    label: str
    command: tuple[str, ...]
    environment: Mapping[str, str]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic MPE project verification."
    )
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="Restart the managed loopback service and probe its API and admin assets.",
    )
    args = parser.parse_args(argv)

    try:
        with tempfile.TemporaryDirectory(prefix="mpe-verify-pycache-") as pycache_dir:
            deterministic_environment = _project_environment(
                {"PYTHONPYCACHEPREFIX": pycache_dir}
            )
            _run_steps(_deterministic_steps(deterministic_environment))
        if args.runtime:
            runtime_environment = _project_environment()
            _run_steps(_runtime_steps(runtime_environment))
            _verify_runtime_surface(runtime_environment)
    except VerificationError as exc:
        print(f"\n[verify] FAILED: {exc}", file=sys.stderr)
        return 1

    scope = "deterministic and runtime" if args.runtime else "deterministic"
    print(f"\n[verify] PASSED: {scope} checks completed.")
    return 0


def _deterministic_steps(
    environment: Mapping[str, str],
) -> tuple[VerificationStep, ...]:
    python = sys.executable
    node = _required_executable("node")
    git = _required_executable("git")
    return (
        VerificationStep(
            "Python unit tests",
            (python, "-m", "unittest", "discover", "-s", "tests", "-q"),
            environment,
        ),
        VerificationStep(
            "Python compile check",
            (python, "-m", "compileall", "-q", "src", "tests"),
            environment,
        ),
        VerificationStep(
            "Admin JavaScript syntax",
            (
                node,
                "--check",
                "src/model_processing_engine/admin_ui/app.js",
            ),
            environment,
        ),
        VerificationStep(
            "Git whitespace and conflict markers",
            (git, "diff", "--check"),
            environment,
        ),
        VerificationStep(
            "Example Task Pack contract",
            (
                python,
                "-m",
                "model_processing_engine.cli",
                "task",
                "validate",
                "--task-dir",
                str(EXAMPLE_TASK),
            ),
            environment,
        ),
    )


def _runtime_steps(
    environment: Mapping[str, str],
) -> tuple[VerificationStep, ...]:
    python = sys.executable
    cli = (python, "-m", "model_processing_engine.cli")
    return (
        VerificationStep(
            "Managed service restart",
            (*cli, "restart"),
            environment,
        ),
        VerificationStep(
            "Managed service status",
            (*cli, "status"),
            environment,
        ),
    )


def _run_steps(steps: Sequence[VerificationStep]) -> None:
    total = len(steps)
    for index, step in enumerate(steps, start=1):
        print(f"\n[verify] {index}/{total} {step.label}")
        print(f"$ {shlex.join(step.command)}")
        result = subprocess.run(
            step.command,
            cwd=PROJECT_ROOT,
            env=dict(step.environment),
            check=False,
        )
        if result.returncode:
            raise VerificationError(
                f"{step.label} exited with code {result.returncode}"
            )


def _verify_runtime_surface(environment: Mapping[str, str]) -> None:
    previous_project_dir = os.environ.get("MPE_PROJECT_DIR")
    os.environ["MPE_PROJECT_DIR"] = environment["MPE_PROJECT_DIR"]
    try:
        from model_processing_engine.settings import load_settings

        settings = load_settings()
    finally:
        if previous_project_dir is None:
            os.environ.pop("MPE_PROJECT_DIR", None)
        else:
            os.environ["MPE_PROJECT_DIR"] = previous_project_dir

    if not _is_loopback(settings.host):
        raise VerificationError(
            f"runtime verification requires a loopback host, got {settings.host!r}"
        )

    url_host = f"[{settings.host}]" if ":" in settings.host else settings.host
    base_url = f"http://{url_host}:{settings.port}"
    health, _headers = _read_json(f"{base_url}/v1/health")
    if health.get("status") != "ok" or health.get("app") != "model-processing-engine":
        raise VerificationError("health endpoint returned an unexpected service identity")
    async_queue = health.get("asyncQueue")
    if not isinstance(async_queue, dict):
        raise VerificationError("health endpoint omitted asyncQueue")
    for field in ("queued", "running", "total", "capacity", "workers"):
        if field not in async_queue:
            raise VerificationError(f"health endpoint omitted asyncQueue.{field}")

    history, _headers = _read_json(
        f"{base_url}/v1/admin/executions?limit=1"
    )
    summary = history.get("summary")
    if not isinstance(summary, dict):
        raise VerificationError("admin execution history omitted its summary")
    for field in (
        "cacheHits",
        "providerCacheHitExecutions",
        "providerCallCount",
        "transportRetries",
        "contractRepairs",
    ):
        if field not in summary:
            raise VerificationError(
                f"admin execution history omitted summary.{field}"
            )

    statistics, _headers = _read_json(
        f"{base_url}/v1/admin/execution-stats"
        f"?period=year&anchor={time.gmtime().tm_year}&timezone=UTC&kind=all"
    )
    if not isinstance(statistics.get("series"), list):
        raise VerificationError("admin execution statistics omitted series")
    if not isinstance(statistics.get("models"), list):
        raise VerificationError("admin execution statistics omitted models")
    if not isinstance(statistics.get("facets"), dict):
        raise VerificationError("admin execution statistics omitted facets")

    page, _headers = _read_text(f"{base_url}/admin")
    for marker in ('data-view="providers"', 'data-view="executions"'):
        if marker not in page:
            raise VerificationError(f"served admin page omitted {marker}")

    script, headers = _read_text(f"{base_url}/admin/app.js")
    for marker in ADMIN_SCRIPT_MARKERS:
        if marker not in script:
            raise VerificationError(f"served admin script omitted {marker}")
    if "no-store" not in headers.get("cache-control", ""):
        raise VerificationError("served admin script is missing no-store cache control")

    from model_processing_engine.task_loader import load_task_pack

    task = load_task_pack(EXAMPLE_TASK)
    input_payload = json.loads(
        (EXAMPLE_TASK / "input.example.json").read_text(encoding="utf-8")
    )
    queued, _headers = _post_json(
        f"{base_url}/v1/executions",
        {
            "task": task.model_dump(by_alias=True),
            "input": input_payload,
            "runtime": {
                "providerId": "mock",
                "model": "schema-sample-v1",
                "forceRefresh": True,
            },
            "asyncMode": True,
        },
        token=settings.api_token,
    )
    execution_id = str(queued.get("executionId") or "")
    if queued.get("status") != "queued" or not execution_id:
        raise VerificationError("async execution endpoint did not return a queued envelope")
    deadline = time.monotonic() + 5
    record: dict[str, object] = queued
    while time.monotonic() < deadline:
        record, _headers = _read_json(
            f"{base_url}/v1/executions/{execution_id}",
            token=settings.api_token,
        )
        if record.get("status") in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    if record.get("status") != "succeeded":
        raise VerificationError(
            f"async execution did not succeed, got {record.get('status')!r}"
        )

    print(
        "[verify] Runtime surface passed: "
        f"{base_url}, PID {health.get('processId', 'unknown')}, "
        f"async execution {execution_id}."
    )


def _read_json(
    url: str,
    *,
    token: str = "",
) -> tuple[dict[str, object], Mapping[str, str]]:
    text, headers = _read_text(url, token=token)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"{url} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"{url} did not return a JSON object")
    return value, headers


def _read_text(
    url: str,
    *,
    token: str = "",
) -> tuple[str, Mapping[str, str]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"Accept": "application/json, text/javascript"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )
    return _open_text(opener, request, url=url)


def _post_json(
    url: str,
    payload: Mapping[str, object],
    *,
    token: str = "",
) -> tuple[dict[str, object], Mapping[str, str]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    text, response_headers = _open_text(opener, request, url=url)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"{url} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"{url} did not return a JSON object")
    return value, response_headers


def _open_text(
    opener: urllib.request.OpenerDirector,
    request: urllib.request.Request,
    *,
    url: str,
) -> tuple[str, Mapping[str, str]]:
    try:
        with opener.open(request, timeout=5) as response:
            return (
                response.read().decode("utf-8"),
                {key.casefold(): value for key, value in response.headers.items()},
            )
    except OSError as exc:
        raise VerificationError(f"could not read {url}: {exc.__class__.__name__}") from exc


def _project_environment(
    additions: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment["MPE_PROJECT_DIR"] = str(PROJECT_ROOT)
    if additions:
        environment.update(additions)
    return environment


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise VerificationError(f"required executable is unavailable: {name}")
    return executable


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
