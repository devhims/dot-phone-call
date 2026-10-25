# dot-phone-call

Local incoming and outgoing phone calls with Dot. The Mac runs the queue, MCP tools, caller and PIN checks, browser controls, and audio helper. Twilio provides the telephone connection and registered browser client. Dot provides its own intelligence and voice.

The default call path uses a registered Twilio browser client. It does not start Cloudflare, use Media Streams, or need public HTTP access to the Mac. Outgoing calls use inline TwiML through Twilio's REST API. Incoming calls use a small Twilio-hosted TwiML Bin to collect a PIN and route to the client; all authorization decisions happen locally.

The existing `dot-phone-plugin` source, helper, and hosted plugin are retained as an explicit fallback. This project has separate credentials, ports, native host identity, and runtime state. The installed **Dot Phone Local** connection uses Secure MCP Tunnel; the Twilio number now routes incoming calls to this project. See [cutover and rollback](docs/CUTOVER.md). New configurations still default to calling disabled.

## Call lifecycle

For example, when you call your Twilio number, Twilio collects the six-digit PIN and offers the call to this project's registered browser. The local backend checks the actual parent and child call records with Twilio, verifies your saved caller number and PIN, and queues the call. The extension starts the native audio helper. Once the audio route is ready, the browser accepts and the helper starts Dot.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'actorBkg': '#e2e8f0', 'actorTextColor': '#111827', 'actorBorder': '#334155', 'signalColor': '#334155', 'signalTextColor': '#111827', 'sequenceNumberColor': '#ffffff'}}}%%
sequenceDiagram
    autonumber
    participant P as Your phone
    participant T as Twilio
    participant B as Registered browser client
    participant L as Local backend
    participant H as Extension and audio helper
    participant D as Dot
    P->>T: Dial Twilio number and enter PIN
    T->>B: Offer client call with PIN parameter
    B->>L: Request authorization
    L->>T: Verify parent, child, account and numbers
    L->>L: Verify PIN and queue once
    H->>L: Claim local job
    H->>B: Prepare authenticated audio pipe
    B->>T: Accept authorized call
    H->>D: Start voice and verify microphone
    P<<->>D: Audio through Twilio, browser and BlackHole
    L->>T: End owned phone call
    B->>L: Confirm audio tracks released
    H->>D: Hang up voice
    H->>H: Restore original Mac audio devices
```

Outgoing requests follow the same local queue and audio preparation. The backend dials only the saved number after the browser confirms audio readiness. Dot starts after the phone answers. There is no diagnostic arm file or manual bridge-page step in the normal call path.

The browser remains registered while idle without opening the microphone. The extension creates a hidden Chrome offscreen document automatically on startup. The bridge no longer needs a visible tab. Dot's own page still opens for its voice session. Access tokens refresh before expiry. Native audio runs only during a call. Cancellation, phone hangup, browser loss, helper loss, and time limits all initiate cleanup. A persistent journal blocks automatic retries after an uncertain call-creation response.

## Components

- `src/registered-calls.mjs`: registered-client authorization, token renewal, provider calls, and durable recovery.
- `src/local-backend.mjs`, `local-main.mjs`, `local-queue.mjs`: private HTTP MCP, companion API, and SQLite queue.
- `browser/`: persistent registration, incoming call screening, per-call audio, and browser cleanup.
- `chrome-extension/`: automatic bridge startup, Dot controls, microphone selection, and native messaging.
- `mac-bridge/registered_call.py`: per-call 48 kHz PCM playback to BlackHole and verified audio restoration.
- `mac-bridge/companion.py`: the registered-client path is the default. Older transport code is retained only as an explicit legacy implementation.
- `prototype/registered/`: separate client-to-client audio experiment. Never use the saved-owner prototype as a no-phone test.
- `scripts/`: independent builds, installation, configuration, and incoming TwiML rendering.
- `test/` and `mac-bridge/test_*.py`: automated tests without provider calls or hardware audio.

## Independent installation

Requires Node.js 24+, Python 3.11, Chrome signed into Dot, Swift build tools, and BlackHole 2ch and 16ch. The Mac and Chrome must be awake and running. This project does not make Dot available through a server-side API.

```sh
npm ci --ignore-scripts
python3.11 -m venv .venv
.venv/bin/python -m pip install -r mac-bridge/requirements.lock
python3 scripts/configure-local.py
node scripts/build-mac-extension.mjs --enable-calling
python3 scripts/build-mac-app.py
swiftc -module-cache-path .runtime/swift-cache mac-bridge/routing.swift -o .runtime/audio-routing
python3 scripts/install-native-host.py
```

The extension flag enables local polling; it cannot bypass the backend's disabled-calling flags. Load `.runtime/dot-phone-extension` as a new unpacked Chrome extension. Grant microphone access to **Dot Phone Call Helper** when macOS asks. The extension opens **Dot Phone audio setup** when microphone permission is missing. Click **Enable microphone** and allow access once, then close the setup page. This briefly opens and immediately closes a microphone track. Permission belongs to the extension, so the old localhost permission does not carry over. Normal calls then start unattended. Chrome protects its extension installation page, so that step is manual.

Private configuration lives only in this project's `.secrets/`, excluded from Git. Use mode `0700` for the directory and `0600` for files. `config/` contains templates. `inbound-worker-secrets.json` retains its historical filename but is local configuration. It holds Twilio account credentials, a separate API key, a unique stable client identity, saved phone numbers, and the salted PIN hash. The hash is lowercase SHA-256 of `salt:123456`, substituting the actual random salt and six-digit PIN.

### Background app

The installed **Dot Phone.app** owns the backend and Secure MCP Tunnel under one login service, `dev.dot-phone-call.app`. Look for **Dot Phone** beside the clock in the macOS menu bar. Its menu shows connection status, opens logs, restarts idle services, and quits cleanly. Restart is unavailable during an active call. Quitting during a call asks before ending it and restoring audio.

The Mac audio helper remains separate and starts on demand through Chrome. It is not another login service. Keep the Mac awake and Chrome running. The app itself does not open the microphone or dial a number.

Build and install the background app:

```sh
npm run build:app
.venv/bin/python scripts/install-background-app.py --install
```

The installer verifies an idle service, saves the old login-agent definitions, and replaces only this project's two login agents. It preserves the existing calling flags and credentials. Its setup currently expects the already-configured local backend and tunnel; it is not a distributable first-time installer.

To launch the installed app again after quitting:

```sh
launchctl kickstart "gui/$(id -u)/dev.dot-phone-call.app"
```

For rollback to the previous two login services:

```sh
.venv/bin/python scripts/install-background-app.py --restore
```

Do not run `npm start` alongside the app. That command starts the same backend in the foreground for development and would compete for port 8894. See [background app details and validation](docs/BACKGROUND-APP.md).

### Incoming routing

Create a new TwiML Bin in Twilio, then render its body with its URL:

```sh
node scripts/render-incoming-route.mjs https://handler.twilio.com/twiml/EH_YOUR_BIN_ID
```

Use an actual `EH` SID with 32 hexadecimal characters. Copy `.runtime/incoming-route.xml` into that Bin. It collects six digits and forwards them through a custom client parameter. It contains no PIN or account secret. The local backend compares a salted hash, validates the actual Twilio call records, and enforces attempt limits before allowing audio.

The renderer never changes the Twilio number. Validate the saved Bin with `scripts/test-incoming-route.py URL`, then use `scripts/switch-incoming-route.py --url URL --apply` to back up and switch the saved number. The current installation has completed this cutover. The original project remains available as a fallback.

### Enable calls after validation

`calling_enabled` allows the local queue to accept calls. `allow_phone_dialing` separately allows outgoing requests to the saved number. Both must be true for outgoing phone calls. Incoming authorization requires `calling_enabled`; it never creates an outgoing phone call. Restart the backend after editing configuration.

## Tests

```sh
npm test
npm run test:python
npm run test:tunnel
```

These use mocked provider calls and audio devices, temporary HTTP/WebSocket listeners, and in-memory audio. They cover incoming PIN/caller checks, outgoing audio gating, at-most-once dialing, uncertain-result recovery, cancellation, token renewal, automatic browser registration, authenticated PCM, track release, and helper cleanup. No phone is dialed.

For a spoken Dot test, build with `--audio-test --enable-calling` and load or reload this project's extension. That diagnostic cannot invoke Twilio. It requires `whisper-cli` and `.runtime/ggml-base.en.bin`. It runs once per generated test ID and reports in `.runtime/browser-audio-test-result.json` and `.runtime/native-helper-result.json`.

The separate `registered_probe.py --native-audio` experiment creates temporary Twilio client-to-client calls and uses generated tones. It changes virtual audio routing and needs Chrome microphone access. It cannot dial a phone number. `test-session.py` and the saved-owner prototype can dial phones and are not unit tests.

See `docs/VALIDATION.md` for observed results and remaining live checks. Historical extraction notes are in `docs/reference/`.

## Resource isolation and limits

- Private backend and browser page: `127.0.0.1:8894`.
- Per-call native audio WebSocket: `127.0.0.1:8893`.
- Native host: `dev.dot_phone_call.helper`.
- macOS bundle: `dev.dot-phone-call.helper`.
- Local service: `dev.dot-phone-call.backend`.

The helper refuses to take over an existing BlackHole default route. Physical devices and the signed-in Dot tab are still shared resources, so do not run simultaneous calls from both projects. Global Mac output routing can include audio from other applications. Dot-only output isolation is not part of this integration.

A lost outgoing REST response is deliberately not retried. The journal retains the uncertain intent and blocks subsequent calls until the provider outcome is reconciled. This prevents a timeout from placing a duplicate call. Credentials and runtime call records remain local and ignored by Git.

Provider references: [Twilio Device registration and tokens](https://www.twilio.com/docs/voice/sdks/javascript/twiliodevice), [TwiML Bin templating](https://www.twilio.com/docs/serverless/twiml-bins/getting-started), and [custom client parameters](https://help.twilio.com/articles/115011213347-Passing-Custom-Information-via-Requests-to-Twilio).

## Hidden bridge diagnostic and fallback

Build with `node scripts/build-mac-extension.mjs --enable-calling --backend=local --hidden-audio-test` to run one diagnostic after reloading the extension and granting microphone permission. Disable both backend calling flags during this test. It launches the native helper automatically and passes a 997 Hz tone through BlackHole 2ch, the real browser audio session, local Opus WebRTC, PCM playback, and BlackHole 16ch. It checks the received tone, closes all test tracks and streams, and never invokes the phone API. The test uses a muted audio element to match Twilio's remote playback lifecycle and a duplex BlackHole recorder to avoid conflicting CoreAudio streams. This verifies local audio transport; it does not prove a live telephone call or Dot response.

The packaged page contains the Twilio SDK and audio code. Its HTTP and WebSocket access requires the paired extension origin and existing local capabilities. The local backend reports `browser_context: offscreen` and microphone readiness. Registration stays active while idle with no microphone track open.

For rollback, rebuild with `node scripts/build-mac-extension.mjs --enable-calling --backend=local --tab-bridge`, then reload the extension while idle. This restores the localhost bridge tab. Do not run both bridge modes at once.

Verified on 2026-10-25: two hidden-page audio checks passed with 124 and 125 received Opus packets, approximately 997 Hz recovered at BlackHole 16ch, and zero dropped PCM chunks. The helper exited and its audio listener closed after each check. Backend restart recreated the hidden page automatically. Secure MCP Tunnel reported calling ready with `browser_context: offscreen`, and Chrome had no visible bridge tab. No telephone call was placed during these checks. A full Mac reboot and a live telephone call on this hidden-page build remain untested.

During development, a local `.runtime/hidden-audio-test-request.json` containing a fresh UUID `id` and an `expires_at` timestamp in milliseconds can request another audio diagnostic without reloading the extension. The backend exposes that request only while calling is disabled, no call is active, and expiry is less than ten minutes away. The extension executes each ID once. Remove the request and restore the saved calling settings after testing. Runtime files and credentials remain ignored by Git.
