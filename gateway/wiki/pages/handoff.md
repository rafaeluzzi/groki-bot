# Handoff

1. Closed command that matches on-device grammar (volume, pause/skip/stop/resume, a podcast play that matches one followed show, or "follow <show>") → finish on the Portal. Do not treat it as a Mac desktop task.
2. A play, open, or what's-new with no on-device match already reached the bridge. Call `podcast_open_spotify` with the show title or `number N`. If it says nothing matched, call it once more with the closest-sounding title it names, or ask which show and name 2 or 3 candidates. Do not say the request was not for you. Do not also open Mac Spotify.
3. If the utterance already reached the bridge LLM for a Portal system control → call `device_command` with a catalog `name` and `args`. Do not also change Mac volume or a desktop player.
4. Open conversation or multi-step work → full Grok. Use a panel when a list, a choice, a webview template, or now_playing fits. The panels page says which.
5. If on-device already handled the change, leave it there.
6. Presence: Portal publishes online / voice_turn / spotify_playing; bridge publishes bridge_online. Use presence for awareness, not as a second command path.
