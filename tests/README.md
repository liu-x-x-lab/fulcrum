# Testing

Select checks for the changed behavior. Full regression is a separate task unless
a shared runtime, authentication, schema or other high-risk change requires it.
Tests use fictional data and mocked external services. Never use a live database,
real recipients or deployment credentials.

For small, low-risk requests, run only the minimum directly relevant checks and
stop when they pass. Do not default to full-suite or full end-to-end runs; expand
only for new failures, further changes, concrete risk or an explicit user request.
Follow [the shared verification policy](../AGETNS-DEV.md#5-verification-and-test-maintenance)
to limit time, compute and token consumption.

## Run focused checks

Install development dependencies as described in [CONTRIBUTING.md](../CONTRIBUTING.md).
The [isolated runner](../scripts/run_tests_isolated.sh) accepts a **text file with
one test path per line**, not a Python test file:

```bash
test_list="$(mktemp)"
printf '%s\n' tests/test_plugin_template.py tests/test_plugin_manifest.py > "$test_list"
PYTHON=.venv/bin/python JOBS=1 scripts/run_tests_isolated.sh "$test_list"
```

Each file gets its own process, temporary database, logs, timeout and relay port.
`JOBS` defaults to 4, `TIMEOUT` to 300 seconds and `TZ` to `Asia/Shanghai`. Some
files mutate process-wide settings, so do not combine unrelated suites in one
pytest process. Local HTTP integration checks need permission to open a loopback
socket. Browser checks need the existing Playwright/browser setup; a skipped
browser check is not evidence of working interaction.

[conftest.py](conftest.py) rejects non-SQLite databases and paths outside the system
temporary directory or `.run/test-runtime/`, and always excludes business `data/`.
An explicitly configured test database also requires `APP_ENV=test`. Preserve
these guards and let the isolated runner supply environment values.

## Select by boundary

| Change | Useful starting points |
| --- | --- |
| Plugin behavior | `test_plugin_<code>.py`, `test_music_plugin.py`, relevant `test_existing_*_plugins.py` |
| Manifest / template / schedules | `test_plugin_manifest.py`, `test_plugin_template.py`, `test_plugin_schedule_manifest.py`, `test_plugin_scheduler.py` |
| Catalog / packages | `test_plugin_migration_catalog.py`, `test_plugin_packages.py`; `python -m scripts.verify_plugin_migration` |
| Configuration / secrets / group overrides | `test_plugin_secret_forms.py`, `test_plugin_runtime_secrets.py`, `test_group_plugin_settings.py` |
| Tenant / authorization / shared state | `test_tenant_db_scope.py`, `test_tenant_security_matrix.py`, `test_plugin_common_services.py` |
| Outbound scope / recall | `test_plugin_outbound_claim_scopes.py`, `test_message_recall_events.py`, `test_plugin_auto_revoke.py` |
| UI source contracts / visual structure | `test_ui_contracts.py`, `test_soft_ui.py` |
| Browser behavior | Relevant `*_browser.py`, `test_select_layout.py`, existing `*.test.cjs`; `node --check` for changed scripts |
| Documentation center / example ZIP | `test_developer_docs.py`, `test_plugin_template.py` |
| Supported imports / upgrades | `test_legacy_import.py`, `test_alembic_lifecycle.py`, `test_webridge_legacy_upgrade.py` |

These are entry points, not a requirement to run every listed file. CI's selected
Python suite is [ci-test-files.txt](../.github/ci-test-files.txt); the
[workflow](../.github/workflows/ci.yml) also runs explicit JS and delivery checks.
Run the selected Python suite with:

```bash
PYTHON=.venv/bin/python scripts/run_tests_isolated.sh .github/ci-test-files.txt
```

No argument discovers all top-level `tests/test_*.py`. The runner does not
recursively discover subdirectories or automatically execute JavaScript tests.
A selected CI run is not a full regression.

## Maintain coverage without process clutter

- Put regressions next to the durable feature they protect, not in files named
  after PRs, dates or implementation phases. Reuse fixtures without importing
  another suite's test functions, which pytest may collect again.
- Merge files only when fixture/process boundaries remain valid. Preserve
  assertions and parameterized cases, and update imports, CI paths and guides.
- Remove a test only when its behavior is retired or equivalent coverage exists.
  An old filename, a failure or absence from CI is insufficient. Mixed large
  suites require case-by-case review rather than bulk deletion.
- Preserve tenant, permission, secret, side-effect and supported migration checks.
  Keep `golden/` fixtures and JS tests required by current behavior.
- Keep generated reports, screenshots and local audit notes under ignored `.run/`
  or the local documentation working directories. Report actual passes, failures,
  skips and unverified layers concisely; do not publish raw execution diaries.
