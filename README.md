# Model Processing Engine

Model Processing Engine (MPE) is a business-neutral runtime for structured model
tasks. A calling project owns the prompt, schemas, taxonomy, cache semantics, and
business persistence. MPE owns provider calls, retries, validation, concurrency,
progress, usage reporting, execution records, and disposable result caching.

It can be used as a Python library, a CLI, or a versioned local HTTP service.

## Boundary

```text
calling project
  -> project-owned adapter and Task Pack
  -> MPE execution contract
  -> configured model provider
  -> validated ResultEnvelope
  -> project-owned review and durable persistence
```

MPE does not contain caller Jobs, media types, report formats, writeback rules,
or domain prompts. A book parser, financial classifier, or future media task is
defined by its calling project and submitted through the same contract.

## Requirements and installation

- Python 3.10 or newer
- A provider credential in a local environment variable when using a real model

For development:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

The bundled default provider is deterministic `mock`; it makes the repository
runnable without network access or credentials. Copy `config/providers.json` to
your deployment configuration and set `MPE_PROVIDER_CONFIG` before real calls.

`MPE_HOME` defaults to `~/.model-processing-engine`. Managed-service data, logs,
and process records live below that stable root unless their individual paths
are overridden. An explicit `--root` selects a separate runtime profile.

## Task Pack

A Task Pack is owned and versioned by the calling project:

```text
tasks/example/
  task.json
  prompt.md
  input.schema.json
  output.schema.json
  taxonomy.json       # optional
```

`task.json` contains only declarative execution metadata:

```json
{
  "schemaVersion": 1,
  "namespace": "my-project",
  "id": "structured_summary",
  "version": "1",
  "title": "Structured Summary",
  "promptPath": "prompt.md",
  "inputSchemaPath": "input.schema.json",
  "outputSchemaPath": "output.schema.json",
  "cachePolicy": {
    "mode": "exact",
    "semanticVersion": "summary-v1",
    "ttlSeconds": 86400,
    "sensitive": false
  },
  "runtimeDefaults": {
    "providerId": "openai-compatible",
    "model": "your-model-id",
    "temperature": 0.1
  }
}
```

Supported cache modes are:

- `disabled`: no reusable result is stored. Required when `sensitive` is true.
- `exact`: the complete validated input participates in the cache key.
- `semantic`: only caller-declared `identityFields` participate in input
  identity. Every declared field must exist at execution time.

All modes also include namespace, complete Task Pack digest, provider identity,
model, inference parameters, and semantic version. Cached output is disposable;
the caller remains responsible for authoritative business data.

Sensitive tasks must execute synchronously. Their result is returned to that
caller but omitted from persistent execution records as well as the result
cache.

Task files cannot escape their directory. Input and output schemas support local
JSON Schema fragments such as `#/$defs/Item`; external `$ref` values are rejected
so validation cannot fetch untrusted resources.

## CLI

Validate the included neutral example:

```bash
.venv/bin/mpe task validate \
  --task-dir examples/tasks/generic_summary
```

Execute it with the offline mock provider:

```bash
.venv/bin/mpe execute \
  --root . \
  --task-dir examples/tasks/generic_summary \
  --input examples/tasks/generic_summary/input.example.json
```

Remove expired cache entries:

```bash
.venv/bin/mpe cache cleanup --root .
```

## Python SDK

```python
import json
from pathlib import Path

from model_processing_engine import ExecutionRequest, build_default_engine, load_task_pack

root = Path("/path/to/calling-project")
task = load_task_pack(root / "tasks" / "structured_summary")
payload = json.loads((root / "input.json").read_text(encoding="utf-8"))

request = ExecutionRequest.model_validate(
    {
        "task": task.model_dump(by_alias=True),
        "input": payload,
        "runtime": {"forceRefresh": False},
    }
)
result = build_default_engine(root=root).execute(request)
```

The caller should persist `result.result` only after checking
`result.status == "succeeded"` and applying its own review rules.

## HTTP service

Start a managed background service using the stable default runtime root:

```bash
.venv/bin/mpe start
.venv/bin/mpe status
.venv/bin/mpe restart
.venv/bin/mpe stop
```

Use an explicit runtime profile when isolation is required:

```bash
.venv/bin/mpe start --root /path/to/runtime-profile
```

For foreground development and diagnostics:

```bash
.venv/bin/mpe serve --root .
```

Managed startup is idempotent. It writes a restricted service record and log,
waits for verified health, and rejects a conflicting listener. Stop verifies the
application ID, runtime instance ID, and process ID before signaling a process;
an unverified live PID is never terminated.

`status` reports `running`, `stopped`, `stale`, `unresponsive`, `conflict`, or
`running_unmanaged`. A foreground `serve` process is intentionally unmanaged and
must be stopped by its owning terminal.

Available routes:

- `GET /v1/health`
- `GET /v1/providers`
- `POST /v1/executions`
- `GET /v1/executions/{execution_id}`
- `POST /v1/cache/cleanup`

HTTP requests contain the resolved `TaskDefinition` and input data, never a
server-side task directory path. This keeps filesystem ownership with the
calling project. Set `asyncMode` to true to receive a queued envelope and poll
the execution route.

The default host is `127.0.0.1`. Non-loopback binding requires both
`MPE_ALLOW_REMOTE=1` and `MPE_API_TOKEN`; authenticated routes then require
`Authorization: Bearer <token>`. Health remains public only for loopback
deployments. Use a reverse proxy with TLS if the service crosses a trusted
machine boundary.

Use one service process per `MPE_DATA_DIR`. Service startup marks queued or
running records from the previous runtime as interrupted. Separate callers may
share a service, but separate service processes should use separate data paths.

Health includes the application version, API version, stable runtime instance
ID, and process ID so local service management can verify process identity.

## Provider configuration

`config/providers.json` shows the supported provider types:

- `mock`: deterministic schema-derived output for tests and integration setup.
- `openai_compatible`: JSON chat-completions transport with bounded retry for
  rate-limit, server, timeout, and connection failures.

Provider configuration may declare `availableModels` and `capabilities`. The
`/v1/providers` response exposes those non-secret declarations while preserving
the original provider-ID list for compatibility.

Credentials are referenced by environment-variable name in provider config.
They must never be placed in Task Packs, request payloads, or committed files.
Set a non-secret `cacheIdentity` per account/endpoint context and increment it
when that context changes; the value participates in technical cache identity.

## Verification

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests
.venv/bin/mpe task validate --task-dir examples/tasks/generic_summary
.venv/bin/mpe status --root /tmp/mpe-verification-profile
```

The automated suite uses only the mock provider or mocked transports. A real
provider acceptance call is intentionally separate because it requires a local
credential and may incur cost.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for ownership and design
decisions.
