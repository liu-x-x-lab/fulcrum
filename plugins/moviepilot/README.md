# MoviePilot movie downloads and subscriptions

A Fulcrum group-chat integration for MoviePilot V3. It uses existing REST APIs;
no MoviePilot plugin, source modification or webhook configuration is required.

## Setup

1. Upgrade Fulcrum and run its database migrations through
   `20261118_0044` before enabling version 1.1.0. The plugin ZIP alone does not
   install the required subscription and download ledger tables or models.
2. Enable the plugin for the tenant, bind it to a bot and enable it in the
   managed groups that should use it.
3. Set the MoviePilot root URL (for example `https://moviepilot.example.test`)
   and API Key in the bot's plugin settings. Both fields use encrypted binding
   storage, so a group cannot change the destination while inheriting the key.
   An empty secret input preserves the current value; use the clear control to
   remove it. Only configure trusted MoviePilot instances.
4. Enable download and transfer notifications. The platform checks every five
   minutes. The switch may be overridden per group.

The MoviePilot API Key grants broad upstream authority. Fulcrum exposes only
the commands below and enforces local registration ownership. Connection
changes, including key rotation, isolate old selections and registrations;
register watches again after changing the connection.

## Commands

| Command | Behavior |
| --- | --- |
| `MP帮助` | Show command help. |
| `搜电影 片名` | Search TMDB movies by name; return up to five choices. |
| `选电影 1` | Show the selected movie, description and poster link card. |
| `找资源 1` | Show up to eight resources with site, size, seeders and promotion labels. |
| `下载资源 2` | Select resource 2 from your latest resource list and receive a confirmation code. |
| `确认下载 abc123def456` | Recheck the selected resource and submit a direct download without creating a subscription. |
| `订阅电影 1` | Create a native MoviePilot movie subscription. |
| `我的电影订阅` | List your registrations, including direct-download notification watches, with recent resource-query snapshots. |
| `取消电影订阅 12` | Cancel a native subscription explicitly created by you through this binding and group. Existing downloads and files remain. |
| `关注电影 1` | Watch an existing native subscription, or resume muted notifications. Does not grant deletion rights. |
| `取消电影关注 12` | Mute notifications without deleting the native subscription or losing recorded ownership. |

Selections expire after ten minutes and are isolated by tenant, bot binding,
group, sender and upstream connection. `1` in selection commands is a search
result number; `12` in cancellation commands is the local registration ID
shown by `我的电影订阅`. Native MP subscription IDs are never accepted as local
authority. Search again to choose another movie. This first version supports
TMDB movies; television seasons and music are outside its scope.

**Native subscriptions download automatically according to MoviePilot settings.**
There is no group approval step for automatic subscription downloads. New
subscriptions also remain visible in MoviePilot. Direct resource downloads use
the explicit confirmation flow below.

## Direct downloads and promotions

Send `搜电影 片名`, then `找资源 1`, `下载资源 2`, and the exact
`确认下载 <code>` shown by the bot. Film and resource numbers refer to different
lists. Resource selections last ten minutes; confirmation codes last five
minutes and belong to the requesting member, group, binding and MP connection.
The confirmation shows the movie, resource title, site, size and promotion.

Confirmation calls the native `/download/add` API with the selected movie
identity and a freshly retrieved resource, using MP's default download settings.
It does not call the subscription creation API or change subscription filters.
Resources lacking verifiable movie/site identity cannot be submitted. If the
resource disappears, confirmation fails without submitting. A changed promotion
produces a new confirmation code, invalidating the old one.

**Free means zero download accounting; 50% means half of downloaded bytes count.**
Upload multipliers and promotion deadlines are displayed when available.
Missing or malformed factors display as unknown, and expired promotions are
not shown as currently Free. These are upstream snapshots, not a guarantee of
the tracker's eventual accounting; promotions can change after a check.
There are no Free-only or discount filters in this plugin.

`我的电影订阅` labels a recent resource snapshot with its query time: the first
result of a resource query, or the selected resource at direct submission.
It does not imply the entire movie has that promotion or that MP
selected it for automatic downloading. If no query snapshot exists, it shows
unknown. Existing native MP history does not include promotion factors: those
notifications explicitly show unknown. A matching direct-download hash can use
the ledger's submission-time snapshot instead; other torrents of the same movie
must not inherit it.

Direct downloads create a local movie watch for scoped history notifications,
without native subscription deletion rights. They appear as direct-download
records in `我的电影订阅`; `取消电影关注` mutes their notifications. Existing muted
watches stay muted. The same movie may subsequently be natively subscribed.

The tenant-owned download ledger blocks repeated submission of a resource in
the same binding/connection, including after a confirmation expires or a later
search returns it again. A timeout or malformed success receipt remains
uncertain and must be checked in MP; it is not retried automatically. This is
not a distributed transaction with MP: a process/database failure after an
external write can prevent its outcome from being persisted. Check MP before
manually retrying after such an incident. Different bindings are independent.
The plugin does not provide an automatic reset of an uncertain/submitted entry.

Only safe metadata and hashes enter selection state or the ledger. Torrent
URLs, passkeys and site cookies remain in memory during the refreshed request
and are sent only back to the configured MP instance.

## Ownership and uncertain results

The plugin checks for existing subscriptions before creation. Existing
subscriptions can be watched, but cannot be claimed for cancellation. The
creation response must explicitly report a new subscription before deletion
rights are granted; an existing or unfamiliar response only creates a watch.
Cancellation rechecks the remote identity, creation date and account, so reused
native IDs are not sufficient to authorize deletion.

Writes are not automatically retried. A timed-out creation or cancellation is
retained as an uncertain operation. Check the MP page before further action;
the plugin will not repeat the write. If MP accepted a subscription but the
response was lost, the plugin cannot safely infer who created it. An
administrator must reconcile the registration; restarting the process does not
turn an uncertain operation into a new write.

## Notifications

Only registered movies are announced in their corresponding enabled groups.
The initial baseline is collected before registration, so older history is not
replayed. Movie identity is matched by metadata source, ID and type, not title.
Multiple watchers in the same group share one outbound event. Disabled groups,
channels, bots and plugins do not receive new queued notifications.

Notifications distinguish **download submitted**, **transfer completed**, and
**transfer failed**. Download history is not evidence that downloading has
finished; successful transfer is the completion signal used here. Native
subscriptions may disappear after completion, while their registered movie
continues to receive download/transfer events until muted or cancelled.

Each run reads at most 500 records per history type. Excessive backlog is
reported without advancing the affected run's watermarks. Resolve it or
explicitly mute and resume a watch to establish a fresh baseline. Status changes
to already observed transfer records are detected in the fetched recent window;
very old in-place history changes outside that window are not guaranteed to be
observed. Recent event fingerprints are bounded to 500 per registration.

Queueing and watermarks share the platform transaction. Notifications use the
standard outbound queue with a tenant-scoped deduplication key and one delivery
attempt. A queued event is not proof of receipt; inspect platform delivery logs
for failures. Raw filesystem paths, torrent URLs, site cookies and API keys are
not included in messages or registration records.

The reload action is side-effect-free. When upgrading helper modules, restart
the Fulcrum application through its normal deployment process: the current
directory loader reloads the entry module only, not imported Python helpers.

## Validation

Use the isolated runner with `tests/test_moviepilot_plugin.py` and
`tests/test_plugin_migration_catalog.py`. Tests use mocked MP requests and
temporary SQLite, including ownership, duplicate notifications, uncertain
writes and migration upgrade/downgrade. Real subscription creation may trigger
downloads and must be a separate, deliberate acceptance check.
