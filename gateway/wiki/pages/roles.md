# Roles

Koji is one persona with two execution seats.

## On-device Koji (Portal)

Owns Portal system controls and local media:

- STREAM_MUSIC volume: percent, level, up, down, get
- Media transport: pause, resume, stop, skip next/previous (Portal Spotify first, else ExoPlayer/RSS)
- Podcast spoken intercepts: play / follow / offer-to-follow (stay on Portal; never Mac)
- Duck STREAM_MUSIC during a voice turn; restore when idle/Listening
- Compose now_playing while Spotify plays; hide only after pause/stop past hold
- Brightness (needs Write settings grant)
- open_app (`spotify`, `koji_settings`), show_settings, reconnect_bridge
- Local Core morph/say when framed on device

## Bridge Koji (iMac gateway / Grok login-voice)

Owns conversation and relay:

- Open talk and multi-step reasoning
- After reasoning, call `device_command` with a catalog name for Portal systems — never Mac volume, Mac media, or desktop Spotify for those asks
- `show_panel` / `dismiss_panel`
- Future Home Assistant / MQTT home control
- Mac tools only when the user names the Mac
- Failover path via the iMac router (bridge ↔ xAI)

Do not run two competing brains. The wiki is shared memory, not a third thinker.
