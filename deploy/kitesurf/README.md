# Kitesurf screenshot service

Fulcrum uses Kitesurf as a separate screenshot service over CDP. This directory
provides its container build, runtime configuration and application network setup.
The build pins upstream commit `39feadfb9a3e6c14b40cf170a559ce417d0fce5d`.

## Start the service

Run from the repository root:

```bash
docker compose -f deploy/kitesurf/docker-compose.yml up -d --build
curl --fail http://127.0.0.1:8787/health
```

The first build downloads Node dependencies, Bazel, Rust and WASM tooling. The
runtime image uses the compiled artifacts. Review the pinned upstream version
and `wrangler.fulcrum.jsonc` together when upgrading.

Use `KITESURF_PORT` to change the host port. Keep CDP bound to loopback: this
endpoint has no application authentication. Application screenshot requests must
use Fulcrum's authenticated tool API. Keep private-network access disabled.
Adjust the container's CPU and memory limits to suit the host and total worker
concurrency.

## Connect Fulcrum

For a Python application running on the host, set these values in its private
environment file:

```dotenv
KITESURF_WS_URL=ws://127.0.0.1:8787/devtools/browser
KITESURF_TIMEOUT_SECONDS=45
KITESURF_MAX_CONCURRENCY=2
```

For Docker, start the screenshot service first, then include the network override
whenever creating or updating the application:

```bash
docker compose --env-file .env.docker -f docker-compose.yml -f deploy/kitesurf/app-compose.yml up -d --build --no-deps app
```

The override connects the application to the screenshot network and sets
`ws://kitesurf:8787/devtools/browser`. Keep using the normal deployment environment
and database migration procedure. Use `--no-build` when deploying a previously
built and validated image.

## API and troubleshooting

Use `app.services.web_screenshots.capture_screenshot(ScreenshotRequest(...))`
with trusted tenant scope, or `POST /api/tool/screenshots` with a tool API key and
`X-Tenant-Code`. See the [plugin guide](../../docs/plugin-development.md).

- `screenshot_not_configured`: configure the application connection URL.
- `screenshot_busy`: the application worker's concurrency limit has been reached.
- `screenshot_timeout` or `screenshot_unavailable`: check service health and network access.
- `screenshot_too_large`: reduce the page dimensions or request a viewport capture.

The API returns image bytes, dimensions, format and elapsed time. It does not
forward browser login cookies or accept arbitrary authentication headers. For
protected application content, render authorized HTML and submit it in HTML mode.
After a deployment, verify an authenticated screenshot request and inspect its
image; a running container alone does not establish rendering success.
