# Model Processing Engine Project Rules

## Project Identity

This repository is a business-neutral runtime for executing externally supplied
model tasks. It owns execution mechanics, not caller business semantics.

This repository must remain standalone. It must not import another project's
code, read project-specific paths, depend on project-specific schemas, or modify
another project. Real project integrations are separate follow-up work.

## Ownership Boundary

- The engine owns provider calls, retries, concurrency, progress, usage,
  contract validation, execution records, and cache infrastructure.
- Calling projects own task prompts, input and output schemas, taxonomies,
  semantic cache policy, deterministic business logic, acceptance decisions,
  and durable business results.
- The engine may retain immutable task and execution digests for reproducibility,
  but it is not the source of truth for business task definitions.
- Task packs may contain declarative data only. Never execute caller-supplied
  Python, JavaScript, shell, templates with arbitrary evaluation, or other code.

## Runtime And Security

- Python 3.10 or newer is required.
- The HTTP service binds to `127.0.0.1` by default. Remote binding requires an
  explicit environment opt-in and an environment-provided API token.
- Provider credentials come only from environment variables named in local
  provider configuration. Never place credentials in task packs, requests,
  logs, fixtures, or committed files.
- HTTP task requests contain data, never arbitrary local filesystem paths.
- Cache namespaces are isolated by caller project. Cross-project cache reuse is
  forbidden in the core contract.
- Sensitive tasks must disable local result caching.
- One service process owns one data directory. Do not run competing service
  processes against the same execution-record database.

## Architecture

- `contracts.py`: stable external contracts and validation limits.
- `task_loader.py`: safe loading of caller-owned task-pack directories.
- `providers/`: provider protocol and implementations.
- `cache.py`: SQLite cache and execution-record ownership.
- `engine.py`: orchestration, batching, validation, and single-flight behavior.
- `service.py`: versioned loopback HTTP surface.
- `cli.py`: local validation, execution, service, and cache-maintenance entrypoint.

Keep dependency direction one-way: entrypoints depend on the engine; the engine
depends on contracts, cache, and providers; lower layers never import entrypoints.

## Verification

The project-local verification script is the canonical completion entrypoint.
Before reporting implementation complete, run it with the repository virtual
environment:

```bash
# macOS / Linux
.venv/bin/python scripts/verify_project.py

# Windows
.venv\Scripts\python scripts\verify_project.py
```

Do not manually reconstruct the checks already owned by this script. Use
`scripts/verify_project.py --runtime` when changes affect service startup,
process management, HTTP routes, or served admin assets and a managed loopback
service restart is within the approved task scope. Default verification must
remain deterministic and must not restart the service.

Real provider calls are optional acceptance checks and must never be required by
the default test suite.
