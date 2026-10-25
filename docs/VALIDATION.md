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
