# Handoff

1. Closed command that matches on-device grammar (volume, pause/skip/stop/resume, podcast play/follow) → finish on the Portal. Do not treat it as a Mac desktop task.
2. If the utterance already reached the bridge LLM → call `device_command` with a catalog `name` and `args`. Do not also change Mac volume or a desktop player.
3. Open conversation or multi-step work → full Grok. Use panels when a list / now_playing / choice / webview fits.
4. If on-device already handled the change, leave it there.
5. Presence: Portal publishes online / voice_turn / spotify_playing; bridge publishes bridge_online. Use presence for awareness, not as a second command path.
