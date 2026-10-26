# Plan: remove the native audio helper

Status: proposal, 26 October 2026. Builds on the `experiment/direct-audio-routing`
branch (commit `74115c2`).

## Why this is possible now

The helper (`Dot Phone Call Helper.app`, launched by Chrome through native
messaging for each call) exists mainly because Dot's audio followed the Mac's
default output. Switching system devices needs native code. The experiment showed
Chrome can route both directions itself:

- Dot's page: the extension's document-start script routed Dot's 2 audio
  elements and 1 audio context to BlackHole 2ch with `setSinkId`. chatgpt.com
  allowed it; no errors.
- Hidden Twilio page: phone audio played straight to BlackHole 16ch. The no-phone
  check recovered 997 Hz there with zero frames passing through the helper.
- A live call worked in both directions, and the Mac's speakers and microphone
  stayed on their normal devices.

## What the helper does today, and what replaces it

| Helper duty (today) | Replacement |
| --- | --- |
| Switch default output to BlackHole 2ch and input to BlackHole 16ch, then restore them (`audio-routing`, `SNAPSHOT`) | Not needed. Dot's page routes its own output; the microphone hook already forces BlackHole 16ch. |
| Play phone audio into BlackHole 16ch (`LocalAudioPipe`, jitter buffer) | Hidden page plays it directly with `AudioContext.setSinkId`. WebRTC's own jitter buffer handles timing. |
| Pre-call audio check (`verify_local_audio`, tone loops) | In-browser check in the hidden page: tone to each BlackHole output, captured from the matching input. |
| Claim the job and report status (`pulse` busy, `update`, `cancel_call_id`) | Extension background sends the same companion requests itself. |
| Call sequencing (`run_registered_call`): prepare Dot tab, report ready, start Dot after answer, watch Dot, finish | Backend owns the call state machine; the extension executes idempotent steps (see below). |
| Cleanup when Chrome or the page dies | Mostly unnecessary: nothing system-wide changes. The backend already ends the Twilio call when the browser lease lapses (12 s), and Dot dies with Chrome. |
| Recovery mode after a crash (`recover`) | Extension boot ends any owned Dot session and tells the backend. |
| macOS microphone permission for its own audio checks | Chrome's existing permission. |
| No-phone diagnostics (`hidden-audio-test`, spoken `audio-test`) | Hidden-page self-test reported to the backend; drop the whisper-based spoken test. |

## Target design

The backend's `RegisteredCalls` record already tracks most of a call. Make it the
single source of truth, and let the extension background act on each pulse:

1. Job queued: the extension claims it with `pulse` (busy), prepares the Dot tab,
   and arms the microphone and output hooks.
2. The hidden page opens BlackHole 2ch through the voice shaping, selects
   BlackHole 16ch for playout, and posts `ready`. No audio socket or capability.
3. The backend dials (outgoing) or accepts the verified inbound leg, as today.
4. On `connected`, the extension starts Dot's voice and verifies microphone and
   output routing.
5. While connected, the extension inspects the Dot tab each second; the backend
   watches Twilio and the browser lease.
6. Any end (phone hangup, end tool, Dot hangup, cancel, timeout) runs one
   idempotent finish: end the Twilio call, release browser tracks, end Dot,
   restore Dot's output sinks, report the outcome.

Every step is safe to repeat. A Manifest V3 service worker can be stopped by
Chrome; on restart it rebuilds what to do from the backend's record instead of
in-memory state.

## Phases

### Phase 1: direct routing by default, helper still sequences

Small and shippable, and keeps today's safety net.

- Make direct routing the only mode; remove `--direct-audio`.
- Gate calls on output routing: if Dot connects but nothing is routed within a few
  seconds, fail with `DOT_OUTPUT_NOT_ROUTED` rather than risk silence.
- Remove the PCM worklet, audio socket, `LocalAudioPipe` playout and the playout
  jitter buffer. The helper's preflight and route switching go too.
- Live check: outgoing and incoming calls.

### Phase 2: move sequencing out of the helper

- Backend: drop the audio capability from `prepare`; add the explicit state
  transitions the extension needs; keep at-most-once dialing and the uncertain-
  result journal unchanged.
- Extension background: run the steps above from pulses, persist only the job id,
  and resume after a service-worker restart.
- Hidden page: add the in-browser pre-call check and report it with `ready`.
- Remove native messaging: `nativeMessaging` permission, `helper-client.js`
  helper launch, the `ws://127.0.0.1:8893` CSP entry.

### Phase 3: delete the helper and simplify setup

- Delete: `mac-bridge/` call code (`native_host.py`, `companion.py`,
  `registered_call.py`, `registered_probe.py`, `offscreen_test.py`,
  `audio_check.py`, `audio.py`, `phone_voice.py`, `resampling.py`, `bridge.py`,
  `browser_audio_check.py`, `registered_owner.py`, the probes, tunnel helpers,
  `opus_prototype.py`, `test-session.py`, `launcher.swift`, `routing.swift`) and
  their tests; `scripts/build-mac-app.py`, `install-native-host.py`,
  `test-native-audio-route.py`, `benchmark-audio-quality.py`,
  `analyze-opus-packets.py`; `prototype/`.
- Remove legacy Media Streams code paths (`src/inbound.mjs` public listener,
  `inbound-ready`) once nothing uses them.
- `verify-setup.py --audio` reads the hidden-page self-test from the backend.
- README: no helper build, native host, `swiftc` routing step, or helper
  microphone prompt. Python no longer needs to be 3.11: `audioop` lived only in
  `mac-bridge/audio.py`. Remaining Python needs are the supervisor and setup
  scripts (standard library) plus `twilio` and `requests` for incoming routing.
- Existing installations: finish any call, remove the old native-host file and
  helper app. If `.runtime/mac-audio-original.json` exists, restore devices with
  the current build first; the installer already refuses while it exists.

### Phase 4: validation

Unit tests for each step, then live checks:

| Scenario | Expect |
| --- | --- |
| Outgoing and incoming calls | Two-way audio; Mac devices unchanged |
| Hangup from the phone, the end tool, and Dot | One clean finish each; Dot output restored |
| Cancel while ringing | No Dot session; no stray call |
| 10-minute call | No service-worker gaps or dropouts |
| Headset plugged or unplugged during a call | Call unaffected (no system routing) |
| Music playing during a call | Not heard on the phone |
| Chrome or extension restart while idle | Registration and pulses recover |
| Two Dot tabs open | The selected tab is used; the other is untouched |

## Risks

- **Dot changes how it plays audio.** The hook covers audio elements and audio
  contexts today. The output-routing gate turns a future change into a clear
  failure instead of a silent call. The project already depends on Dot's page for
  its microphone and call buttons.
- **A brief leak at playback start.** `setSinkId` is asynchronous, so the first
  milliseconds of a new Dot player could reach the speakers. Measure in Phase 1;
  if audible, hold new players muted until routing resolves.
- **Service-worker suspension.** Mitigated by backend-held state, alarms, and
  idempotent steps; covered by the 10-minute test.
- **Weaker native safety net.** Acceptable because no system state changes. The
  at-most-once dialing guarantees stay in the backend.

## What remains after removal

The menu-bar app and supervisor, the Node backend, the OpenAI tunnel client, the
Chrome extension with its hidden Twilio page, and BlackHole 2ch and 16ch. Xcode
Command Line Tools are still needed to build the menu-bar app.
