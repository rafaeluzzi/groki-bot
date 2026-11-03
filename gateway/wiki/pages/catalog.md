# Device command catalog (pointer)

Canonical names live in Portal `DeviceCommandCatalog` and gateway `DEVICE_COMMAND_NAMES`.

Volume: `set_volume_percent`, `set_volume_level`, `volume_up`, `volume_down`, `get_volume`
Media: `media_pause`, `media_resume`, `media_stop`, `media_skip_next`, `media_skip_previous`
Display: `set_brightness_percent`, `brightness_up`, `brightness_down`
Apps: `open_app`, `show_settings`
Connectivity: `reconnect_bridge`

Wire: `{"type":"device_command","name":"...","args":{...}}` → on-device execute → optional `device_command_result`.
See `android/docs/device-commands.md` and gateway `PORTAL_DEVICE_COMMANDS.md`.
