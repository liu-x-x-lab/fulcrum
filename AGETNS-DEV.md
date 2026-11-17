# Fulcrum AI-assisted development contract

This is the shared contract for developers and coding agents. **MUST** is a
requirement; **SHOULD** is the default unless the task has a documented reason to
differ. Explicit user instructions govern scope.

## 1. Start here

The root [AGENTS.md](AGENTS.md) directs agents here. Tools that do not read
`AGENTS.md` must be configured to read this file. Keep tool-specific entry points
short and reference this contract instead of copying it.

1. Read the request, this contract, optional `AGENTS.local.md`, and `git status`.
   Preserve unrelated working-tree and staged changes.
2. Trace the current entry point, service, configuration, template, and relevant
   tests. Check existing implementations before introducing another abstraction.
3. State the intended user behavior, affected boundaries, reuse, and focused
   checks. Resolve material ambiguity before dependent work.
4. Implement the complete requested path using the confirmed architecture.
   Remove obsolete paths within scope; do not add aliases, dual writes, hidden
   fallbacks, or parallel account systems for retired behavior.
5. Run relevant checks, review the exact diff, and commit only the task's changes.
   Distinguish implementation, installation, deployment, and real-device results.

Read the relevant canonical guide before editing its boundary:

| Work | Reference |
| --- | --- |
| Plugin manifest, execution, configuration, schedules, packaging | [Plugin development](docs/plugin-development.md) |
| Templates, components, visual values, interaction and accessibility | [UI guidelines](docs/ui/soft-ui-guidelines.md) |
| Test selection, isolation and maintenance | [Testing](tests/README.md) |
| API, channel integration, deployment and supported data import | [Documentation index](docs/README.md) |

Keep detailed contracts in those guides. If documentation and implementation
disagree, inspect the call site and tests and correct the affected guide with
the approved change. A historical proposal or local experiment is not a public
API. Do not duplicate implementation diaries in the shared contract.

## 2. Architecture and ownership

Fulcrum is a multi-tenant bot operations platform using Python 3.11, FastAPI,
SQLAlchemy 2, Alembic, Jinja2, shared CSS and vanilla JavaScript, with SQLite and
MariaDB support. Ordinary features must not introduce a separate frontend
framework, database, worker architecture, or authentication system.

| Location | Responsibility |
| --- | --- |
| [app/main.py](app/main.py), [app/web](app/web), [app/api](app/api) | Composition, browser routes and machine-facing boundaries |
| [plugin_directory.py](app/services/plugin_directory.py), [plugin_manifest.py](app/services/plugin_manifest.py) | Discovery, matching, dispatch and TOML declarations |
| [plugin_runtime](app/services/plugin_runtime) | Execution contracts and common plugin services |
| [plugins](plugins) | Directory plugins, one business capability per package |
| [group_features](app/services/group_features) | Platform-native group features and their registry |
| [tenant_context.py](app/core/tenant_context.py), [tenant_db.py](app/core/tenant_db.py) | Trusted tenant identity and database scope |
| [app/models](app/models), [migrations](migrations) | Persistence and Alembic changes |
| [app/templates](app/templates), [app/static](app/static) | Shared UI and browser behavior |

Choose the existing extension boundary:

- Commands, queries, observers and scheduled business replies use directory
  plugins. Inspect both plugin and native group-feature registries to avoid
  implementing the same behavior twice.
- Ordinary settings use manifest fields and the shared configuration form.
  Rich CRUD/upload workspaces use explicit reviewed routes, services, models
  and shared templates; see the plugin guide's administration section.
- New transports belong in channel adapters. Capabilities needed by several
  plugins belong in an existing common service after checking its callers.
- Platform administration, statistics, updates and group-scope management stay
  in the platform. Do not recreate them as plugins.

The message path is channel normalization and inbound policy, platform dispatch
and scope checks, plugin execution, then platform-managed outbound delivery.
Preserve [self_sent.py](app/services/self_sent.py) and the gates in
[channel_events.py](app/services/channel_events.py),
[async_workers.py](app/services/async_workers.py) and
[inbound_policy.py](app/services/inbound_policy.py). Self-sent messages are
archived without content automation; expired messages must not trigger delayed
business actions. Test incoming automation with a separate sender.

## 3. Mandatory implementation boundaries

- Start plugins from [hello_plugin](docs/plugin-template/hello_plugin). Implement
  real actions, explicit unsupported-action results and a side-effect-free
  reload. Discovery or a placeholder response is not completion.
- Use platform-assembled `request.config` and trusted `request.context`.
  Payloads, form fields and external sender names cannot grant tenant authority.
  Do not fabricate context or fall back to another tenant's identity or secrets.
- Use the supplied tenant-scoped ORM session. Verify bot/channel/conversation
  ownership as well as tenant ownership. Follow the caller's transaction boundary;
  do not close shared sessions, commit unrelated work, or open another database.
- Tenant-owned models inherit `TenantOwnedMixin`; relevant keys and constraints
  include tenant identity. Schema changes require Alembic migrations supporting
  SQLite and MariaDB. Do not mutate schema from plugin startup.
- Background jobs restore trusted tenant scope through the existing worker path.
  Module imports must not start threads, install dependencies or contact services.
- Editable fields belong in `[[config.fields]]`. Secrets use encrypted binding
  storage and explicit clearing controls; group overrides cannot store secrets
  or replace binding identity. Validate persisted values at execution boundaries.
- Return standard replies and use the platform outbound queue for proactive sends.
  Do not call concrete channel clients or build plugin-specific delivery queues.
- New periodic actions use `[[schedule]]` and `execute_scheduled_action`. Resolve
  owned targets, respect group/plugin scope and skip missed daily windows.
  Queueing does not prove delivery. Retrying charges, rewards or external effects
  requires idempotency; a timeout does not prove rollback.
- Reuse common HTTP, state, rate-limit, message, text-generation, media and
  screenshot services described in the plugin guide. `PluginHttpClient` is not a
  complete SSRF barrier; apply the relevant resource-fetch validation as well.
  Plugins must not launch browsers or define per-plugin screenshot engines.
- Plugins execute as server-side Python in the application process. Manifest
  checks and timeouts are not an OS sandbox. Only platform administrators install
  packages; tenants configure plugins allowed by platform policy.
- Return truthful errors and safe identifiers. Never log full configuration,
  decrypted secrets, authorization headers or sensitive upstream payloads/URLs.

## 4. UI rules

**Avoid unnecessary secondary labels and descriptions. Let the layout do the explaining.**

Use grouping, alignment, spacing and hierarchy to explain relationships. Remove
copy that repeats visible content; retain essential field labels, status/error
feedback and warnings for consequential actions.

Use the compact Soft UI design, shared tokens, server-rendered page shells,
configuration macros and interaction scripts specified in the
[UI guidelines](docs/ui/soft-ui-guidelines.md). A settings-only plugin does not
need a separate page, stylesheet or frontend application.

Preserve form actions, names, validation and CSRF. Enforce tenant membership,
roles, plugin availability and resource ownership on the server; hidden controls
do not enforce permission. Escape untrusted content and keep platform and tenant
authentication distinct. Show pending, success and failure states, prevent
duplicate submission and preserve input on errors. Check keyboard operation,
mobile overflow and relevant long/empty/error states when changing UI behavior.

## 5. Verification and test maintenance

Prioritize core business functionality and its main path. Run focused lightweight
checks for the changed boundary, using [the testing guide](tests/README.md).
Expand coverage for shared runtime, authentication, schema or other high-risk
changes. Record unrelated historical failures without expanding scope to fix them.

Small, low-risk requests MUST use only the minimum directly relevant checks;
do not run full-suite tests, full end-to-end flows or whole-product regression
by default. Prefer a targeted unit test, syntax/static check or brief check of
the affected behavior. Documentation-only edits need content/link review and
`git diff --check`. Once relevant checks pass, stop; do not repeat builds or
broaden testing without a new failure, further changes, a concrete risk or an
explicit user request. Minimize time, compute and token consumption.

Tests use fictional data, mocked external services and temporary SQLite only.
Do not bypass [conftest.py](tests/conftest.py), source deployment credentials or
target live recipients. The isolated runner takes a **list file of test paths**.
Do not combine unrelated files in one pytest process when they mutate globals.

Organize regressions by durable behavior, not a PR number or development phase.
Before removing a test, establish that its behavior is retired or its assertions
are covered elsewhere. Preserve tenant, permission, secret, idempotency and
supported migration checks. Update CI paths and fixture imports when moving tests.
Do not require full-suite coverage or add tests that only mirror documentation.

Report the checks actually run, failures and unverified layers. Syntax, template
rendering, browser interaction and external message receipt are different evidence.
Run `git diff --check`; documentation changes also need link/example validation.

## 6. Delivery and publication

Public documentation, GitHub commits, PR titles/bodies and release notes MUST use
English. Meaningful Chinese source comments explain non-obvious business rules,
ownership and side effects. Application labels/messages remain consistent with
the current Chinese UI.

- Publish current product source, safe configuration templates, necessary tests,
  concise usage/API/deployment guides and shared development rules.
- Keep historical/other projects, obsolete implementations, backups, private or
  localized plugins and their resources out of public changes. Check related
  paths outside `plugins/` as well.
- Keep local plans, session records, drafts, audit diaries, raw test output,
  process screenshots and runtime data local. `docs/plans/`, `docs/reviews/`,
  `docs/reports/` and `docs/screenshots/` are ignored working directories.
  Deliberate sanitized product illustrations belong in `docs/images/` and must
  be referenced by maintained documentation.
- Never publish `AGENTS.local.md`, deployment credentials, internal addresses,
  real accounts/groups or workstation tool settings. Examples must be fictional.
- `FULCRUM_PRIVATE_PLUGINS` only affects catalog checks. It does not prevent Git
  tracking or grant permission to publish private packages.
- Stage explicit task paths/hunks; inspect the entire proposed commit. Do not
  use `git add .` or `git add -A`, or bundle another developer's staged changes.
- An ignore rule does not remove already tracked files or history. Check every
  commit being pushed. If internal history contains excluded material, prepare
  a separate public export/branch without that history; preserve internal history.
- Follow the plugin guide's package limits, versioning and licensing rules.
  Package rollback does not roll back data, migrations or external effects.
  A local commit alone does not authorize pushing or deployment.

Completion requires one clear current path, agreement between code and its
canonical guide, preserved ownership/secret boundaries, appropriate checks and
a scoped commit. Keep machine-specific operations and execution diaries local.
