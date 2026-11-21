# Image Source Search

Enable the plugin on a bot and in the intended groups. Set a binding-level
Google Cloud Vision API key restricted to the Vision API to search for matching
web pages (including cosplay and illustrations). Configure quota and billing at
the provider. The optional trace.moe fallback searches animation frames if web
search yields no results or is unavailable. Without a key only anime search runs.

Reply to an archived inbound image with `找鸡`, send `牛牛找鸡 <public-image-url>`,
or use `找鸡 帮助`. Quoted images require supported WeChat quote metadata and a
downloaded attachment in the same conversation. Enable group media download for
this workflow. Where only a native server ID is available, the latest 200
inbound images in the conversation are checked. Other channels can use URLs.

Only explicit commands upload a resized image, without its original filename or
metadata. JPEG, PNG, GIF (first frame) and WebP are accepted up to 8 MiB and
16 million pixels. Results link to matching pages, not verified first publishers
or identities. User queries are limited to three per minute per binding.

Interaction inspired by `ttlvip/astrbot_plugin_fish_find_chicken`; this is an
independent Fulcrum implementation with no upstream code or bundled assets.
