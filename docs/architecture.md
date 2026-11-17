# Architecture and shared dependencies

Use this map with the [AI development contract](../AGETNS-DEV.md) and
[plugin guide](plugin-development.md). Search the domain and function name first;
application entry points should remain small enough to show composition clearly.

## Source map

| Location | Owns |
| --- | --- |
| `app/main.py` | Application composition, lifecycle, request metrics, authentication middleware and health probes |
| `app/core/application.py` | Process start time and repository root |
| `app/web/shared/` | Tenant access/session helpers, template environment, URLs, forms, queries and operation feedback |
| `app/web/channels/` | Account pages, connection workflow, iPad login, channel actions, WeBridge and WeCom contacts |
| `app/web/chatrooms/` | Directory/detail pages, group management and group plugin settings |
| `app/web/plugins/` | Plugin administration routes and view builders |
| `app/web/messages/` | Conversations, messages, attachments and moments |
| `app/web/dashboard.py`, `bots.py`, `points.py`, `setup.py` | Their corresponding browser domains |
| `app/api/channel_webhooks.py` | Signature verification and trusted tenant restoration for generic channel inbound events |
| `app/services/wechat_ipad/operations/` | Login, status, heartbeat, recovery, contacts, synchronization and scope mutations |
| `app/services/wechat_ipad/service.py` | Generic protocol action orchestration |
| `app/services/wechat_ipad/runtime.py`, `transport.py` | Runtime persistence and protocol call/result handling |
| `app/services/wechat_ipad/group_details.py` | Group detail loading and inherited-scope preparation in the caller transaction |
| `app/services/wechat_ipad/views/` | Account/group snapshots, overview and operator priority presentation |
| `app/services/wechat_ipad/diagnostics/` | Error, message-flow, synchronization and recent-protocol diagnostics |
| `app/services/wechat_ipad/common.py`, `qr.py`, `login_profiles.py`, `events.py` | Value parsing, QR state, device profiles and event/sync signal extraction |

The iPad package exports its supported public operations and view builders from
`__init__.py`, loading their actual owner lazily. Internal callers may import the
specific owner. Do not restore private helpers in `service.py` as aliases for
old import or test paths.

## Dependency direction

- Application composition imports routers. Routers import shared browser helpers
  and services. Neither may import `app.main`.
- Shared browser helpers must not depend on domain routers. Put a reused parser,
  access check or rendering dependency in its narrow shared owner.
- iPad operations use transport, runtime persistence and value helpers. Runtime
  returns view snapshots; views and diagnostics must not call operations or
  mutate runtime state. Group views may read through the caller's ORM session.
  `group_details.py` explicitly retains the existing inherited-scope materialization
  and flush when scopes are absent; it does not commit or contact a device.
- Imports must not start workers or call external services. Preserve caller
  transaction boundaries, authentication, tenant scope, CSRF and outbound policy
  when moving code.
- Register fixed routes before overlapping dynamic routes. A move must preserve
  paths, methods, names, dependencies and dispatch precedence. Update test imports
  and mocks at the new owner; do not preserve an obsolete application namespace.

Split by responsibility when a file mixes unrelated domains or becomes difficult
to retrieve and review. Several hundred cohesive lines are preferable to another
large catch-all or many one-function wrappers. Give modules descriptive names and
Chinese docstrings explaining ownership and side effects. Avoid numbered parts,
wildcard imports, dynamic forwarding and duplicated helpers. Update this map when
ownership changes. Use focused tests for affected boundaries rather than adding a
full regression requirement to every small change.

## Shared external services

These are platform integrations, not dependencies for each plugin to install.
Application configuration belongs in `app/core/settings.py`; deployment secrets
and operator-specific endpoints stay outside source control.

| Capability | Implementation | Configuration and behavior |
| --- | --- | --- |
| Web/HTML screenshots | [web_screenshots.py](../app/services/web_screenshots.py), [Markdown adapter](../app/core/markdown_to_image.py) | `KITESURF_WS_URL`; empty means unavailable. Timeout defaults to 45 seconds (1–120); process-local concurrency defaults to 2 (1–8). |
| Long-term AI memory | [ai_memory.py](../app/services/ai_memory.py), [ai_memory_digest.py](../app/services/ai_memory_digest.py) | `HINDSIGHT_URL`; empty disables memory. The address is platform configuration, not tenant input. |
| Text generation | [plugin_runtime/text_generation.py](../app/services/plugin_runtime/text_generation.py) | Use the shared execution context and resolved binding configuration. |
| AI search | [ai_search.py](../app/services/ai_search.py) | Reuse the existing AI search path rather than adding another search stack. |

### Kitesurf

The checked-in [engine deployment](../deploy/kitesurf/Dockerfile) builds a pinned
Cloudflare Kitesurf revision and runs it as an independent Wrangler/Workerd CDP
service. The application connects through WebSocket. A Cloudflare cloud deployment
is not required by this integration.

Plugins call `capture_screenshot(ScreenshotRequest(...))` and return
`ScreenshotResult.as_image_reply()` through ordinary platform delivery. Markdown
rendering already uses the same service. Do not launch a browser, install a second
engine, expose CDP settings in a plugin or silently switch engines on failure.

Each request gets its own browser context and page. Preserve trusted tenant scope,
public URL validation, engine network restrictions, resource cleanup, timeout and
image validation. Never forward application cookies, authorization headers or
local files. HTML resources must be accessible to the engine. Concurrency limits
are per application process; they are not a distributed quota.

The [plugin guide](plugin-development.md#web-and-html-screenshots) owns input/output
limits and error codes. `POST /api/tool/screenshots` uses tool-key authentication
and a validated tenant identity; it returns bytes, not an outbound message.

### Hindsight

The implemented memory contract uses recall and asynchronous retain under
`/v1/default/banks`. Do not assume an unimplemented reflection API or copy a
memory client into a plugin. Use the existing AI chat/worker path and respect the
conversation's memory settings. Network calls must happen outside database
sessions held by the chat/digest workflow.

Bank IDs come from `ai_memory.bank_id`: groups use the tenant prefix plus sanitized
group external ID; direct messages use tenant plus channel ID. Group banks do not
include channel ID. Sender tags select personal memories while untagged group
background is shared. Preserve these semantics and trusted identity sources;
changing bank naming requires an explicit data migration decision.

Recall uses a 4-second timeout, up to 8 items and an 800-token budget; retained
requests use a 6-second timeout and asynchronous server processing. Accepted
writes do not prove completed extraction. The client has a 60-second availability
circuit; failed recall returns no memories and failed retain reports failure
without blocking ordinary chat. Do not inject recalled content as trusted system
instructions or log full memories and credentials.

Group digest starts at the latest message when first enabled, without replaying
history. It batches eligible messages after 30 messages or 20 minutes idle, with
limits of 60 messages and 8,000 characters per batch. Failed submission leaves the
pending batch cursor unchanged. Keep the existing cursor and daily-cost limits.
The [deployment configuration](../deploy/hindsight/docker-compose.yml) includes
PostgreSQL/pgvector plus separate LLM and embedding configuration; actual provider
values and credentials belong to the operator.
