# MaiBot adapter

Connect one Fulcrum bot to multiple independently running MaiBot instances.
Each connection has its own chats, WebSocket, queue, token, and status.

## Set up a connection

1. In **Plugins → MaiBot adapter**, bind a bot and add a connection.
2. Enter the full MaiM WebSocket address, for example `wss://bot.example.com/maim/example-bot/ws`.
   Enable MaiM on the receiving bot. If it requires a token, add the same value to
   `maim_message.auth_token` on MaiBot and to the token field in Fulcrum.
3. Search the group or direct chat drop-down by name, note, or ID. The list uses
   synced chats and contacts. You can filter by channel account to distinguish
   chats with the same name. Save and enable the binding and the connection.

One connection owns each chat within the binding. Explicitly transfer a chat
to move it to another connection. Saving or reconnecting one connection leaves
the others running. Connection tokens are encrypted and never returned to the
browser. At the saved endpoint, leave the token blank to retain it. Changing
the URL requires a new token or an explicit clear. Redirects are not followed.
Testing a connection checks the WebSocket handshake without sending a chat message.

Existing single connections appear as the default connection and keep their
settings and chat scope. The first save stores the multi-connection format
without a database migration. Existing endpoints keep their legacy protocol
unless explicitly switched; new connections default to MaiM. You can also collect messages
without sending replies or limit group forwarding to mentions of the bot.

## WeChat messages

- MaiM receives synced nicknames, group display names, and native mentions.
  A typed `@name` stays text. Native mentions in replies can target synced
  members of the current group only; mentioning everyone is unsupported.
- Incoming quotes provide an excerpt. WeBridge and Guga support native
  replies quoting a message in the same chat. Guga displays mentions in
  quoted replies as plain text. iPad does not send native quotes.
- WeBridge and Guga can fetch attachments on demand. iPad uses the existing
  group attachment download flow. MaiBot waits about 22 seconds before
  falling back to a text placeholder when a file is unavailable.
- Mixed replies can contain text, images, stickers, and voice. WeBridge
  requires a device-accessible `PUBLIC_BASE_URL` for media. Guga follows the
  capabilities reported by the device. iPad can send images and voice but
  requires the optional audio encoder to send WAV. The bridge accepts PCM WAV
  for voice; other incoming encodings fall back to a placeholder. Each file
  is limited to 1 MiB. GIF images stay images unless marked as stickers.
- The channel delivery status summarizes each part of the latest reply.
  Phone queues wait for the previous part's result before sending the next.
  Assignment, configuration, and permissions are checked again before sending.

Other channels and the legacy API keep text behavior. Fulcrum routes replies
using internal chat identifiers from recent forwards and does not fetch
arbitrary media URLs or paths supplied by MaiBot.
