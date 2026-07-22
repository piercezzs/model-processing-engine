# Architecture

## Boundary

The engine answers one question: given an externally defined task and validated
input, execute the task through a configured model provider and return a
validated, reproducible result envelope.

The engine does not know about caller Jobs, modules, reports, Markdown libraries,
databases, or writeback rules.

```text
calling project
  -> project-owned input adapter
  -> TaskDefinition + input payload
  -> model-processing-engine
  -> ResultEnvelope
  -> project-owned review and persistence
```

## Task Ownership

Calling projects own `task.json`, prompts, schemas, taxonomies, semantic cache
fields, and business rules. The engine owns the manifest contract and safe loader.

An execution stores the task digest and component hashes. Those are immutable
runtime evidence, not editable canonical task definitions.

## Cache Ownership

The engine owns SQLite storage, transactions, TTL cleanup, single-flight
coordination, and technical cache identity. A caller task owns whether caching is
disabled, exact, or semantic and which business identity fields are meaningful.

Every final cache key includes the caller namespace, task digest, provider
identity, model, inference parameters, semantic cache version, and normalized
input identity. Namespaces never share entries.

Cached model output is disposable computation reuse. Durable business results
remain owned and stored by the caller.

Sensitive tasks are synchronous only. Their result is returned in memory to the
active caller and omitted from both cache entries and persisted execution
records.

Each data directory has one service-process owner. Service startup recovers
queued or running records left by the prior runtime. SDK and one-shot CLI engine
construction does not perform recovery, so it cannot rewrite an active service's
execution status.

## Security Boundary

The service is loopback-only by default. Non-loopback binding requires an
explicit remote opt-in and an API token supplied through the environment.
Provider secrets are also environment-only. Task schemas may use internal JSON
Schema fragments, but external references are rejected to prevent validation
from retrieving untrusted resources.

## Managed Service Lifecycle

`mpe start`, `stop`, `status`, and `restart` treat the runtime root as a service
identity. The default root is `~/.model-processing-engine`; explicit roots form
isolated runtime profiles. Data, logs, and process-control records have separate
subdirectories and environment overrides.

The manager serializes control operations with an exclusive short-lived lock.
The service record is written atomically with owner-only permissions. Startup
waits for health that matches the application ID, API version, runtime instance
digest, and spawned process ID. Stop sends signals only after the same checks;
foreign listeners and unverified live PIDs fail closed.

The foreground service also holds an operating-system file lock inside its data
directory for its full lifetime. This makes the one-service-per-data-directory
rule machine-enforced even when separate runtime roots or ports are configured.

The HTTP path version comes from one runtime constant. Provider capability and
configured-model declarations are exposed without credential environment names
or secret values.

## Local Administration Boundary

The loopback-only `/admin` surface manages deployment configuration, not caller
business tasks. The same-origin page sends strictly validated Provider drafts to
versioned admin routes. Mutations require a per-process CSRF token, a loopback
Host, and an exact Origin match. Admin routes are unavailable when remote mode is
enabled.

Provider tests use submitted credentials only in memory. A successful test
issues a short-lived token bound to the exact Provider/model-list/default-model/
key payload. Only a matching token can activate that payload. Provider presets
are non-secret UI defaults; protocol type remains the runtime boundary. Model
discovery uses the configured OpenAI-compatible `/models` path and submitted or
previously stored credential only in memory, with manual model entry as the
compatibility fallback. Activation atomically writes the project `.env` and
ignored `config/providers.local.json`, then starts a detached control helper
that uses the existing verified process manager to restart the service. The UI
polls health and obtains a new CSRF token after recovery.

Secrets remain plaintext by the explicitly selected project `.env` policy, but
the file is Git-ignored, mode `0600` on Unix, never returned over the API, and
never duplicated into automatic backups. Process environment variables remain
the higher-precedence deployment override.

## Complexity Gate

The service is L3: it exposes a local network interface, accepts external input,
performs persistent writes, and supports concurrent callers. Its mandatory
design pillars are module boundaries, security, schema validation, versioned API,
cache consistency, observability, dependency control, tests, and machine-checked
guards.

UI code splitting is not applicable because the admin page is a small,
dependency-free surface. Formal public release management is deferred until the
tool is distributed outside its owner-controlled environment. Cache backup is
not required because cache entries are disposable; callers own durable-result
backup. Project `.env` backup is intentionally not automated because it contains
replaceable credentials and the selected policy forbids extra secret copies.
