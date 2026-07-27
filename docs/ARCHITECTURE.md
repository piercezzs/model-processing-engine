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

The store owns its filesystem privacy invariant. Before SQLite is opened, every
entry path secures the selected data directory and database file; POSIX
deployments use `0700` for the directory and `0600` for the database and
sidecars. Keeping this invariant in the storage layer prevents SDK and one-shot
CLI construction from bypassing service-manager preparation.

Provider prompt caching is a separate upstream optimization. The engine places
the stable task output schema and optional taxonomy before caller-specific input
in the serialized Provider payload so prefix-based caches can reuse the largest
safe task-owned prefix. OpenAI-style `prompt_tokens_details.cached_tokens` and
DeepSeek-style `prompt_cache_hit_tokens` are normalized into
`cacheReadInputTokens`; this usage never counts as an MPE result-cache hit.
The engine cache schema participates in every result-cache key and is also
stored in cache metadata. Engine construction removes cache entries from older
wire schemas; execution history remains untouched.

A forced refresh bypasses cache reads and replaces the matching cache entry only
after provider execution and output validation succeed. Failed refreshes do not
destroy an existing valid entry.

Sensitive tasks are synchronous only. Their result is returned in memory to the
active caller and omitted from both cache entries and persisted execution
records.

Each data directory has one service-process owner. The HTTP service stores every
asynchronous request and its queued execution envelope in one SQLite transaction.
A bounded fixed worker pool claims queued rows; startup moves interrupted running
rows back to queued before workers begin. Records without a durable queue payload
remain unrecoverable and are marked failed rather than reconstructed. SDK and
one-shot CLI engine construction does not perform recovery, so it cannot rewrite
an active service's execution status.

Recovery provides at-least-once execution for work interrupted before a terminal
snapshot is committed. Queue rows whose execution snapshot is already terminal
are removed without re-execution. Result caching and caller idempotency remain
the safeguards for the narrower crash window before terminal persistence.

Execution history is the technical audit source for all normal task executions
and Provider connection tests. The paginated history contract excludes result
bodies and exposes only task identity, Provider/model identity, status, timing,
retry, Token-usage, and cache metadata. Calling projects may retain execution
IDs and batch-level aggregates, but do not own a competing complete ledger.
`execution_records` remains the operation-level terminal snapshot, while
`provider_call_records` stores one redacted row per real upstream request. The
second layer preserves usage for batch chunks, probe retries, transport errors,
contract-repair calls, and calls whose returned content later fails output-schema
validation. Neither layer stores prompts, caller input, Provider response bodies,
or credentials in the history query surface. The queue table temporarily stores
the complete validated asynchronous request for recovery, is not exposed by the
history API, rejects sensitive tasks through the request contract, and deletes
its row after terminal completion.

Execution records also normalize task kind and Provider/model identity into
indexed columns. Existing databases backfill those columns from their redacted
execution envelope during schema initialization. Time-window statistics query
the immutable UTC timestamps and group them in the caller-supplied IANA
timezone. Day, month, and year summaries are calculated from the event ledger;
there is no separate rollup table whose state could drift from the underlying
records.

## Security Boundary

The service is loopback-only by default. Non-loopback binding requires an
explicit remote opt-in and an API token supplied through the environment.
Provider secrets are also environment-only. Task schemas may use internal JSON
Schema fragments, but external references are rejected to prevent validation
from retrieving untrusted resources.

The HTTP boundary rejects malformed or oversized declared request lengths and
also counts the actual ASGI request body before framework parsing. Provider
transport independently bounds successful and error response bodies before JSON
decoding. These limits constrain memory use on both sides of the execution
contract.

Remote authentication represents the service as a whole. It does not create
caller principals, bind namespaces to identities, enforce per-caller quotas, or
authorize access to individual execution records. Sharing is therefore limited
to trusted callers; mutually untrusted callers require separate service
instances, credentials, and data directories.

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

Provider-native JSON Schema is capability-gated. An OpenAI-compatible Provider
uses strict `json_schema` response format only when its configuration explicitly
declares `native_json_schema`; otherwise it uses JSON-object mode. Local Draft
2020-12 validation always remains authoritative. A validation failure may trigger
the task's bounded contract-repair count. Each repair is a separate audited
Provider call and is not counted as a transport retry.

## Provider Concurrency Ownership

Concurrency limits belong to Provider configuration rather than caller Task
Packs or business adapters. The engine keeps an independent bounded slot pool
for each Provider, shared by synchronous calls, asynchronous executions, and
Task Pack chunk workers. The effective limit is the smaller of the Provider's
`maxConcurrency` value and the service-wide
`MPE_MAX_PROVIDER_CONCURRENCY` safety ceiling.

Worker counts are demand-driven. A single pending request occupies one slot;
larger workloads can fill the configured limit, and excess calls wait without
forcing the caller to coordinate with other projects using the same MPE
process. Concurrency settings do not participate in cache identity because they
change scheduling, not model semantics.

The asynchronous worker count bounds how many execution envelopes may be active
at once; Provider semaphores remain the final upstream concurrency ceiling.
Queue capacity bounds queued plus active asynchronous work and applies
backpressure with HTTP 429 before another request is persisted.

## Local Administration Boundary

The loopback-only `/admin` surface manages deployment configuration, not caller
business tasks. The same-origin page sends strictly validated Provider drafts to
versioned admin routes. Mutations require a per-process CSRF token, a loopback
Host, and an exact Origin match. Admin routes are unavailable when remote mode is
enabled.

Provider tests use submitted credentials only in memory. A successful test
issues a short-lived token bound to the exact Provider/model-list/default-model/
key/concurrency payload. Only a matching token can activate that payload.
Every test attempt is written to the redacted execution ledger; successful
tests retain Provider-reported usage when available, while failed tests retain
status and a bounded diagnostic without credentials or request content.
Provider presets are non-secret UI defaults; protocol type remains the runtime
boundary. Model discovery uses the configured OpenAI-compatible `/models` path
and submitted or previously stored credential only in memory, with manual model
entry as the compatibility fallback. Activation atomically writes the project
`.env` and ignored `config/providers.local.json`, then starts a detached control
helper that uses the existing verified process manager to restart the service.
The UI polls health and obtains a new CSRF token after recovery.

The admin surface keeps Provider configuration and execution observability as
separate top-level views. Its history view defaults to formal tasks so Provider
connection tests do not silently affect business-call statistics. A dedicated
statistics route owns period summaries, trend buckets, filter facets, and
Provider/model breakdowns, while the paginated execution route remains the
redacted record-detail source.

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
