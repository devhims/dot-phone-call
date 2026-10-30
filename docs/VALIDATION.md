# Local integration validation

Validated on 25 October 2026 in `dot-phone-call`, using its own dependencies, extension identity, native helper, and local service.

## Automated checks

- 83 JavaScript tests pass, including registered incoming/outgoing lifecycles, PIN and caller checks, no-phone gating, duplicate suppression, cancellation, token renewal, automatic bridge startup, and session rotation.
- 116 Python tests pass, including authenticated 48 kHz PCM transport and incoming/outgoing helper cleanup with mocked devices.
- Native wrapper compiled, signed, and verified. Audio-routing utility compiled.
- Provider calls in the automated tests are simulated. The native lifecycle tests do not change hardware audio.

## Live checks without calling a phone

- Chrome registered the new project's independent client with Twilio without a user click or microphone capture.
- Loading the new extension automatically launched its separate native helper and started a local Dot voice test.
- Dot heard the challenge words `silver, garden, dolphin` and replied `Dolphin Garden Silver.` The speech checker verified the expected response.
- The native result reports `verified: true`, `dialed: false`, `cleanup_ok: true`, and `audio_restored: true`. The diagnostic took 39.75 seconds, including spoken instructions and recognition. Normal calls do not run this spoken diagnostic.
- The helper reported exit. macOS input/output were verified as MacBook Pro Microphone and MacBook Pro Speakers afterward.
- The first backend restart exposed a stale browser-session bug. Updating only the URL fragment did not reload credentials. The page now reloads on a session-fragment change, with a regression test. The idle browser now uses server-driven heartbeat events instead of background-tab timers. The final repeat restart reconnected automatically in 3.06 seconds without a user click. The result is saved in `.runtime/registration-test-result.json`.

Private diagnostic details are in `.runtime/native-helper-result.json` and `.runtime/browser-audio-test-result.json`. They are ignored by Git. The diagnostic exercises Dot and BlackHole audio locally; it is not an end-to-end phone-network audio test.

## Initial isolation checks and later cutover

The original project's 115 tracked/non-ignored source files and Git status matched the pre-work snapshot. Its native host, backend service, ChatGPT connection, and Twilio number routing were not changed. Only separately named new-project services and a new Twilio client API key were created.

At the end of the initial integration validation, `calling_enabled` and `allow_phone_dialing` were both false. No phone-number call was placed. The new incoming TwiML Bin has not been assigned to the Twilio number, because that would replace the existing incoming handler. The new local MCP endpoint also needs its own ChatGPT tunnel connection before Dot can invoke these tools directly.

Actual incoming and outgoing phone-network behavior after that cutover is unverified in this project. Those live phone checks require a later explicitly requested call. Global output routing still includes other Mac audio; isolation of Dot's output is a separate issue.

The subsequent tunnel connection and incoming-route switch are recorded in [CUTOVER.md](CUTOVER.md). Incoming and outgoing calling are now enabled following explicit owner approval. No phone call was placed to enable or verify this setting.

## Startup fixes and live outgoing call, 25 October 2026

Two failures were isolated after cutover:

1. The new bridge origin had no Chrome microphone grant. Audio preparation checked permission and exited before prompting. The browser then reported a generic ended event, so the helper's completion hid the error. The page now provides a one-time Enable microphone control, closes its temporary permission track immediately, and preserves startup errors in the call record and page. The owner granted access to port 8894.
2. Once the phone answered, Dot startup could exceed 15 seconds. The registered-call watchdog only refreshed on state polling, which paused while the helper awaited browser startup. The native host's independent two-second busy heartbeat continued, but the watchdog ignored it. The backend now refreshes liveness from authenticated busy pulses belonging to the current helper instance. Missing heartbeats and unrelated instances still cannot keep a call alive.

A regression test drives the actual HTTP pulse handler and simulated provider through 24 seconds of Dot startup. It failed before the fix and passes afterward. It also verifies that another helper instance cannot keep the call alive. All 87 JavaScript tests pass.

Live call `ca3eeb09-b763-4b2e-9966-7be5bbc27c6c` reached the recorded ringing stage 9.71 seconds after queueing and reported Dot connected after 22.45 seconds. These are service timestamps, not measured handset-ring latency. The owner confirmed clear audio both ways. The end-call tool then completed the call; the helper reported cleanup and audio restoration successful. Hardware status confirmed MacBook Pro Microphone and MacBook Pro Speakers. All 115 original-project source files still match the snapshot. Incoming phone-network calling remains untested.

The owner subsequently confirmed that an incoming phone call also passed. Background-app packaging and its separate no-phone lifecycle checks are documented in [BACKGROUND-APP.md](BACKGROUND-APP.md).

## Fresh Dot tab preparation, extension 0.1.9

A later request failed with BROWSER_COMMAND_FAILED during prepare, before audio routing or dialing. Browser inventory showed the previous Dot tab had navigated to Plugins and a new Dot tab was created at the time of failure. The extension called script injection immediately after creating a tab, before its document finished loading. A regression test reproduced the resulting Chrome access failure; the native boundary had previously collapsed that error into the generic code.

Preparation now waits for the requested Dot document to finish loading before checking controls, including after an automatic refresh. The native preparation deadline accommodates that bounded wait. Known Chrome failures now map to specific tab-closed, window-closed, access-denied, and controls-unavailable codes without persisting raw URLs.

After the owner reloaded version 0.1.9, an automatic prepare-only diagnostic created a fresh Dot tab and passed in 2.59 seconds. Its result reported verified=true, dialed=false, cleanup_ok=true. It did not arm audio or start voice. All 90 JavaScript tests and 117 Python tests pass. The live result is saved in .runtime/native-helper-result.json. No failed phone request was retried.

## Fresh developer setup, 25 October 2026

The README now starts from a fresh clone and separates local setup from provider-account permissions. The interactive configuration wizard creates independent credentials/client identity and a salted PIN hash, preserves existing files, and leaves calls disabled. The app installer supports `--fresh --install` without either legacy login agent; its rollback removes the fresh app login item without inventing older services. The no-phone verifier checks hidden-browser readiness and only requests audio diagnostics while calling is disabled.

A temporary clean source copy started with no `.secrets`, `.runtime`, `.venv`, or `node_modules`. `npm ci --ignore-scripts` and a new Python 3.11 virtual environment installed the locked dependencies. With dummy provider credentials, the extension generated a different identity, both apps compiled and passed code-signature verification, and the audio-routing binary compiled. Native-host registration used an isolated home directory, and fresh service preparation used isolated login-item paths. No real services were installed from this copy and no provider call was made.

The clean copy passed 96 JavaScript, 119 native-helper, 10 initial setup, and six supervisor tests. Two additional fresh-install/rollback tests brought the setup suite to 12 passing tests. The separate local stdio MCP probe also passed. Relative documentation links were checked. The existing installation and original project's source were unchanged.

Remaining external verification: a different developer must complete their own Twilio and OpenAI account setup, Chrome/macOS permissions, and ChatGPT plugin discovery. The isolated build does not prove those permissions, a full reboot, or telephone-network behavior for a new account. npm reported the existing Zod advisory documented in the README; no dependency upgrade was included in this setup change.

## Headset disconnect and idle BlackHole recovery

A call failed before dialing because macOS selected BlackHole 16ch and 2ch as defaults after the owner disconnected their Sony headset. The headset was absent from the device list, the service had no active call or helper, and CoreAudio reported both BlackHole devices idle. The previous guard treated any BlackHole default as an active route. A regression test failed against that committed implementation and passed with the fix.

Preflight now checks CoreAudio's device-running state and only recovers idle defaults to built-in microphone/speakers. The routing utility rechecks activity immediately before changing devices. Active or unknown state remains protected. Enable/restore wait for stable read-back; restore retains its snapshot if verification fails and uses available built-in audio when a saved device is absent.

Live no-phone checks recovered the actual idle route, verified that a deliberately opened silent BlackHole output prevented takeover, and verified restoration when an isolated test snapshot referenced disconnected devices. Final defaults were MacBook Pro Microphone and MacBook Pro Speakers. All 121 native-helper tests passed. No phone call was retried. The updated routing binary is installed; no Chrome reload is required.

## Telephone voice shaping and phone-to-Dot jitter buffer

The audio fixes from the original bridge were present but unused: calls take the registered browser path, which sent Dot's voice through a flat 1.4x gain with no limiter, and played phone audio without a prebuffer.

Dot-to-phone audio now passes through `phone-voice`, an AudioWorklet port of `mac-bridge/phone_voice.py` at 48 kHz: a 4.4 kHz fourth-order low-pass, a 260 Hz high-pass, a +6 dB shelf at 2 kHz, the same leveler (-12 dBFS envelope target, -10 to +15 dB) and the same soft limiter. In the telephone band its response is within 1 dB of the bridge chain: -5.9 dB at 200 Hz, +4.8 dB at 3 kHz and +4.9 dB at 3.4 kHz. Tones spanning 17.5 dB of input level land within 3.4 dB of each other. Rendering in Chrome through `OfflineAudioContext` matched the main-thread implementation within 0.1 dB, with loud input held at a 0.949 peak.

Phone-to-Dot playout uses the bridge's `SampleQueue`: three 20 ms chunks of prebuffer, whole-chunk refill after a shortfall, quiet-only drain above 100 ms and a 500 ms hard bound. With two chunks delayed every second, the previous playout spliced in 40 ms of silence; the new playout has none. `registered_audio_closed` logs received and played frames, underrun, rebuffer, drained and dropped counters after each call.

The hidden-page diagnostic's tone bound is now 0.015-0.45 RMS, because the shaping levels its -27 dBFS tone to about -13 dBFS. 105 JavaScript and 127 native-helper tests passed, plus the setup, supervisor and tunnel suites. No phone was dialed. The browser changes take effect after rebuilding and reloading the extension. A first live call before the rebuild still used the flat gain; after rebuilding and reloading, the owner confirmed on 2026-10-25 that a live call sounded good.

## Per-tab audio routing (helper removal, Phase 1)

Calls no longer change the Mac's default audio devices. During an armed call, the extension's document-start script routes Dot's audio elements and audio contexts to BlackHole 2ch with `setSinkId`, and restores them afterwards. The hidden Twilio page plays phone audio to BlackHole 16ch through its own `AudioContext` sink. The PCM worklet, the `127.0.0.1:8893` audio socket, the helper's playout and its jitter buffer, and the helper's route switching are removed. The helper only sequences calls.

If Dot connects but no player is routed within 12 seconds, or a player refuses BlackHole 2ch, the call fails with `DOT_OUTPUT_NOT_ROUTED`. Routing errors during a call end it the same way, rather than letting Dot play on the speakers.

Evidence before this change: a scratch Chrome profile routed WebRTC streams per element and per context with no leakage between BlackHole devices; the real extension's hidden page recovered 997 Hz on BlackHole 16ch with no helper PCM; and a live call worked in both directions with the Mac's speakers unchanged. On that call Dot used two audio elements and one audio context, and chatgpt.com accepted every routing request. 111 JavaScript, 123 native-helper and 14 setup tests pass.

Live check on this build: an outgoing call worked in both directions with Dot's output routed (2 elements, 1 context). The next two calls, both incoming, were hung up as Dot answered, with `DOT_OUTPUT_NOT_ROUTED` and `contexts:InvalidStateError`. Dot closes its audio context after each voice session, and the hook still tracked the closed ones from earlier calls in the same tab; Chrome rejects changing a closed context's output. Closed contexts are now skipped and pruned. A regression test fails on the previous hook and passes with the fix. The hook version moved to 0.1.9 so an open Dot tab reloads into the fix while idle.

After the fix and an extension reload: two incoming attempts made immediately after the reload were dropped right after the PIN. Twilio reported the browser leg as no-answer in 0 seconds and the backend never saw them, consistent with the hidden page still re-registering. The next incoming call connected and completed with Dot's output routed. A further call in the same Dot tab, without refreshing it, saw two audio contexts (one closed from the previous call), routed the live one with no errors, and completed with both directions audible. After reloading the extension, wait for **Ready for calls** before calling.

## Native helper removed (helper removal, Phases 2 and 3)

The extension's background worker now runs each call (`chrome-extension/calls.js`): it claims the job, prepares Dot's tab, reports the call prepared, starts Dot after the answer, watches Dot and its output routing every second, and runs one finish for every ending. A worker restart mid-call finishes that call once on start; the backend also ends a call whose extension stops checking in for 15 seconds. The no-phone audio check runs entirely in the hidden page, which plays a tone into BlackHole 2ch and records BlackHole 16ch; the backend records the result only for the requested ID. The one safety check that needed native code, refusing a call while the Mac's default output is BlackHole, now uses Chrome's default-device label.

Removed: the `Dot Phone Call Helper` app, its Chrome native-messaging host and permission, the `audio-routing` tool, all `mac-bridge/` Python code, the prototypes, and their scripts and tests. Python is needed only for setup scripts and the supervisor, so the Python 3.11-only restriction is gone. Call progress, including Dot's output-routing counts, is logged to `.runtime/app-backend.log`. 114 JavaScript, 14 setup, and 6 supervisor tests pass, plus the tunnel probe.

Live checks on this build, with no helper process running: the no-phone check recovered 996.1 Hz on BlackHole 16ch (RMS 0.144, 131 Opus packets, audible for 2.2 s) with no native code involved. Then an incoming call and an outgoing call in the same Dot tab both connected and completed with both directions audible. The backend log recorded Dot's output routed on each (2 elements and 1 context; the second call also saw the closed context from the first, with no errors). The old helper's log was not written by either call.

Restart while idle: a full service restart returned the menu to **Ready for calls** in 9 seconds without user action; the hidden page re-registered with Twilio and calling remained available.

## Call log, and the first call after a Chrome restart, 27 and 28 October 2026

Two live calls could not be explained from the old one-line progress log:

- 27 October, 23:00, outgoing: Dot stopped speaking partway through. Twilio showed both legs connected for 114 seconds and ending together, the extension reported Dot's output routed with no errors, and the backend recorded a normal completion. Nothing showed which direction went silent.
- 28 October, 14:42, incoming, 8 minutes after Chrome started: the extension answered, pressed Start call, saw Dot's voice controls appear, and then saw them disappear before Dot's microphone connected (`DOT_VOICE_ENDED_BEFORE_CONNECT`). A retry 5 minutes later in the same tab answered normally without clicking the tab. Both calls used the same tab ID, which suggests the extension opened the Dot tab for the first call and Dot was not yet ready to hold a voice session. Not confirmed.

The backend, extension and hidden Twilio page now write one timestamped call log (`.runtime/logs/`), read with `npm run call-log`. It records the reason the backend ended each call (phone hung up, Dot's hang-up tool, extension heartbeat lost, Twilio client unregistered, time limit, restart), how the Dot tab was found and how old its page was, how voice start progressed, Dot's microphone track and player state, Twilio errors and warnings, and 5-second audio peaks for the caller, Dot's raw voice and the audio sent to the phone. Live check, 28 October 2026, 15:12 and 15:13: one call placed by Dot and one call from the owner's phone both connected and were hung up from the phone. Each was logged end to end by all three parts: dial or PIN, Chrome answering, Dot connecting about 1.6 seconds after Start call, 5-second audio peaks in both directions (about -10 to -20 dB while speaking), and the ending, with Twilio's phone-leg durations (65 s and 54 s). Those calls exposed reader mistakes that were then fixed: a call Dot places was labeled incoming (Chrome is rung as a Twilio leg either way), a Twilio disconnect after the phone hung up was described as a dropped connection, and a suspended Dot audio context was flagged although Dot was audible, so that signal is no longer reported. 124 JavaScript tests pass.

## Nightly calls from Dot, 28 to 30 October 2026

The call log showed that every call Dot placed at night began with no Dot tab open, so the extension opened one for the call:

- 28 October, 23:01: worked, and was cut off by the 120-second default time limit.
- 29 October, 23:00: the new tab was gone before Dot could start (`DOT_TAB_CLOSED`).
- 30 October, 23:00: Dot connected 24 seconds after its tab opened, played about one second of audio, then sent pure silence while the caller's audio kept arriving; Twilio warned of constant outgoing audio. Dot created a third audio player during the call (every healthy call used two), and its voice session ended by itself 24 seconds after connecting. This looks like Dot's own voice connection failing to recover, not Twilio, routing or the network. Not confirmed.

Daytime calls in a Dot tab that had been open for several minutes worked. The extension now keeps one Dot tab loaded between calls (opened pinned in the background if missing, never unloaded by Chrome), and the call log records when Dot creates a new audio player mid-call. The report also now recognizes the time-limit cutoff, and no longer blames a microphone track that ended only because Dot's session ended. 129 JavaScript tests pass. Not yet checked on a live call.
