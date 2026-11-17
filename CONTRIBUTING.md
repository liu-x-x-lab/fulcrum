# Contributing

Read [AGETNS-DEV.md](AGETNS-DEV.md) before developing a plugin or changing its UI.
It defines the shared architecture, plugin contracts, UI rules, and delivery
requirements. [AGENTS.md](AGENTS.md) is the entry point for compatible coding
agents; configure other tools to read the same contract instead of maintaining
independent instructions. Keep workstation and deployment details in the ignored
`AGENTS.local.md` file.

## Development environment

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

The example environment uses development settings. Never copy real deployment
credentials into source, examples, or test configuration.

## Focused checks

Use [the testing guide](tests/README.md) to select and run checks for the changed
boundary. It documents the isolated runner's list-file input, temporary database
guards, CI selection and test maintenance rules. Do not claim full regression
from a focused run. Documentation changes need link/example checks and
`git diff --check`; check the documentation-center routes when their content changes.

Use [the documentation index](docs/README.md) to update the canonical guide for a
feature. Keep plans, raw output and acceptance diaries in ignored local directories.

## Code and pull requests

- Follow the current architecture and shared UI components. Remove obsolete paths
  within scope instead of adding compatibility branches for retired behavior.
- Explain non-obvious rules and side effects in Chinese code comments. Use English
  for public documentation, commits, PR titles/descriptions, and release notes.
- Use tenant-scoped database access. Schema changes need Alembic migrations that
  support SQLite and MariaDB.
- Keep commits focused and preserve unrelated working-tree and staged changes.
  Explain the product behavior, reason for the change, and checks actually run.
- For UI work, include concise, sanitized evidence when useful; keep raw process
  captures, logs, and development notes outside the public change.

## Private plugins and local material

Private or localized plugins do not belong in the public repository. Keep their
paths ignored, including related resources outside the plugin directory. The
comma-separated `FULCRUM_PRIVATE_PLUGINS` variable excludes local packages from
the public catalog check; it does not prevent Git from tracking or publishing
them, and does not exempt them from the common screenshot policy.

Review explicit file selections and all history being pushed. Exclude historical
projects, credentials, local instructions, deployment information, runtime data,
backups, and process records. Follow the publication rules in `AGETNS-DEV.md`.
