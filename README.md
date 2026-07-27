# Model Processing Engine

English | [简体中文](README.zh-CN.md)

> [!IMPORTANT]
> **Source-available, not open source.** Copyright (c) 2026 piercezzs. You may
> download and run an unmodified copy locally for personal or internal use. You
> may not modify, redistribute, republish, host for third parties, or release
> derivative versions. See [LICENSE](LICENSE) for the complete terms.
>
> **Release status:** `0.1.0` is experimental and local-first. Public contracts
> and operational behavior may change before a stable release. Remote,
> production use has not been certified.

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

`uv.lock` records the cross-platform dependency resolution reviewed by CI. When
`uv` is available, reproduce that environment with:

```bash
uv sync --locked --extra dev
```

For one-click local setup and managed service startup, use the platform wrapper:

```bash
# macOS: Terminal or Finder double-click
chmod +x start_mpe.command stop_mpe.command
./start_mpe.command

# Windows: Command Prompt or Explorer double-click
start_mpe.bat
```

The start wrapper finds Python 3.10 or newer, creates or repairs `.venv`, installs
runtime dependencies from `pyproject.toml`, and records a dependency fingerprint
so unchanged environments can be reused without reinstalling. If an existing
`.venv` can already import MPE and its runtime dependencies and passes
`pip check`, but only the fingerprint is missing, the wrapper adopts that
environment without an unnecessary networked install. It delegates
service identity and port-conflict handling to MPE's verified process manager
and never terminates a process merely because port `8787` is occupied.

Run the non-starting readiness check after setup with
`./start_mpe.command --check-only` on macOS or `start_mpe.bat --check-only` on
Windows. Stop the verified managed service with `./stop_mpe.command` or
`stop_mpe.bat`.

Normal one-click startup opens the loopback management page at `/admin` after
verified health. Use `--no-open` to start without opening a browser. The
management page is disabled for remote bindings.

## Local model management

The management page is the normal local entry for Provider, model, and API-key
configuration:

```text
http://127.0.0.1:8787/admin
```

The page separates Provider configuration and redacted execution history into
top-level tabs. Execution history supports day, month, and year periods in the
browser's IANA timezone, plus formal-task/connection-test, Provider, and model
filters. Its summary and per-model breakdown distinguish execution count,
actual Provider call count, Token usage, transport retries, contract repairs,
MPE result-cache hits, and Provider prompt-cache usage. These are separate
signals: an MPE result-cache hit skips the Provider call, while a Provider
prompt-cache hit reuses input-prefix Token but still generates a new result.
History rows never include model result bodies.
Provider connection tests are recorded in the same technical ledger, including
usage when the Provider returns it. Provider-call rows are captured before
output-schema validation, so consumed Token remains accounted for even when
returned JSON is rejected by the task contract.

Choose a known service preset such as OpenAI or DeepSeek, or use the custom
OpenAI-compatible option. Presets fill the protocol URL and endpoint paths but
remain editable. After an API key is available, **Detect and fetch models**
requests the Provider's configured `/models` endpoint and populates the default
model picker. Providers without a compatible model-list endpoint can still use
a manually entered model ID.

One Provider configuration can retain multiple discovered model IDs and one
default execution model. Calling projects may still override that model in an
execution request. Chat-completions protocol, request/list paths, timeout, and
transport retry count live under advanced connection settings. The same section
also sets the Provider's maximum concurrent in-flight requests. Actual
concurrency is demand-driven: one pending execution uses one slot, while larger
workloads queue after the configured Provider limit.

The advanced **Provider native JSON Schema** switch is an explicit capability
declaration, not automatic detection. Enable it only when the selected platform
and model support Chat Completions `response_format.type=json_schema`. MPE still
validates every returned object locally.

MPE first tests the exact submitted Provider, model list, default model, and key
in memory. Only a matching, unexpired successful test can be saved and
activated. Model discovery and failed connection tests do not modify project
files.

Successful activation writes user-managed state inside this project checkout:

- `.env`: active Provider/model, absolute local Provider-config path, and API
  key. This file is plaintext, mode `0600` on macOS, and ignored by Git.
- `config/providers.local.json`: non-secret Provider metadata. This file is also
  ignored by Git.

Both files are written with atomic replacement. Existing keys are never
returned by the API or management page; the UI shows only whether a credential
is configured. After saving, MPE schedules a verified managed restart and the
page waits for the new service health response. A failed connection test does
not modify either file.

The project `.env` is loaded automatically when MPE starts through the platform
wrapper. Existing process environment variables take precedence, which keeps
headless and CI deployment overrides available. Do not commit, copy, or share
the project `.env` file.

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
    "temperature": 0.1,
    "contractRetries": 1
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

`runtime.forceRefresh` bypasses cache reads. After a successful provider call and
output validation, the engine replaces the matching reusable cache entry. A
failed refresh leaves the previous cache entry unchanged.

`runtimeDefaults.contractRetries` controls bounded output-contract repair
requests and defaults to `1`; an execution may override it with
`runtime.contractRetries` from `0` through `2`. Transport retries and contract
repairs are separate counters. A repair reuses the original task/input prefix,
adds the rejected object plus a bounded validation diagnostic, and is recorded
as another real Provider call.

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

On POSIX systems, every SDK, CLI, foreground-service, and managed-service entry
secures the selected runtime data directory as `0700` and the SQLite database as
`0600`. Existing wider permissions are repaired before the database is opened.
SQLite WAL and shared-memory sidecars inherit the private database mode.

`status` reports `running`, `stopped`, `stale`, `unresponsive`, `conflict`, or
`running_unmanaged`. A foreground `serve` process is intentionally unmanaged and
must be stopped by its owning terminal.

Available routes:

- `GET /v1/health`
- `GET /v1/providers`
- `POST /v1/executions`
- `GET /v1/executions/{execution_id}`
- `POST /v1/cache/cleanup`
- `GET /admin` (loopback management page)
- `GET /v1/admin/config` (masked local configuration)
- `GET /v1/admin/executions` (loopback-only redacted history)
- `GET /v1/admin/execution-stats` (time-bounded aggregates and model breakdown)
- `POST /v1/admin/providers/test` (same-origin and CSRF protected)
- `POST /v1/admin/providers/models` (in-memory Provider model discovery)
- `POST /v1/admin/providers/apply` (same-origin and CSRF protected)

HTTP requests contain the resolved `TaskDefinition` and input data, never a
server-side task directory path. This keeps filesystem ownership with the
calling project. Set `asyncMode` to true to receive a queued envelope and poll
the execution route.

The default host is `127.0.0.1`. Non-loopback binding requires both
`MPE_ALLOW_REMOTE=1` and `MPE_API_TOKEN`; authenticated routes then require
`Authorization: Bearer <token>`. Health remains public only for loopback
deployments. Use a reverse proxy with TLS if the service crosses a trusted
machine boundary.

`MPE_MAX_REQUEST_BYTES` defaults to 2 MiB. MPE rejects oversized or malformed
declared lengths before routing and also counts the actual body bytes before
FastAPI parses the request, so omitted `Content-Length` and chunked transfer do
not bypass the limit.

Use one service process per `MPE_DATA_DIR`. Asynchronous requests are written to
the same SQLite database before the API returns. A fixed worker pool claims work
in FIFO order; a restart requeues work that was running and continues queued
requests. Legacy or incomplete records that have no durable request payload are
marked interrupted instead of being guessed. `MPE_ASYNC_WORKERS` defaults to
`4`, and `MPE_ASYNC_QUEUE_CAPACITY` defaults to `100`; a full queue returns HTTP
429. Queue capacity includes both queued and running requests.

Sensitive tasks remain synchronous because a recoverable queue must persist the
request until it reaches a terminal state. Completed queue rows are deleted.
Recovery is at-least-once for a request interrupted before its terminal
execution snapshot is committed; already-terminal snapshots are not rerun.
Separate trusted callers may share a service. MPE has one service-wide bearer
token and does not provide caller accounts, per-caller authorization, or quotas;
mutually untrusted callers must use isolated service instances, credentials, and
data paths.

Health includes the application version, API version, stable runtime instance
ID, and process ID so local service management can verify process identity.

## Provider configuration

`config/providers.json` shows the supported provider types:

- `mock`: deterministic schema-derived output for tests and integration setup.
- `openai_compatible`: JSON chat-completions transport with bounded retry for
  rate-limit, server, timeout, and connection failures.

OpenAI-compatible response and error bodies are read with an 8 MiB hard limit.
This bound applies to both model execution and model discovery before JSON
decoding, preventing a misconfigured or hostile endpoint from exhausting memory
with an unbounded response.

Provider configuration may declare `availableModels`, `capabilities`, and
`maxConcurrency`. The `/v1/providers` response exposes those non-secret
declarations while preserving the original provider-ID list for compatibility.
`structured_json` uses JSON-object mode. `native_json_schema` sends the task
output schema through the Provider's strict JSON Schema request field and must
only be declared for known-compatible platform/model combinations.
`maxConcurrency` is enforced independently for each Provider across all callers.
The environment-level `MPE_MAX_PROVIDER_CONCURRENCY` remains a service-wide
safety ceiling; the effective Provider limit is the smaller of the two.

Credentials are referenced by environment-variable name in provider config.
They must never be placed in Task Packs, request payloads, or committed files.
Set a non-secret `cacheIdentity` per account/endpoint context and increment it
when that context changes; the value participates in technical cache identity.

## Security

Report undisclosed vulnerabilities through the private process described in
[SECURITY.md](SECURITY.md). Never include live credentials, real task payloads,
or cached model results in a report.

## Verification

Run the complete deterministic project verification:

```bash
# macOS / Linux
.venv/bin/python scripts/verify_project.py

# Windows
.venv\Scripts\python scripts\verify_project.py
```

`uv`, Node.js, and Git must be available on `PATH`, and the project dev
dependencies must be installed. This verifies that `uv.lock` is current, parses
all `.github` YAML files, and runs the full unit suite, Python compilation,
admin JavaScript syntax, `git diff --check`, and the neutral example Task Pack
validation. It does not restart or mutate the running service.

Add `--runtime` only when a managed-service restart is intended:

```bash
.venv/bin/python scripts/verify_project.py --runtime
```

Runtime mode uses the verified process manager to restart MPE, checks service
identity and status, probes the redacted history contract, and confirms that
the current admin script is served with cache-safe headers. It also submits one
offline mock request through the persistent asynchronous queue and polls it to a
successful terminal state. Browser-based visual verification remains a separate
manual or agent-assisted check.

The automated suite uses only the mock provider or mocked transports. A real
provider acceptance call is intentionally separate because it requires a local
credential and may incur cost.

## License

MPE is proprietary source-available software, not open source. Unmodified local
use is permitted for personal or internal purposes; modification,
redistribution, republication, third-party hosting, and derivative releases are
not permitted without prior written authorization from piercezzs. See
[LICENSE](LICENSE).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for ownership and design
decisions.
