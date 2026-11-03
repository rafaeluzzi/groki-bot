<!-- SPDX-FileCopyrightText: 2026 Joseph Zhou -->
<!-- SPDX-License-Identifier: MIT -->
# Experimental Grok Bot login voice backend

The opt-in `gbot` backend reuses a signed-in Grok Bot desktop account to obtain
an ephemeral voice credential. It does not require a separate xAI API key.
Gemini remains the default, and existing firmware does not need to be flashed.

This integration uses internal APIs observed in Grok Bot 0.71.2 on macOS. It is
version dependent; account access, voice availability and usage limits still apply.
A login session must be renewed in the desktop app if it expires.

## Local setup

Use your own authorized test agent ID. Start the loopback login helper from the
`gateway` directory in one terminal:

```sh
STACKCHAN_GBOT_VOICE_BOT_ID="YOUR_AGENT_ID" node scripts/gbot_voice_session_bridge.mjs
```

The helper reads the active account from local application storage and macOS
Keychain. It does not save credentials or change the desktop account state.
It listens on `127.0.0.1:18771`, rejects cross-origin/wrong-host requests, and
permits credential minting, voice session planning, and explicitly enabled native
Bot routes. Extra harness tools remain disabled by default. Audio-only probes
force an empty tool catalog and never register desktop presence.

Create two mono PCM16 WAV fixtures, at 16 or 24 kHz: one asking the assistant to
count slowly, and one saying “Stop counting. Say only: interruption successful.”
Then run a gateway-only probe in a separate terminal:

```sh
VOICE_EVIDENCE="$(mktemp -d)"
STACKCHAN_GBOT_VOICE_BOT_ID="YOUR_AGENT_ID" STACKCHAN_GBOT_VOICE_LANGUAGE=en \
  uv run --all-extras python scripts/probe_gbot_voice.py \
  --input-wav fixtures/count.wav --interrupt-wav fixtures/interrupt.wav \
  --output-dir "$VOICE_EVIDENCE/direct"
```

For the device protocol path, use `scripts/probe_gbot_voice_device.py` with the
same arguments. It sends real 16 kHz Opus through `GbotVoiceProxy`, decodes the
24 kHz Opus output in a fake device, injects an explicit device `abort`, and
checks queue clearing, `tts.stop`, late old-response rejection and a new reply.
No physical robot or full gateway process participates in these probes.

## Switching and scope

Set `STACKCHAN_VOICE_BACKEND=gbot` and `STACKCHAN_GBOT_VOICE_BOT_ID=YOUR_AGENT_ID`
for an explicitly started gateway session. Restore `STACKCHAN_VOICE_BACKEND=gemini`
to use the existing Gemini configuration. Failures do not automatically switch
providers. The normal gateway USB opt-in defaults remain unchanged.

Additional settings are `STACKCHAN_GBOT_VOICE_URL` (loopback HTTP only),
`STACKCHAN_GBOT_VOICE_LANGUAGE` (`zh` by default, `en` for the fixtures), and an
optional `STACKCHAN_GBOT_VOICE_NAME`. Temporary voice secrets are sent only to
`wss://api.x.ai`, with the minted model bound to the connection URL; redirects
are rejected. Account RPCs use a fixed allowlist and server error details are redacted.

Both voice providers use the same device transport, wake gate, end-conversation
callback, keepalive, and playback queue. Grok uses its own audio connection and
server VAD; it does not create a Gemini network session or require its API key.
Audio connection recovery and credential renewal are not automatic.

## Local tools and native desktop messages

The shared local executor supplies enabled device, emotion, tracking, Mac and
optional external-task actions to both providers. Existing opt-in settings still
apply. Grok omits local tools that duplicate its native desktop interface. Grok normalizes dotted tool names on the
wire and dispatches them back to their original names. Provider-built-in Google
Search is not available through this adapter; queries may instead be delegated
to the configured desktop Bot, without claiming that a search already ran.

For native desktop handoff, configure **both** the gateway and helper with
`STACKCHAN_TOOL_BOT=assistant` and `STACKCHAN_TOOL_BOT_ID=YOUR_AGENT_ID`, with
`STACKCHAN_GBOT_VOICE_BOT_ID` pointing to that same agent. This enables native `send_task` and `search_conversations` plus call-scoped
presence and filtered event streams. Grok does not declare or execute the local
`ask_grokbot` wrapper and does not use a CLI or the older HTTP Bot bridge.
`end_the_call` uses the existing end-conversation callback; when present it
replaces the duplicate local `end_conversation` declaration. Portal screen
tools are local, not native harness tools. Expression is morph and say only:
`core_morph` / `morph` / `self.core.morph` and `core_say` / `say` /
`self.core.say` send one thin `type:core` frame on the device
WebSocket and wait for `{"type":"core","ok":true,"action":"..."}`. Other
unknown native tools stay disabled, including when the legacy
`STACKCHAN_GBOT_VOICE_TOOLS` setting is present. `STACKCHAN_CORE_FRAMES=0`
leaves the Portal tools undeclared. Audio-only diagnostics use
`local_tools=False`, which also disables desktop access and these frames.

The helper filters current-call task outcomes and new owner-authored desktop
messages. The audio bridge forwards them with the client's native
`async_update.add` envelopes (`work_landed` or `user_message`). It does not add a
competing `response.create`. Tool results arriving during an active response
wait for that response to finish. Task result announcements do not grant
permission to dispatch another desktop task: a new real user input is required.
Duplicate model call IDs are ignored. Ordinary interruption stops speech while
accepted desktop work continues. Stopping the bridge closes local listeners and
drops late results; it cannot retract a task already delivered to the desktop.

Applications can inject `voice_tool_executor_factory` when constructing the
proxy. It receives device, wake, tracking, end-conversation and announcement
callbacks and returns a shared executor. Private extensions can therefore remain
outside this repository. No private phone implementation is included.

The receive loop stays independent of ordered audio playback. Interruption
cancels old output callbacks and proxy PCM/Opus queues before a new reply can play.
Firmware playback buffers may still drain after `tts.stop`, and firmware that
stops capturing its microphone while speaking cannot provide acoustic barge-in
through this gateway change alone.

## Validation boundary

An earlier implementation baseline passed real cloud voice exchanges using
synthetic English audio, plus fake-device Opus and cancellation checks. Those
results establish gateway behavior, not physical microphone/speaker performance.
The earlier local baseline also received user acceptance for physical robot
conversation and touch-triggered interruption, without acoustic timing; this
does not establish purely spoken acoustic barge-in.
The port onto this repository's current main was validated separately with
251 voice-related Python tests, three Node helper tests and Ruff; the ported
checkout was not deployed to or acoustically tested on a physical robot.
The subsequent shared-tool/native-channel integration was checked offline with
fake tools and a loopback SSE server (283 Python tests and seven Node helper
tests passed, with Ruff): local dispatch, normalized schemas,
call-scoped native envelopes, typed-message deduplication, interruption,
late-result rejection, and provider lifecycle hooks. The helper's native
connection and transcript subscription were separately checked at handshake
level. A later user-originated deployed test established task acceptance, completion
arrival, a sent native update, an `async_update` model response and its first
output audio. The exact completion words were confirmed to play correctly on the physical
speaker (2026-10-18). Typed desktop-message delivery and full feature parity
remain unverified. See
[latency methods and measurements](VOICE_LATENCY_BENCHMARK.md).

### Current capability evidence

| Capability | Implementation and evidence | Still requires deployment validation |
| --- | --- | --- |
| Login-state audio and interruption | Earlier real cloud audio; current queue and proxy regressions | Acoustic barge-in on the selected firmware |
| Wake, idle, end, injected speech | Shared proxy lifecycle and offline callback tests | Current robot wake and touch experience |
| Local tool catalog and dispatch | Shared executor, schema/alias checks, dispatch and concurrency tests | Each enabled physical or desktop action |
| Native desktop task handoff | Native `send_task`; deployed user requests returned `served` / `accepted`, then completion events | Visible desktop delivery and broader task coverage |
| Desktop completion replies | Deployed completion → native update sent → `response.created` with `trigger=async_update` → first output audio; about 0.6 s from sent update to first audio in one observed sample; exact words confirmed on the physical speaker (2026-10-18) | Broader reliability |
| Desktop typed messages | Filtered own-user subscription and offline tests; upstream subscription errors still occur and reconnect independently | Live typed-message delivery; missed messages are not replayed |
| Private extensions | Injected executor factory; no private source in this repository | Host-specific extension tests and deployment |

No recordings, credentials, account identifiers or private device configuration
are included. Gateway licensing remains MIT; `gateway/LICENSE` is unchanged.


### Desktop dispatch diagnostic follow-up

A subsequent offline regression found that the adapter rejected extra arguments
even when the advertised schema allowed them. It now validates the declared
schema, then forwards only the canonical `request` field to native `send_task`.
This is a verified compatibility defect; it has not yet been established as the
cause of a particular missed desktop request.

Native harness results are now unwrapped and checked against the client protocol:
only `kind=served` with `output.relay=accepted` means the task was accepted.
Acceptance does not prove that a message is visibly delivered in the desktop UI.
Refused, unreachable, malformed and non-served replies become explicit safe
failures. Unlike the desktop client's early receipt flow, this adapter waits for
the actual relay result before acknowledging acceptance.

Diagnostic counters log only declared tool names, call-ID presence, integer user
turn counters, fixed rejection/result enums and local HTTP status codes. They do
not log task text, arguments, transcripts, IDs, credentials or raw results. An
internal `speak_text`/debug speech request is not new user authorization to send a
desktop task; test dispatch through an actual user-input path.


### Native tool priority

The observed native catalog is `send_task`, `search_conversations` and
`end_the_call`. Grok uses these native names instead of the temporary local Bot
alias. Shared instructions referring to `ask_grokbot`, including its old early
receipt promise, are removed only from the Grok provider's prompt; Gemini's
catalog and instructions are unchanged. The native task result still requires
`relay=accepted` before an acceptance announcement.

Conversation search is not a replacement for local browser search. Existing
hardware, emotion, tracking, Mac and separately configured agent actions remain
available because the native catalog has no equivalent. This change is checked
offline and does not itself establish desktop delivery or deployed behavior.

### Completion delivery queue follow-up

The native client classifies `async_update.add` completion notices as
orchestrated delivery. They do not reserve a manual `response.create` slot, and
a particular update is not guaranteed to produce `response.created`.
The adapter previously reserved that slot, which could block every later
completion after one silent update. It now keeps native delivery separate from
manual response requests. Completion notices waiting for user speech to end are
also flushed on `speech_stopped`, even without a later model response.

Offline regressions cover both stalls and verify that native updates never add
an extra `response.create`. Safe diagnostics distinguish queued, blocked and sent
updates, then model response triggers and first audio. A queued or sent update
alone does not prove that a result was spoken. These fixes do not yet establish
the cause of the initially reported missing completion narration. Deployed
native result delivery and audible feedback were subsequently confirmed on the
physical speaker (2026-10-18).

When an explicit manual response and a native completion are both pending, the
manual request is sent once first. Native notices wait for that response's
normal boundary. An already-requested manual response also holds native notices;
no additional response request is manufactured for the native notice itself.
This prevents a silent native update from starving an existing tool reply.

### Native channel availability

The audio connection remains usable while the separate desktop sandbox is
unreachable. Native presence and event-stream registration retry in the
background at five-second intervals. Desktop readiness requires both successful
presence registration and a connected event stream. Until then, `send_task` and
`search_conversations` return an explicit unavailable result without posting a
task; local audio and hardware tools remain available. Restoring the native
channel enables these tools automatically. Stopping voice cancels registration,
retry and event listening, and attempts bounded presence cleanup. No CLI or
alternate desktop transport is used as a fallback.


### README update completion

The root English, Chinese and Japanese READMEs and both gateway READMEs now
describe login-state Grok voice, native desktop tools, the retained Gemini/CLI
path, macOS prerequisites, explicit helper environment variables and the current
validation limits. The face-demo README's legacy forwarding note is also marked
as a Gemini path. This documentation update changes no runtime configuration,
firmware or service process.


The final documentation baseline passed 300 tests across the eight targeted
Python voice test files and ten Node helper tests, with optional dependencies
installed. These are targeted checks, not the entire gateway suite. README
command entry points, local documentation links and wording checks were also
checked; no gateway, desktop task or firmware action was started for the
README update.
