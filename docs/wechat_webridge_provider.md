# WeBridge channel and directory operations

Fulcrum's `wechat_webridge` channel supports the WeBridge 8080 phone protocol
through long polling and WebSocket. Both transports share account confirmation,
tenant isolation, event ingestion and the outbound queue.

## Account connection

A tenant administrator creates an account from the WeBridge connection page,
generates an address and selects a transport. Fulcrum supplies the channel ID
and a random per-channel token; users do not edit adapter JSON.

```text
https://bot.example.com/api/wechat-webridge/12/<channel-token>
wss://bot.example.com/api/wechat-webridge/12/<channel-token>/ws
```

Use the complete generated address in the phone plugin. The token belongs in the
path, not query parameters. Address generation prefers `PUBLIC_BASE_URL`, falling
back to the browser request origin; use an address reachable from the phone.
Treat connection URLs as credentials. Server logs mask the token, but phone
settings and diagnostic output may still show it.

Registration records device, session, account and capability information. It does
not enable business processing or consume an account quota. The administrator
reviews the returned identity and confirms connection; before confirmation only
account/device information requests are allowed. Missing profile data remains
pending instead of being invented. Deleted drafts cannot be reactivated by an old
confirmation page.

Connection addresses are visible only to tenant administrators and can be
reopened from account details. Token-free addresses are rejected; an existing
account without a token must retrieve its current address and update the phone.
All phone endpoints validate the token and registered identity. Generic inbound
webhooks do not accept WeBridge messages.

## Transport and media

Long polling appends these endpoints to the token-bearing base address:

```text
POST /agent/register
POST /agent/state
POST /agent/event
POST /agent/poll
POST /agent/command_result
POST /agent/upload_media
POST /agent/moments_upload
```

`poll` delivers queued commands, and `command_result` reports execution. Incoming
media first creates a `MessageAttachment`, then requests `upload_message_media`.
The upload uses multipart `device_id`, `session_id`, `account_id` and `file`.
Moments uploads also require their existing one-time media token. Stored attachments
become `downloaded` after completion; open events then use the saved resource.

For WebSocket, preserve the `/ws` suffix and forward Upgrade through the reverse
proxy. The phone sends `hello`; the platform returns `hello_ack`. Identity checks
apply to events/results received during the handshake. Command frames use
`type:command`, `request_id`, `command_name` and `args`. Events and command results
return on the same connection; no business ACK is emitted. Full event IDs deduplicate
events; request IDs correlate results, and the outer `ok` determines success.
Late unassociated results do not close the connection.

Protocol Ping/Pong is handled by the WebSocket library. Application pings run every
25 seconds; no application pong for 60 seconds closes the connection. Missing
`hello` for 10 seconds or invalid identity also closes it. A valid reconnect can
refresh the session ID for the same device/account. Phone suspension may interrupt
connectivity. Long polling and WebSocket are mutually exclusive on the phone.

Downstream frames are limited to 10 MiB; media bytes use HTTP upload. Failed sends
or disconnects do not automatically replay claimed commands, because an external
action may already have happened. Verify uncertain results before retrying.

For ordinary Douyin parser videos, the phone receives the playback URL and
the size limit saved in the plugin's sending settings (default: 100 MiB).
Changes apply to new replies without restarting the service. Fulcrum does not
fetch the video. When the phone explicitly rejects a video larger than the
limit recorded on that command, a link card is queued once for the same
conversation. A timeout or an unrelated failure does not trigger a card.

## Directory synchronization

Member join, leave and group-nickname events update the directory after inbound
deduplication. Notification switches, quiet hours and storage-only mode do not
block directory updates; fully disabled groups remain excluded. Departed members
retain history and points and are shown as having left.

Member events schedule `room_members` to fill group name, remark, owner, member
names and roles. Pending requests coalesce per group, with a default two-second
delay and at least ten seconds between reads. A snapshot's directory version is
captured when claimed. Newer member events invalidate an older snapshot and request
a fresh read; stale/repeated events must not roll back current membership.

Empty, truncated, inconsistent or invalid-ID lists cannot establish departures.
Failed/incomplete reads back off at least 60 seconds. Pending refreshes can recover
when the phone reconnects. `groups_all` remains responsible for discovery and
confirming that the bot left a group. Synchronization preserves local group
settings, media switches and member metadata.

Ordinary messages create missing basic group/sender records and request discovery
and member details. Refresh is also requested for missing names/members or a full
member snapshot older than five minutes. This freshness timestamp is separate
from last message time. Requests coalesce; missing details retain ten-second
spacing and failure backoff. Media messages participate, system events do not count
as member activity. A fresh message may restore membership, but an older message
must not undo a later departure or re-enable a disabled/bot-departed group.

## Member notifications and welcome cards

The group member-change card configures a title and bodies for joining, leaving,
removal and nickname changes. Saving affects the current group. Empty titles omit
the title; an empty body suppresses that text, while welcome cards can still be
used. The join-notification switch also controls welcome cards.

Templates support `{nickname}`, `{group_name}` and `{now}` (Shanghai time).
Nickname changes additionally support `{old_nickname}` and `{new_nickname}`.
Use the pre-event directory and explicit event evidence for old names. Missing
and cleared names have distinct labels. Unknown variables remain literal, and
substituted values are not evaluated again. Multiple members share one title with
individually expanded bodies; text over 20,000 characters is explicitly truncated.

Welcome-card thumbnails use either a custom HTTP(S) image URL or the new member's
avatar. Avatar lookup is limited to that member in the current account/group,
then the same account's contact record; it never uses the inviter's avatar.
Each joining member gets their own card, up to three cards per event. Changing
the source preserves the custom URL but does not use it as an implicit avatar fallback.

If the required avatar is missing while the directory is synchronizing, the
notification job yields and checks again every second, for up to 15 seconds from
creation. Other messages continue. Once synchronization finishes or the deadline
expires, missing/invalid images are omitted without a later duplicate card.
Disabling notifications or a member leaving cancels the corresponding welcome;
waiting does not bypass the ten-minute inbound message-age limit. Fixed custom
URLs do not wait for avatars.

Card title, description, destination URL and custom image URL support single-pass
`{nickname}`, `{now}` and `{avatar_url}` substitution. Thumbnails use
`send_link_card.thumb_url`; actual display depends on the phone's send result.

## Official accounts

`friends` and `friends_all` do not supply official accounts. Use `official_accounts`
and its real `account_id`, not a guessed prefix. Keep official accounts separate
from friends and reuse their names/avatars in current and historical message views.

Full maintenance synchronization uses pages of 100, continues using `has_more`
and actual result size, refreshes hourly after success and backs off at least five
minutes on failure. Manual synchronization shares the same coalesced work. Empty
or failed results must not erase saved profiles. Filtered command-bridge queries
save returned profiles without expanding their requested scope.

Incomplete official-account conversations can also trigger a capability-checked
profile request, coalesced and spaced at least ten minutes apart. A direct article
can fill a missing name only when its publisher identity matches sender and
conversation; forwarded articles must not rewrite contact/group identity.
Page GETs do not dispatch synchronization. Article covers are not account avatars;
`CONTACTS_NOT_READY` is not success, and missing avatars retain the initial fallback.

## Friend checks and saved progress

Friend checks scan all synchronized friends of the selected account, independent
of list search/pagination. Each result is added as it completes; result tables
support independent pagination and selection. Checks use `friend_pay_name` with
`refresh: true`, not cached payment names. Only one check per account can be in
flight, with up to 60 seconds each for claim and execution.

Results are estimates: a returned payment-name suffix suggests mutual friendship;
no suffix suggests possible deletion; errors, timeouts or invalid results are
unknown. Shared groups and other causes can produce misleading results. Checking
does not send a payment/message or modify contacts.

The account's queue, contact snapshot, completed results, interval and progress
are persisted. Refreshing or reopening restores them; continuing requires an
explicit resume. Leaving the page stops dispatching subsequent checks. Unconfirmed
checks or contact actions query their original tasks rather than duplicating them.
Pausing waits for the current item; a restart begins a new scan. Clearing saved
records requires confirmation and changes the generation, so old pages and late
receipts cannot restore cleared data. Deleted contacts retain their scan snapshot.

Tenant administrators/operators can check, recheck, append remarks and delete
contacts, subject to role, ownership and CSRF validation. Read-only users can see
saved progress. Deletion needs confirmation; bulk deletion also requires the count.
Only a successful device result matching the target updates local contact data.
Unknown outcomes preserve it. Do not delete a contact solely on an estimated result.
Run the current Alembic upgrades before deployment; do not pin an old migration head.

## Command bridge and validation

Authorized tenant operators use the existing channel-scoped interfaces:

```text
GET  /api/wechat-webridge/{channel_id}/capabilities
POST /api/wechat-webridge/{channel_id}/commands
GET  /api/wechat-webridge/{channel_id}/commands/{request_id}
```

The POST body contains `command_name`, `args` and `request_id`. The capability
response combines the server catalog, latest device capabilities and online state;
actual execution still depends on the phone. Modeled results update business data;
other commands retain a result summary rather than pretending the SaaS workflow
completed. These tenant interfaces do not replace the external [Open API](bridge-api.md).

Validate registration/handshake, identity confirmation, events, sending, uploads
and reconnection on each supported phone transport. Automated protocol/route tests
are separate from real-device acceptance. Keep raw captures and operational notes
local; use [the testing guide](../tests/README.md) for focused regression checks.
