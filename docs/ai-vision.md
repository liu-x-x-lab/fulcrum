# Quoted image and video understanding

Vision is part of AI conversations, not a separately installed plugin. Quote an
image or video in the same conversation and ask a question. In groups, mention
the bot. Existing explicit plugin commands keep their normal dispatch priority.

## Configuration

Create a connection in [AI 供应商](ai-providers.md). In **AI 助手 → 独立视觉模型**,
select its provider and model, then enable **识别图片和视频**. The endpoint must support OpenAI-compatible
`/chat/completions` requests with inline `image_url` content parts. The connection
test sends a locally generated synthetic image; a successful text request alone
does not establish image support.

Vision selects its connection independently and never silently inherits text-model
credentials. A provider can be deliberately shared by multiple capabilities.
Encrypted keys, endpoint changes and explicit key removal are managed only in
**AI 供应商**; capability forms never contain keys. Disabling a provider stops
its dependent capabilities rather than switching them to another connection.

The tenant AI master switch and conversation AI-chat switch must also be enabled.
Each group's **AI 对话 → 视觉理解** switch can disable media analysis without
changing its text model. Group text-model overrides do not override the tenant
vision model. Vision is disabled by default after upgrading.

## Supported input

- Static JPEG, PNG, WebP and GIF: at most 16 MiB and 25 million pixels. Images are
  resized to fit 1536×1536, converted to JPEG and stripped of EXIF metadata before
  transmission. Animated images are explicitly rejected rather than silently
  analyzing only their first frame.
- MP4 video: at most 40 MiB, 120 seconds and 25 million pixels per frame. The
  existing FFmpeg/FFprobe runtime produces at most eight uniformly distributed,
  timestamped frames. **This is sampled visual analysis, not complete video or
  audio understanding.** Every video answer includes that limitation. Speech,
  music and events between sampled frames are not analyzed.

Media is uploaded only when requested, to the configured model provider. Enable
this capability only for groups that understand and accept that processing.
Media-derived interpretations are not automatically retained as personal facts;
the existing text-memory controls remain separate.

## Retrieval and failures

The platform resolves the exact native reference ID against its own message
archive, within the same tenant, channel and conversation. It does not guess the
latest picture, reuse another group's attachment or dereference an arbitrary
media URL supplied in a quote. Missing, ambiguous or recalled originals produce
an actionable error. If the original was never archived, send it again before
quoting it.

Downloaded attachments are reused. Otherwise, the existing WeBridge, iPad or
iOS channel media worker retrieves the original. Waiting reschedules the same
AI task for up to 150 seconds, without holding a database connection, occupying
a model thread continuously or issuing paid model requests. The automatic media
download switch does not have to be enabled.

Visual requests use the tenant AI allowance and existing outbound delivery path.
They do not fall back to the text model on failure, invoke tools based on image
text, or silently retry a paid model request. Download failures, missing FFmpeg,
invalid files and unsupported image inputs are reported explicitly. Self-sent
and expired messages retain the platform's archive-only behavior.

## Operations

Run the standard Alembic upgrade before starting the new application version.
The checked-in application Dockerfile already includes FFmpeg; non-container
installations must provide both `ffmpeg` and `ffprobe` on `PATH`. Runtime code does
not install dependencies. The model connection test, actual media extraction and
real-device message delivery are separate checks.
