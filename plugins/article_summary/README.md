# Article link summaries

Enable the tenant AI model, bind **文章链接摘要** to a bot, and enable it for the intended groups. The plugin is disabled by default. No mention or command is needed. It reads the first eligible HTTP(S) link in a message or an article share card and sends a short Chinese summary back to that group. It does not process private chats or transcribe videos.

The optional **摘要模型** field selects a model on the same tenant endpoint for article summaries only. Leave it blank to inherit the tenant model, or set it in one group's plugin settings without changing normal chat or other groups. The endpoint and credentials cannot be overridden by this field. The model timeout defaults to 30 seconds and can be set to 5–60 seconds in the same settings.

The reader opens the original link from a shared article card with anonymous desktop-browser-compatible HTTP headers. WeChat Official Account cards do not need to contain the article body: the plugin extracts it from the returned page. No separate skill, browser installation, cookies, login or external extraction service is required.

Only publicly readable HTML is supported. Downloads are limited to 8 MiB to accommodate article pages with large embedded scripts; scripts, navigation and recommendations are excluded from the extracted article. Each redirect is checked for a public destination and the request has a 12-second total deadline. Only the title and up to 12,000 characters of article text are sent to the selected model. Pages requiring verification, login or payment, deleted articles and unsupported pages are not summarized. It does not write to long-term memory. Inform group members before enabling automatic reading.

Per group and binding, up to six link messages per ten minutes are considered. The same URL is attempted at most once per thirty minutes, including unsuccessful reads. This prevents simultaneous shares from triggering duplicate model calls. Only hashed identifiers and counters are kept for this purpose.
