# Security Policy

## Supported versions

Model Processing Engine is experimental. Security fixes are applied to the
current `main` branch and the latest published `0.1.x` release only.

| Version | Supported |
| --- | --- |
| Latest `0.1.x` | Yes |
| Older versions | No |

## Reporting a vulnerability

Use
[GitHub private vulnerability reporting](https://github.com/piercezzs/model-processing-engine/security/advisories/new)
for a vulnerability that has not already been made public.

Do not open a public issue for an undisclosed vulnerability. Do not include
provider credentials, API tokens, real task payloads, cached model results, or
other sensitive user data in a report. Use synthetic reproduction data and
describe the affected version, execution surface, impact, and minimum steps
needed to reproduce the issue.

Reports about MPE's runtime, HTTP boundary, provider transport, persistence,
process management, or packaged admin interface are in scope. Vulnerabilities
in third-party model providers, caller-owned Task Packs, reverse proxies, or
calling-project business logic should be reported to their respective owners
unless MPE itself creates or amplifies the issue.

The maintainer will acknowledge a complete report as soon as practical, assess
its impact, and coordinate disclosure after a fix or mitigation is available.

## Deployment boundary

MPE is local-first and has not been certified for remote production use. A
remote deployment uses one service-wide bearer token and is not a multi-tenant
authorization boundary. Mutually untrusted callers must use separate service
instances, credentials, and data directories.
