# Media library

Open **媒体库** in the tenant navigation to manage files already stored by the
platform: incoming attachments, outbound uploads, generated/edited images and
downloaded Moments images/videos. The page does not fetch remote media or call AI
providers. It reuses existing files and attachment records; no schema migration
or separate storage service is required.

## Browse and download

- Filter by image, video, audio/voice or other file; source, account, filename and
  date. Results show size, time and a source conversation when it can be verified.
- All media appears in one responsive masonry layout. Images and video covers
  retain their aspect ratios; audio keeps its inline playback bar. Open a tile's
  **ⓘ** button for its filename, size, source, account, time, download and source
  links. The panel supports keyboard activation and Escape; selection stays in
  the opposite corner for batch cleanup.
- Image previews share the same viewer as messages and Moments. Use the mouse
  wheel or **＋ / −** buttons to zoom, then drag to inspect details. **复原** fits
  the image to the available window without enlarging small images; **1:1** shows
  one image pixel per CSS pixel. The percentage displays the current scale.
  Keyboard shortcuts are `+` / `-`, `0` for fit, `1` for original size, and Escape
  to close. Switching images resets the view; video playback keeps its controls.
- Video tiles lazily request a JPEG cover from the first decodable local frame.
  Content-identical videos within a tenant reuse the cover; existing files work
  on first view without redownloading. MP4/MOV with an ISO media header, WebM/MKV
  and AVI containers are supported. Unsupported or broken videos keep their play
  button and original download; cover failure does not block browsing.
- Covers require the installed FFmpeg runtime, accept at most 512 MiB input, and
  hash within a five-second budget. Decoding runs in a separate child for at most
  12 seconds, with two concurrent cover requests per application process and
  640-pixel / 2 MiB output bounds. Linux children also enforce memory, CPU and
  file-size limits. Fixed container parsers, restricted protocols and disabled
  external MOV references keep cover generation local. No AI request is made.
- Original downloads preserve the exact stored bytes and require the current
  tenant login. Every cover request rechecks its source ownership even when the
  derived image is already cached. Removing the final managed source reference
  also releases its cover.
- SILK voices, including the WeChat header variant, are decoded locally into a
  mono 24 kHz WAV on first playback. AMR uses the existing FFmpeg runtime. The
  tenant-scoped preview is cached by source content and decoder version, so
  identical recordings from different conversations reuse one converted file;
  already-downloaded historical voices work without redownloading. Original
  downloads always return the stored bytes, not the converted WAV.
- Conversion accepts at most 32 MiB input and runs for at most 20 seconds, with
  two concurrent conversions per application process. SILK rejects audio beyond
  10 minutes; AMR previews contain at most the first 10 minutes. Invalid, oversized
  or unsupported audio remains downloadable; failed conversion does not publish
  partial previews. No transcription or AI provider is used.
- Historical outbound files without AI provenance remain labelled as outbound
  files. Newly generated images carry generation/edit provenance and are retained
  even if subsequent delivery fails. Previously generated-but-never-saved images
  cannot be reconstructed from the catalog.
- Each listing inspects at most 10,000 records/directories. A visible warning
  identifies partial results and statistics; filter by account to reduce scope.
  Missing files and invalid paths are excluded. Files in plugin, diagnostic,
  backup and secret directories are never scanned.

## Automatic voice downloads

Enable the group's **自动下载附件** switch. New incoming image, voice and video
messages then use the existing channel download/upload worker. Voice messages
without text are archived with a placeholder and are eligible for downloading.
Phone uploads retain their original bytes with media-header format detection.
The media library sees successful downloads directly; there is no second fetch.

The switch remains off by default. Private conversations, self-sent messages,
expired events and disabled groups do not initiate automatic downloads. Existing
attachments reuse their current task; old history is not replayed. Downloading
still requires a supported, online device/channel and available source media.

## Built-in voice decoder

The main requirements include `silk-python==0.2.9` (import name `pysilk`). No
plugin, AI setting, model key or runtime download is needed. The Python 3.11
Docker image installs the decoder with the other requirements; SILK-to-WAV uses
Python's standard `wave` module and does not require FFmpeg. AMR preview and
compressed-audio encoding still require FFmpeg. Existing iPad voice encoding
uses the same packaged codec and its streaming API.

Preview drains all internal frames in each packet through the pinned SDK
bindings, including 40–100 ms multi-frame packets; it does not assume one
20 ms frame per packet or silently shorten those voices.

Do not install the retired PyPI package `pysilk` alongside `silk-python`: they
share an import name but have different APIs. For source installations that used
the old optional dependency, remove it and reinstall the built-in codec:

```bash
python -m pip uninstall -y pysilk
python -m pip install --force-reinstall silk-python==0.2.9
```

The native decoder runs in a separate child process, after authorization and
outside the database session. Input packet lengths and decoded PCM size are
bounded. On Linux, the child also has a 256 MiB address-space limit, CPU/file-size
limits and disabled core dumps. This is resource containment, not an OS sandbox.
Missing components, malformed input, timeouts or a crashed decoder leave the
original intact and produce a preview error rather than failing the web worker.
These controls do not upload voice data or call an external service.

## Cleanup and permissions

Readonly users can browse, preview and download. Only tenant administrators can
delete files, with CSRF validation and explicit confirmation. Select one file or
multiple files on the current page. The server accepts at most 100 references.

Cleanup permanently removes the selected local references, not
messages, audit history or phone/WeChat files. Other references to shared content
remain readable. The final managed reference releases its stored content and
derived preview. Unindexed historical references retain their original bytes
and can regenerate a preview if necessary. Attachment rows remain marked as
deleted. Files referenced by pending/running tasks or pending outbound messages
are protected, as are outbound files created within the last five minutes.
An invalid/protected selection prevents the batch from starting; later failures
are reported with the actual number deleted. Deletion is not reversible.

Every file request revalidates tenant, channel and related conversation ownership
and confines the file to its managed directory. Client-supplied paths, metadata
files, traversal and symlinks are rejected. Preview cannot execute active uploads
such as HTML or SVG. Temporary local preview generation never contacts a model.

## Shared storage and historical duplicates

New incoming attachments, inbound/outbound uploads, AI images and completed
Moments downloads share identical bytes within each tenant. SHA-256 selects a
candidate, followed by byte comparison before sharing. Original names, URLs,
conversation permissions and source records remain separate. Different tenants
never share stored objects. Similar-looking images with different bytes are not
merged.

Storage uses filesystem hard links under `DATA_DIR`, with tenant-private content
anchors in `tenants/<tenant_id>/media_objects`. All managed media paths must be on
one filesystem supporting hard links. Unsupported linking fails explicitly;
there is no copy fallback claiming savings. Writers publish by atomic replacement
and must never modify a shared file in place. Back up and restore the complete
data directory with hard-link relationships preserved to retain the savings.
The browser's storage figures count distinct files by filesystem identity within
the inspected, filtered catalog; they exclude previews, other directories and
filesystem allocation overhead.

For existing duplicates, a tenant administrator selects **扫描重复文件**, reviews
the source groups and estimated savings, then chooses **确认整理**. Scanning uses
the authorized catalog and does not modify originals. It covers all types for
the selected account, regardless of other listing filters. Each scan is bounded
to 1,000 records, 512 MiB of candidate content and a 10-second processing budget;
partial results are identified. Narrow the account filter when results are partial.

A plan belongs to the initiating user and tenant, expires after 30 minutes and
is replaced by that user's next scan. Confirmation requires administrator access
and CSRF validation. The server claims the plan once, then rechecks ownership,
pending-task protection, file identity and content for every reference. Changed,
missing or protected files are skipped. Results report actual replacements and
released file bytes; estimates can decrease if files change. An interrupted or
partly failed operation requires a fresh scan. This is an explicit maintenance
action, not an automatic historical cleanup or a background disk scan.
