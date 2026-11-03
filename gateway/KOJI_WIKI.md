# Koji wiki

Shared memory for on-device Koji (the Portal) and bridge Koji (this gateway's Grok login-voice session). The pages under `gateway/wiki/` are the source. The Portal keeps a cache. This is not a second model, and it does not add Mac volume, Mac media, or desktop Spotify tools. Portal system asks still go through `device_command` ([PORTAL_DEVICE_COMMANDS.md](PORTAL_DEVICE_COMMANDS.md)).

MQTT retained topics are optional later. HTTP and the existing device WebSocket are enough for v1. The gateway does not publish a `koji_wiki` push frame; the Portal pulls.

## Pages

```
gateway/wiki/
  manifest.json
  pages/roles.md
  pages/handoff.md
  pages/presence.md
  pages/catalog.md
  presence/portal.json    # runtime, last Portal heartbeat
  presence/bridge.json    # runtime, Grok voice session health
```

`manifest.json` lists each page id and its path. Etags are computed when the file is read, so an edit does not need a hand-written hash. `presence/*.json` is not source (those names are gitignored).

Edit the Markdown in this checkout. The next Grok login-voice session loads `roles.md`, `handoff.md`, and `catalog.md` when it is configured. `presence.md` is the field notes for humans and for the Portal cache. It is served over HTTP and is not pasted into the model prompt.

`STACKCHAN_KOJI_WIKI_DIR` points at another directory with the same layout. The default is `gateway/wiki` next to the `stackchan_mcp` package.

## Voice instructions

On session configure, the gateway reads those three pages from disk and appends them through the same `ToolExtension.instructions` path as `DEVICE_COMMAND_INSTRUCTIONS` and `PANEL_INSTRUCTIONS`. There is no wiki tool.

If `presence/portal.json` exists, one line is added, for example `Presence: Portal online; Spotify playing.` The raw JSON is not sent.

`STACKCHAN_KOJI_WIKI=0` leaves that block out. The default is on. The flag does not turn off HTTP or presence files.

## HTTP

The routes live on the capture server, the same process and port as `GET /debug/voice-health`. `HOST` defaults to `0.0.0.0` and `CAPTURE_PORT` defaults to `8766`.

The Portal fetches, with the iMac's LAN address:

```
GET http://<iMac-LAN-IP>:8766/wiki/manifest
GET http://<iMac-LAN-IP>:8766/wiki/pages/roles
GET http://<iMac-LAN-IP>:8766/wiki/pages/handoff
GET http://<iMac-LAN-IP>:8766/wiki/pages/presence
GET http://<iMac-LAN-IP>:8766/wiki/pages/catalog
```

No bearer token. Read-only. A page response is `text/markdown` with an `ETag` header. The manifest is JSON:

```json
{
  "version": 1,
  "etag": "\"sha256-<hex>\"",
  "pages": [
    {"id": "roles", "path": "pages/roles.md", "etag": "\"sha256-<hex>\""}
  ]
}
```

A page etag is the SHA-256 of that file's UTF-8 bytes. The document etag covers `version` and each page id plus page etag, in manifest order. Compare `etag` and refresh the cache when it changes. An unknown id, or a missing tree, is HTTP 404.

## Presence frame

The Portal sends this on the same WebSocket it already uses for `type:panel` and `type:device_command` (default `ws://<iMac-LAN-IP>:8765`). Fire-and-forget. There is no ack.

```json
{"type":"koji_presence","presence":{"portal_online":true,"voice_turn":false,"spotify_playing":false,"now_playing_visible":false,"wiki_etag":"\"sha256-...\"","ts":"2026-11-03T18:00:00Z"}}
```

| Field | Meaning |
|---|---|
| `portal_online` | Portal WebSocket is up |
| `bridge_online` | Accepted if the Portal sends it; the gateway's own copy is `bridge.json` |
| `voice_turn` | Koji voice turn is active |
| `spotify_playing` | Sideloaded Spotify is started |
| `now_playing_visible` | now_playing is up, or duck hold is on |
| `wiki_etag` | Last manifest etag the writer applied |
| `ts` | Writer timestamp. ISO-8601, UTC `Z` or a named offset. If omitted, the gateway fills UTC `Z` |

Every field is optional, and at least one must be present. Booleans are JSON `true` / `false`. The two strings are at most 200 characters and have no control characters. Any other key, or any extra top-level key, is not stored. The frame is still not user speech: it is not forwarded into the voice turn, same as inbound `type:panel` and `type:device_command_result`.

A valid body replaces `wiki/presence/portal.json`. The file is the last heartbeat, not a merge of earlier ones.

When the Grok login-voice session is up, the gateway writes `wiki/presence/bridge.json`:

```json
{"bridge_online": true, "wiki_etag": "\"sha256-...\"", "ts": "2026-11-03T18:00:00Z"}
```

`bridge_online` is false when that session drops. `wiki_etag` is omitted when the manifest cannot be read.

## MQTT later

Not implemented. The same payloads can be mirrored as retained topics: `grokportal/wiki/manifest`, `grokportal/presence/portal`, `grokportal/presence/bridge`.

## Restart

The login helper does not read the wiki and does not need a restart for this. After pulling, restart the Python gateway LaunchAgent (usual label `com.stackchan.gateway`):

```sh
launchctl kickstart -k "gui/$(id -u)/com.stackchan.gateway"
```

The Portal reconnects its WebSocket when the gateway is back, then pulls `/wiki/manifest`.
