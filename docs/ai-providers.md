# AI providers and model selection

**AI 供应商** manages tenant-owned OpenAI-compatible connections. Each record has
a name, API base URL, encrypted API key, enable switch and model directory. Use
the provider's API root, for example `https://models.example.com/v1`, not a full
`/chat/completions` path. Other API dialects are not supported in this release.

## Setup

1. A tenant administrator adds a provider and saves its key.
2. Use **刷新模型** to read `GET /models`, or enter model IDs/aliases manually,
   one per line. A directory request does not invoke a generation endpoint.
3. In **AI 助手**, choose provider/model pairs independently for conversation,
   [vision](ai-vision.md) and [drawing](ai-drawing.md). Listed models are suggested;
   unlisted aliases can also be typed directly. The model field searches the
   selected provider's catalog without opening a long native dropdown. It shows
   at most 12 matches and the total count; refine the search to find more models.
   Click a result or use the arrow keys and Enter to select it. Free-typed IDs are
   preserved when leaving the field, and a listed current model appears first
   when opening unfiltered suggestions. Changing the provider clears the old
   model choice, without changing other capabilities.
4. In a group's **AI 对话** settings, either inherit the tenant conversation
   provider or select a complete provider/model pair. A model-only override uses
   the inherited provider. Vision and drawing use their tenant-level selections,
   with separate group permission switches.

A successful model-directory response does not prove that a model supports
images, vision or any particular endpoint. Use the explicit text/vision tests
for those capabilities; they can incur provider charges. Saving settings or
refreshing the directory never generates a paid sample image.

## Credentials and access

Keys are encrypted at rest and never returned in HTML, model pickers or group
configuration. A blank key retains the existing key only when the API base is
unchanged. Changing the base requires a new key to enable the connection. To
remove a key, disable the provider and use the explicit clear control. Requests
carrying a provider key do not follow redirects.

Only tenant administrators can add, change, refresh or delete connections; mutations
also require CSRF and an active subscription. All lookups and selections are
tenant-scoped. Stale model refreshes cannot overwrite a changed connection.
Disabled, missing or foreign selections fail closed: no other provider's key is
borrowed. A provider referenced by any capability or group cannot be deleted;
change those selections first. Disabling a shared provider affects all its users.

## Upgrade

Apply the standard Alembic upgrade before starting the application. The migration
copies existing chat and vision ciphertext into separate imported connections,
preserves their model choices, and converts group endpoint/key overrides into
complete connections using their previous effective values. Model-only overrides
remain model-only overrides. Unrelated group settings are retained.

The old capability endpoint/key columns and group secret fields are removed,
not dual-written. Drawing stays off until explicitly configured and enabled.
Old drawing-plugin bindings and credentials are not imported or automatically
authorized. Schema downgrade restores the selected conversation/vision connection
fields; it cannot represent unused extra providers or drawing-attempt history.
Back up the database before migration, and never roll back and replay paid jobs
as though the provider had refunded or cancelled them.
