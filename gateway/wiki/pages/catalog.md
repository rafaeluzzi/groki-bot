# Device command catalog (pointer)

Canonical names live in Portal `DeviceCommandCatalog` and gateway `DEVICE_COMMAND_NAMES`.

Volume: `set_volume_percent`, `set_volume_level`, `volume_up`, `volume_down`, `get_volume`
Media: `media_pause`, `media_resume`, `media_stop`, `media_skip_next`, `media_skip_previous`
Display: `set_brightness_percent`, `brightness_up`, `brightness_down`
Apps: `open_app`, `show_settings`
Connectivity: `reconnect_bridge`

Wire: `{"type":"device_command","name":"...","args":{...}}` → on-device execute → optional `device_command_result`.
See `android/docs/device-commands.md` and gateway `PORTAL_DEVICE_COMMANDS.md`.

## Podcast open

`podcast_open_spotify` takes `show`: a followed title, or `number N`.

N counts from the top of the "Podcasts I follow" panel. That panel and `GET /api/podcasts/followed` are both A-to-Z by title. A hit is one `{"type":"podcast","announce","openSpotifyUri","openSpotifyUrl"}` frame. The Portal speaks and opens Spotify. Do not also open Spotify on the Mac.
