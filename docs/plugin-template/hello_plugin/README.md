# hello_plugin

An installable example for building a Fulcrum directory plugin. The authenticated
documentation center provides the full plugin development guide and this ZIP.

| File | Purpose |
| --- | --- |
| `config.toml` | Identity, matching, configuration fields and a daily schedule |
| `plugin.py` | Stateless, message and scheduled execution |

## Demonstrated behavior

- `你好` / `hello`: configurable greetings with bounded values.
- `你好榜`: tenant/binding-scoped state, storing stable member IDs and resolving
  group nicknames when displaying results.
- Optional daily greetings: manifest schedule and the platform outbound queue.

## Adapt and install

1. Copy the directory and choose a code matching `[a-z][a-z0-9_]{1,63}`.
2. Set the same manifest code, then update display name, description, version
   and author. Keep UI labels consistent with the Chinese application.
3. Adjust matchers and declared configuration fields, then implement only the
   required execution paths. Test invalid configuration and tenant/group scope.
4. ZIP the files at the archive root or under one plugin directory. A platform
   administrator installs the package and configures its tenant visibility.

Scheduled execution receives a binding, usually no conversation. Resolve owned,
enabled targets and enqueue through `enqueue_outbound_job`; do not call channels
or start timers. Missed daily windows are skipped.

Increase the version for releases. Package rollback does not undo business data.
Keep credentials, real account IDs and local configuration out of the package.
Use encrypted secret fields and the platform's common services. Archives must
exclude symlinks, absolute paths and traversal, and fit the platform package limits.
