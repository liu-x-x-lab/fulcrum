# Group content moderation

Open an account's group details, select **Group features**, and configure
**Sensitive-word moderation**. This native feature is disabled by default and
does not require a plugin binding. The group must be in normal operating mode.

Enter one literal phrase per line (up to 200 phrases / 2,000 characters).
Matching normalizes Unicode compatibility characters and letter case. It does
not use regular expressions, infer intent, remove punctuation, or join separated
characters. Only text messages are inspected. A message matching several phrases
creates one violation. Owners, administrators, the connected account and explicitly
exempt member IDs are excluded.

Choose warning only or removal after a configured number of violations. Counts
are scoped to the tenant, account, group and member. The default window is the
last 24 hours; zero means lifetime counting. Counts are not reset by changing
mode, disabling the feature, or a member leaving and rejoining. Warning and
successful-removal notices support `{member}`, `{count}`, `{limit}` and `{group}`;
an empty template suppresses that notice. Notices use the existing outbound queue.

Matched messages remain archived but do not continue into ordinary group
features, plugins or AI replies. Existing self-sent, quiet-hours, group-operation
and stale-message gates still apply. Duplicate delivery and worker retries do
not add another violation or enqueue another action.

## Removal and results

Removal is implemented for WeBridge devices declaring `kick_group_member`
and for iPad 874 through the same group feature.
Other channels can warn, but report removal as unsupported. The connected account
must be a known group owner or administrator, and the target must be a known,
active ordinary member. Synchronize group members when permissions are unknown.

The adapter follows the [HTTP contract](https://wechat.vbzm.cn/docs/8080/http)
and [long-poll contract](https://wechat.vbzm.cn/docs/8080/long-poll): the command
uses `room_id` and `member_id`, and successful evidence must identify the same
room and single target in `member_ids`, with `member_count` equal to one.
The long-poll envelope carries the execution `ok` field; explicit nested failure
also prevents success. Queue acceptance never means the member was removed.

Existing iPad groups must save the removal option in
the group feature again after upgrading; switching the feature on is not enough.
The worker checks the current group profile and complete roster
before removal. It requires a known owner, an active owner or administrator
account and a single ordinary target. It records the attempt before calling
`DelChatRoomMember`. Only a successful business response listing that exact
member confirms removal. A timeout, missing evidence or interrupted attempt
is not retried. The roster is maintained by directory synchronization, not by
inferring a removal from an absent name.

Before a device claims the command, the platform rechecks the group setting,
membership, permissions, exemptions, threshold and counting window. Commands
older than the original message's ten-minute execution window are cancelled.
Only a confirmed successful result generates the success notice. Timeouts and
incomplete or mismatched evidence leave the result unknown and prevent another
automatic removal for that member. A late confirmed result can settle the same
request without executing it again. Verify membership manually when a result
remains unknown; do not resubmit an uncertain removal with a new request ID.

The feature card shows the most recent 20 violations and removal outcomes.
Operational failures also appear in the error center. Neither complete message
contents nor device error payloads are copied into general error logs.

## Ownership and upgrades

`group_features/moderation.py` owns configuration, `group_moderation/` owns
matching, records and action settlement, and `channel_group_operations.py`
owns shared membership authorization. The WeBridge adapter and existing command
queue own transport. `async_workers.py` runs moderation before ordinary group
features and plugin dispatch.

Apply Alembic migration `20261117_0042` before starting the updated application.
It creates the tenant-owned violation ledger with per-group message uniqueness
and an index for member/time counting. No live groups are automatically enabled.
