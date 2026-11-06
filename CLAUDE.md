# Groki Bot firmware: development notes

Firmware for the Groki Bot desktop robot (ESP-IDF 5.5, C++20). Main board: M5Stack CoreS3; also builds for StopWatch, AtomS3R and AtomS3.

## Build

```sh
git submodule update --init --recursive
tools/apply-m5-patches.sh          # one-line fix to M5Unified, see patches/
make set-target BOARD=cores3       # first time per board
make build BOARD=cores3
make flash BOARD=cores3 PORT=/dev/ttyACM0
```

`make build-docker BOARD=cores3` builds inside `espressif/idf:v5.5.4` without a local IDF. Node.js 18+ must be on `PATH`: CMake compiles `assets/*.avdsl` to bytecode with `tools/avatar_dsl/`.

Host unit tests live under `components/*/test/host/` and run in CI (`.github/workflows/host-tests.yml`).

## Gateway (`gateway/`)

Python gateway for the computer side (Gemini Live voice, wake word, stdio MCP server, Grok Bot forwarding service). MIT licensed, separate from the BSL-1.0 firmware; keep `gateway/LICENSE` unchanged.

```sh
cd gateway
uv sync --all-extras
NO_PROXY="*" uv run pytest -q
```

Koji Portal work (device commands, panels, wiki): follow `AGENTS.md`.

Never let the gateway take the robot's serial port in tests or local runs: set `STACKCHAN_USB_DISABLE=1` and leave `STACKCHAN_USB_TRANSPORT` unset. New gateway files use `SPDX-License-Identifier: MIT`. Optional features (Mac control, device tools, Grok Bot hand-off via `STACKCHAN_TOOL_BOT`) stay off by default. How the parts talk to each other: `docs/architecture.md`.

## License headers

- Files that came from upstream keep their `SPDX-FileCopyrightText: 2026 Kenta IDA <fuga@fugafuga.org>` line. Do not remove it.
- New files use:
  ```cpp
  // SPDX-FileCopyrightText: 2026 Joseph Zhou
  // SPDX-License-Identifier: BSL-1.0
  ```
- `main/aora_ring_data.hpp` and `main/aora_face.hpp` carry Emotion Ball data under a non-commercial license. See `third_party/emotion-ball/` and `THIRD_PARTY_NOTICES.md`.
- Add every new third-party dependency to `THIRD_PARTY_NOTICES.md` and `docs/licenses.html`.

## Coding conventions

- C++20. Declare `target_compile_features(${COMPONENT_LIB} PUBLIC cxx_std_20)` in each component.
- Errors propagate with `tl::expected` (`#include <tl/expected.hpp>`); no exceptions. Use `std::optional` for optional values.
- Pass non-null references as `T&`. Own with `std::unique_ptr` / `std::shared_ptr`; raw pointers are non-owning views only.
- Byte buffers: `std::vector<std::uint8_t>` on the heap, `std::array<std::uint8_t, N>` on the stack. Integers from `<cstdint>`, indices as `std::size_t`.
- Format with the root `.clang-format` (LLVM based, 4 spaces, 120 columns).
- Use M5Unified for board access. Prefer IDF `i2c_master` / `esp_driver_uart` over Arduino-style APIs.

## Components

| Component | Role |
|---|---|
| `components/board` | Board bring-up (`M5.begin()`, PY32 IO expander for servo power) |
| `components/scs_servo` | SCS0009 servo driver and trapezoidal `PathGenerator` |
| `components/avatar`, `components/avatar_vm` | Face rendering: expression controller and the bytecode VM that runs `assets/*.avdsl` |
| `components/groki_motion` | Head motion springs, touch and IMU input mapping |
| `components/conversation` | OpenAI Realtime, Gemini Live and XiaoZhi clients |
| `components/config_service`, `components/wifi_config_service` | BLE and Wi-Fi settings, OTA, HTTP API |
| `components/jtts`, `components/hts_engine` | On-device Japanese speech synthesis |
| `main/` | Task startup (render, servo, conversation) and shared state |

## Servo power sequence

Boot with `board.set_servo_power(false)`. To enable, call `set_servo_power(true)` and wait 200 ms before using `ScsBus`; the servos do not answer until the bus voltage is up.

## Hardware constants (CoreS3)

- Internal I2C: AXP2101 PMIC 0x34 and touch are managed by M5Unified; PY32 IO expander 0x6F (pin 0 = servo power enable).
- Servo bus (SCS0009): UART1, TX GPIO6, RX GPIO7, 1 Mbps 8N1. Yaw ID 1 (zero 460), pitch ID 2 (zero 620). 1 step is about 0.3125 degrees (`deg = (raw - zero) * 5 / 16`).

## Releases

Push a `vX.Y.Z` tag; `.github/workflows/release.yml` builds all boards and publishes a GitHub Release. Then run `pages.yml` so the web flasher and on-device update list pick it up. Steps: `.claude/skills/release/SKILL.md`.
