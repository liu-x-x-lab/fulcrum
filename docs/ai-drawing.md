# Native AI drawing and image editing

Drawing is an AI assistant capability, not an installable plugin. It uses the
existing AI intent workers, media download pipeline and standard outbound replies.
The former `ai_gen_image` plugin is retired; historical records are retained but
its credentials and enablement are not silently reused.

## Enable and use

1. Add an image-capable OpenAI-compatible connection in [AI 供应商](ai-providers.md).
2. In **AI 助手 → 绘图模型**, select its provider/model and enable drawing. Set
   the tenant-wide daily attempt limit (1–200, default 20).
3. Enable the AI master switch and conversation chat. Each group also requires
   **AI 对话 → 允许绘图和引用改图**, which defaults off.
4. Mention the bot in a group and send `画图 一只坐在窗边的猫`. In a direct
   conversation, send the same request without a mention.
5. Quote an image in that conversation and send `改图 把背景换成蓝色`.

Explicit commands are recognized without a separate model-classification request.
Ordinary image questions remain [vision requests](ai-vision.md); talking about
drawing does not itself authorize drawing. Descriptions must contain 1–4000
characters. Existing plugin commands retain their dispatch priority.

## Provider contract

The connection must implement the GPT Image-compatible `POST /images/generations`
and multipart `POST /images/edits` endpoints. Requests use one image, `1024x1024`,
`medium` quality and PNG output. The response must contain exactly one image as
`data[].b64_json` or, when Base64 is absent or empty, `data[].url`. Invalid Base64
is rejected rather than falling back to a URL. The `response_format` parameter is
not sent. A directory listing is not evidence of this endpoint contract.

Paid requests have no automatic retries or redirects, a 180-second total budget
and bounded response size. URL results are downloaded separately using the shared
public-resource reader: only public HTTP(S) targets, validated IP connections,
hostname-verified TLS, at most three revalidated redirects and no provider keys
or cookies. The download is limited to 16 MiB with the reader's socket timeout.
Download failures never start another generation request and do not expose signed
URLs or upstream response bodies in user-facing errors.
Static PNG, JPEG and WebP images are decoded and validated, limited to 16 MiB and
25 million pixels, stripped of metadata and re-encoded as PNG. References fit
within 2048×2048; outputs fit within 4096×4096. Transparency is retained.

## Exact references and privacy

Editing uses the exact archived reference in the same tenant, bot, channel and
conversation. It never guesses the newest image or converts a missing reference
into an unrelated text-to-image request. Quoted outgoing images are supported
only when their exact archive IDs and existing channel media retrieval are
available. If the original was not archived or cannot be downloaded, resend it
before quoting. Videos, animated images and recalled originals are rejected.

The existing media worker may wait up to 150 seconds for a download without
invoking the paid model. Media is sent to the selected drawing provider only on
an explicit request; enable this only for groups that accept that processing.
Image editing does not require the separate vision capability to be enabled.

## Charges, recovery and delivery

A unique tenant/message attempt is committed before each paid request. Attempt
reservation and the daily allowance are serialized per tenant. An uncertain
timeout, worker restart or response-processing error does not authorize a second
request for that message. Failed/uncertain requests consume the attempt allowance;
it is a safety limit, not a provider billing meter. A new message is a new request
and may incur another charge.

Tenant, bot, channel, conversation, recall, expiry and capability switches are
checked again before calling and sending. Dry-run events never invoke a paid
image endpoint. Network waits do not hold database sessions. Delivery failure
does not redraw; ordinary outbound processing handles the generated reply.
“Queued” means accepted by the outbound path, not confirmed receipt on a device.
Generated images entering delivery are retained in the [media library](media-library.md)
with generation/edit provenance. Queueing failures do not automatically regenerate
the image or erase its saved local copy.
Production provider compatibility, download availability and real-device receipt
must be verified separately from local mocked tests.
