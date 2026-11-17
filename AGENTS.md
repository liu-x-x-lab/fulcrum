# AI development instructions

Before planning, editing, or reviewing this repository, read
[AGETNS-DEV.md](AGETNS-DEV.md) in full. It is the shared development contract for
Fulcrum, including plugin architecture, UI conventions, validation, and delivery.
Read the referenced implementation files relevant to the task before changing them.

If `AGENTS.local.md` exists in this checkout, also read it for local workflow and
deployment instructions. Keep that file private; do not copy its contents into
tracked files, public logs, commits, or pull requests. Local operational details
must not weaken the shared tenant, secret, or publication boundaries.

Preserve unrelated working-tree and staged changes. Commit only the files or
hunks belonging to the requested task, and report the checks actually performed.
