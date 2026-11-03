# Presence

Writers update JSON under `wiki/presence/` (and optional MQTT retained topics).

| Field | Writer | Meaning |
|---|---|---|
| portal_online | Portal | Connected to the gateway WebSocket |
| bridge_online | Bridge | Grok voice session healthy |
| voice_turn | Portal | Koji voice turn active (duck may be on) |
| spotify_playing | Portal | Sideloaded Spotify in started state |
| now_playing_visible | Portal | now_playing panel up or duck hold |
| wiki_etag | Both | Last applied wiki manifest etag |
| ts | Writer | Timestamp |

Frame from Portal: `{"type":"koji_presence","presence":{...}}`.
