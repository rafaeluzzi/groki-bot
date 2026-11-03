# Portal device commands (catalog v1)

`device_command` is a Grok login-voice tool. It sends one JSON text frame on the WebSocket the Portal already uses for `type:panel` frames. It does not use MCP, and it does not open another transport.

Portal owns system volume, screen brightness, media transport (pause, resume, stop, skip), and opening an app. The bridge model calls `device_command` for those. It does not call Mac `set_volume`, `media_control`, or `open_app`, and it does not change desktop Spotify or the Mac output volume.

The Portal handlers that accept this frame were not in this repository when the tool was added. This note is the contract those handlers should implement. If a later catalog changes a name or an argument key, update this note, `DEVICE_COMMAND_NAMES` in `stackchan_mcp/device_commands.py`, and the tool description together.

## Frame

```json
{"type":"device_command","name":"set_volume_percent","args":{"percent":50}}
```

- `type` is always `device_command`.
- `name` is one catalog id from the table below, lowercase.
- `args` is always an object. Commands with no parameters use `{}`.
- The frame has no `session_id`. The gateway does not send `abort` with it and does not change Opus audio or the voice session.
- There is no ack. `ok` from the tool means the gateway checked the call and wrote the frame.

`get_volume` uses that same fire-and-forget frame. The gateway does not wait for a result and does not return a volume number. The tool result is `{"ok":true,"name":"get_volume","reading":"on_device"}`. A reading stays on the device. If the device later sends `{"type":"device_command_result",...}` or any inbound `{"type":"device_command",...}`, the gateway drops that text and does not give it to the model.

## Catalog v1

| `name` | `args` | Example |
|---|---|---|
| `set_volume_percent` | `{"percent": <int 0..100>}` | "50%" → `{"percent":50}` |
| `set_volume_level` | `{"level": <int 0..100>}` | a step the device maps onto its scale |
| `volume_up` | `{}` | one step louder |
| `volume_down` | `{}` | one step quieter |
| `get_volume` | `{}` | on-device only; no number comes back |
| `media_pause` | `{}` | |
| `media_resume` | `{}` | |
| `media_stop` | `{}` | |
| `media_skip_next` | `{}` | |
| `media_skip_previous` | `{}` | |
| `set_brightness_percent` | `{"percent": <int 0..100>}` | |
| `brightness_up` | `{}` | |
| `brightness_down` | `{}` | |
| `open_app` | `{"id": "<app id>"}` | `spotify` |
| `show_settings` | `{}` | |
| `reconnect_bridge` | `{}` | |

`open_app` `id` is 1 to 64 characters: a letter or digit, then letters, digits, `.`, `_`, or `-`. The gateway forwards it unchanged and does not launch a Mac app.

Unknown names, extra keys, and values outside those ranges are not sent.

## Tool

Declared name: `device_command`.

| Argument | Required | Meaning |
|---|---|---|
| `name` | yes | one catalog id |
| `args` | no | the object above; omit it for commands that take `{}` |

`portal_device_command` and `self.device.command` are accepted if a session catalog lists them. They send the same frame and are not declared as extra tools.

`STACKCHAN_DEVICE_COMMANDS=0` leaves the tool undeclared and sends no `type:device_command` frames. The default is on for the Grok login-voice path.

## Restart

The Python gateway writes the frame. The login helper LaunchAgent (`com.stackchan.gbot-voice`) does not forward it, so this change does not need a helper restart.

On the iMac, restart the gateway LaunchAgent after pulling this gateway (the usual label is `com.stackchan.gateway`):

```sh
launchctl kickstart -k "gui/$(id -u)/com.stackchan.gateway"
```

Confirm the label with `launchctl print "gui/$(id -u)/com.stackchan.gateway"`. The plist is usually `~/Library/LaunchAgents/com.stackchan.gateway.plist`, and it runs `gateway/scripts/run_stackchan_gateway_launchd.sh`. Pull first, and keep the checkout at the path that plist uses. The Portal reconnects its WebSocket when the gateway is back.
