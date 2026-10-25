# Dot Phone Call

Make and receive phone-number calls with your own ChatGPT Dot, using its native voice. Your Mac runs the calling backend and audio bridge; Twilio connects the telephone call. No additional language model is used.

This is a **macOS developer installation from source**. Each developer supplies their own Twilio account, phone number, ChatGPT access, and Secure MCP Tunnel. You do not need the original `dot-phone-plugin` project, its hosted service, or any files from another developer's `.secrets` folder. There is no downloadable, notarized app installer yet.

## Before you start

| Requirement | What to check |
| --- | --- |
| macOS 13 or later | The Mac must stay awake, online, and signed in while calls are available. |
| Chrome with access to Dot | Open your Dot and confirm that its in-app voice works before installing this project. The extension currently controls `https://chatgpt.com/dots/*`. |
| ChatGPT custom-plugin and tunnel access | Check this before spending time on setup. Account/workspace restrictions still apply; this repository cannot grant those permissions. |
| Twilio | Use an account with a voice-capable number and permission to call your saved destination. Real phone calls use your Twilio balance. |
| Build tools | [Node.js](https://nodejs.org/en/download) 24+, [Python](https://www.python.org/downloads/macos/) **3.11**, Git, and Xcode Command Line Tools. Python 3.13+ is not supported because retained audio code uses `audioop`. |
| Virtual audio | Install **both BlackHole 2ch and BlackHole 16ch** using the [official instructions](https://github.com/ExistentialAudio/BlackHole#installation-instructions). Restart if the installer requests it. |

If you use Homebrew, BlackHole's documented commands are `brew install blackhole-2ch` and `brew install blackhole-16ch`. Install Apple's build tools with `xcode-select --install` if needed. Confirm both BlackHole devices appear in Audio MIDI Setup; leave your normal microphone and speakers selected as defaults.

Keep the checkout in a permanent location. Generated apps and services reference that directory. Only one installation of this project can use its fixed local ports and login-item names on a Mac at a time.

## 1. Clone and install dependencies

Clone this repository using its GitHub **Code** URL, then run the following from the checkout root. Every command below assumes that directory.

```sh
node --version
python3.11 --version
swiftc --version
npm ci --ignore-scripts
python3.11 -m venv .venv
.venv/bin/python -m pip install -r mac-bridge/requirements.lock
mkdir -p .runtime
```

Expected versions are Node 24 or later and Python 3.11. The project installs JavaScript and Python dependencies locally.

## 2. Prepare your accounts

In Twilio Console, obtain your live Account SID and Auth Token, a voice-capable Twilio number, and a separate **Standard API key** with its SID and secret. Use credentials from the same account in the default US1 region. The backend uses the account token for call control; the API key signs short-lived browser access tokens. See [Twilio API keys](https://www.twilio.com/docs/iam/api-keys).

For OpenAI, create a tunnel in the Platform organization's tunnel settings and associate it with the ChatGPT workspace/account that will use Dot. Creating tunnels needs Tunnels Read + Manage; running one needs Read + Use. Create a runtime API key with Read + Use in the appropriate organization. ChatGPT custom-plugin access is a separate permission. For a personal account, use its personal Platform organization. Follow the [official Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

Download the macOS `tunnel-client` binary for your architecture using the Platform download link or the [official latest release](https://github.com/openai/tunnel-client/releases/latest). Extract it, then copy the executable here, replacing the example source path:

```sh
install -m 700 /absolute/path/to/downloaded/tunnel-client .runtime/tunnel-client
.runtime/tunnel-client help quickstart
```

Do not paste credentials into ChatGPT, Git, or shell command arguments. The next step collects them locally with hidden prompts.

## 3. Create your private configuration

```sh
.venv/bin/python scripts/configure-local.py --interactive
```

The wizard asks for the Twilio credentials, your phone number, your Twilio number, a new six-digit incoming-call PIN, your tunnel ID, and the runtime API key. Use international phone-number format such as `+15555550101`.

It creates these ignored files with private permissions:

| File under `.secrets/` | Contents |
| --- | --- |
| `local-backend.json` | Random local MCP credential; incoming and outgoing calls disabled initially. |
| `inbound-worker-secrets.json` | Your Twilio settings, a random client identity, and a salted PIN hash. The historical filename does not mean a cloud worker is required. |
| `tunnel.json` | Your tunnel ID. |
| `tunnel-runtime-key.txt` | Your OpenAI tunnel runtime key. |

Existing files and enabled-calling settings are preserved on subsequent runs. The PIN itself is not stored. Save it in your password manager. `config/` contains examples for manual configuration; do not copy their placeholder values into a live installation.

## 4. Build the extension and apps

```sh
node scripts/build-mac-extension.mjs --enable-calling --backend=local
.venv/bin/python scripts/build-mac-app.py
swiftc -module-cache-path .runtime/swift-cache mac-bridge/routing.swift -o .runtime/audio-routing
.venv/bin/python scripts/install-native-host.py
npm run build:app
```

The extension build creates a unique pairing in `.secrets/mac-companion.json`. Keep that file across rebuilds. The native-host installer registers only that extension's identity with Chrome.

`--enable-calling` enables the extension's polling loop. It does **not** enable phone calls while the backend's two calling flags are false.

Generated outputs are `.runtime/dot-phone-extension`, `.runtime/Dot Phone Call Helper.app`, and `.runtime/Dot Phone.app`. They are locally signed and remain tied to this checkout.

## 5. Install the background app and load Chrome's extension

For a **first installation**, run:

```sh
.venv/bin/python scripts/install-background-app.py --fresh --install
```

This creates one login item, `dev.dot-phone-call.app`, and starts the backend and tunnel. It does not require old login agents. Look for **Dot Phone** beside the macOS clock. Until Chrome is connected, an offline/disabled-calling status is expected.

In Chrome, open `chrome://extensions`, enable Developer mode, choose **Load unpacked**, and select this checkout's `.runtime/dot-phone-extension` folder. Use the Chrome profile where you signed into Dot.

When **Dot Phone audio setup** opens, click **Enable microphone** and allow access. You can close that setup page afterward. Chrome maintains the Twilio bridge in a hidden extension document; no localhost bridge tab is needed. The native **Dot Phone Call Helper** requests macOS microphone permission when its first audio check runs. Allow it in System Settings → Privacy & Security → Microphone if necessary. Chrome also needs microphone access for Dot voice.

Wait for the app to show Chrome connected. Calling remains disabled until step 9.

For an existing installation using this project's two older login agents, use `--install` **without** `--fresh`. That migration preserves their commands and rollback copies. Do not run the old service installers on a new installation.

## 6. Verify locally without dialing

```sh
.venv/bin/python scripts/verify-setup.py
.venv/bin/python scripts/verify-setup.py --audio
```

The first command checks registration and microphone readiness. The second automatically starts the native helper and tests a tone through BlackHole 2ch, local Opus WebRTC, and BlackHole 16ch. Allow the helper's microphone prompt if shown. It requires both calling flags to be false and refuses to run during an active call.

Success reports `verified: true`, `dialed: false`, `cleanup_ok: true`, and a recovered frequency near 997 Hz. The helper closes after the test. This checks the local audio path, not the telephone network or a spoken Dot reply. No phone number is dialed.

## 7. Connect your private ChatGPT plugin

Keep the app running. In [ChatGPT Plugins](https://chatgpt.com/plugins), choose **+ → Add custom MCP server**, name it **Dot Phone Local**, choose **Tunnel**, and select your tunnel ID. Review the permissions and create it as a plugin. See [OpenAI's connection instructions](https://developers.openai.com/plugins/deploy/connect-chatgpt).

For this project's local backend, choose **no connector-level authentication**. The tunnel is controlled by your OpenAI account permissions, and its local client injects the backend's generated bearer credential from `.secrets/local-mcp-authorization.txt`. Do not enter your Twilio or OpenAI API keys in the plugin form.

Verify discovery of `get_phone_service_status`, `prepare_owner_call`, `start_owner_call`, `get_owner_call`, and `end_owner_call`. Ask Dot to use **Dot Phone Local's `get_phone_service_status` only, without placing a call**. Expect `backend: local`, `mac_online: true`, and registered browser status. Calling is still disabled, so `can_request_native_dot_call: false` is expected at this stage. Do not count setup complete until this tool call succeeds through ChatGPT.

## 8. Configure incoming calls, if wanted

Outgoing calling does not require this step. For incoming calls, create a new **TwiML Bin** in Twilio Console. Save it to obtain its URL, then render the instructions with that exact URL:

```sh
node scripts/render-incoming-route.mjs https://handler.twilio.com/twiml/EH_YOUR_BIN_ID
```

Replace `EH_YOUR_BIN_ID` with the actual Bin SID, `EH` plus 32 hexadecimal characters. Copy `.runtime/incoming-route.xml` into the Bin and save it. The XML prompts for six digits and routes to your generated client identity. It does not contain your PIN or API credentials.

Validate the saved instructions, then deliberately switch your Twilio number's incoming route:

```sh
.venv/bin/python scripts/test-incoming-route.py https://handler.twilio.com/twiml/EH_YOUR_BIN_ID
.venv/bin/python scripts/switch-incoming-route.py --url https://handler.twilio.com/twiml/EH_YOUR_BIN_ID --apply
```

These commands fetch/validate TwiML and update routing; they do not dial. The switch saves the previous route in `.secrets/incoming-route-fallback.json`. It changes where incoming calls to that number go, so use a dedicated number or inspect its current use first. Incoming callers must use the saved owner number and correct PIN. A second person's number is not accepted.

## 9. Enable calling and make your first test call

After the local audio check and ChatGPT status tool succeed, edit `.secrets/local-backend.json`. Preserve `mcp_token` and set:

```json
"calling_enabled": true,
"allow_phone_dialing": true
```

This is a fragment to edit, not a replacement for the entire JSON file. Use **Dot Phone → Restart services** in the menu bar. Run `scripts/verify-setup.py` with `.venv/bin/python` again and ask Dot to check status. Expect **Ready for calls** and `can_request_native_dot_call: true`.

Now explicitly ask Dot Phone Local to call you. Confirm audio in both directions, then hang up and confirm cleanup. If you configured incoming routing, call your Twilio number from the saved owner phone and enter the PIN. These are real telephone calls. Do not automatically retry a failed or uncertain outgoing request.

## Everyday use and updates

Keep the Mac awake, Chrome open, and Dot signed in. The menu-bar app starts at login; the audio helper starts only when needed. The app's **Open logs** action opens local diagnostics. You do not need `npm start` while the app runs; both would compete for port 8894.

After quitting, relaunch with:

```sh
launchctl kickstart "gui/$(id -u)/dev.dot-phone-call.app"
```

Keep `.secrets/` and the checkout path when updating. Reinstall dependencies and rebuild changed apps/extension while idle. Reload **Dot Phone Call (local)** in Chrome after extension changes; it refreshes Dot's page when needed. Restart services for backend changes. Do not run simultaneous calls from two checkouts or the old project.

To stop the login installation while retaining source and private configuration:

```sh
.venv/bin/python scripts/install-background-app.py --restore
```

On a fresh installation this removes the app login item. On a migrated installation it restores the saved older services. Finish any call first. To restore previous incoming routing, use `scripts/switch-incoming-route.py --restore --apply` with `.venv/bin/python`. That route backup belongs to your account; no hosted fallback service is supplied by this repository.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Tunnel missing in ChatGPT | Match the account/workspace association and organization-level Tunnels Read + Use permissions. A Platform organization alone may not expose it to your ChatGPT workspace. |
| App installer fails | Check Node is on PATH, `.runtime/tunnel-client` is executable, the wizard completed, and ports 8894/8896 are unused. Inspect `.runtime/app-backend.log` and `.runtime/app-tunnel.log`. Fix the cause, then rerun the installer. |
| Microphone denied | Open the extension's **Options** page to enable microphone access. Check macOS microphone permission for Chrome and Dot Phone Call Helper. |
| Chrome disconnected | Load the extension in the correct profile and inspect its Errors page. Reload it after rebuilding. |
| `AUDIO_ROUTE_ALREADY_IN_USE` | An active BlackHole device is protected from takeover. Idle BlackHole defaults left after a headset disconnect now recover to built-in audio automatically; close the application using BlackHole if the device is genuinely active. |
| Audio diagnostic fails | Inspect `.runtime/native-helper-result.json` and `.runtime/native-helper.log`. Keep calling disabled until it passes. |
| Unexpected music/background audio | Pause other audio applications. Current BlackHole routing can capture other system audio during a call. |
| Dot controls unavailable | Confirm Dot voice works manually in Chrome. This integration depends on Dot's browser UI and may need updates when it changes. |
| Phone does not ring | Check Dot's call status, saved number format, Twilio account restrictions, and calling flags. A queued request is not a confirmed call. |

## Development and verification

```sh
npm test
npm run test:python
npm run test:app
npm run test:setup
npm run test:tunnel
```

Automated tests use simulated provider calls and audio devices. `test:setup` uses temporary directories and mocked service installation. `test:tunnel` checks the local MCP probe over stdio, not your real OpenAI tunnel. None of these commands dial a phone. Scripts under `prototype/` and `mac-bridge/test-session.py` are separate experiments; some can place calls and are not setup instructions.

See [architecture](docs/ARCHITECTURE.md), [background app](docs/BACKGROUND-APP.md), and [validation history](docs/VALIDATION.md). The history records the original installation's tests; it is not a prerequisite or a source of credentials for your installation. The latest hidden-bridge validation used no phone call. A fresh-account end-to-end install and full reboot have not been independently verified yet.

### Known dependency advisory

The 2026-10-25 clean install reported one high-severity npm advisory against the locked Zod 3 dependency: [GHSA-mrhx-c7xc-m3w5](https://github.com/advisories/GHSA-mrhx-c7xc-m3w5). npm proposes a major-version update. That migration has not been tested here; do not treat a successful setup or unit-test run as resolution of the advisory. The local backend already bounds request bodies, but that is not a dependency patch.
