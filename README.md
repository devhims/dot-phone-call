# Dot Phone Call

Make and receive phone-number calls with your own ChatGPT Dot, using its native voice. Your Mac runs the calling backend and a Chrome extension that carries the call audio; Twilio connects the telephone call. No additional language model is used.

This is a **macOS developer installation from source**. Each developer supplies their own Twilio account, phone number, ChatGPT access, and Secure MCP Tunnel; no hosted service or shared credentials are involved. There is no downloadable, notarized app installer yet.

## Before you start

| Requirement | What to check |
| --- | --- |
| macOS 13 or later | The Mac must stay awake, online, and signed in while calls are available. |
| Chrome with access to Dot | Open your Dot and confirm that its in-app voice works before installing this project. The extension currently controls `https://chatgpt.com/dots/*`. |
| ChatGPT custom-plugin and tunnel access | Check this before spending time on setup. Account/workspace restrictions still apply; this repository cannot grant those permissions. |
| Twilio | A voice-capable Twilio number, and Voice **Geographic Permissions** enabled for your own phone's country (Console → Voice → Settings → Geo permissions). Trial accounts can call only verified numbers and play a trial announcement on each call. Real calls use your Twilio balance. |
| Build tools | [Node.js](https://nodejs.org/en/download) **24+** (the repository's `.nvmrc` selects it with `nvm use`), [Python](https://www.python.org/downloads/macos/) **3.11 or later** (for setup scripts), Git, and Xcode Command Line Tools (to build the menu-bar app). |
| Virtual audio | Install **both BlackHole 2ch and BlackHole 16ch** using the [official instructions](https://github.com/ExistentialAudio/BlackHole#installation-instructions). Restart if the installer requests it. |

If you use Homebrew, BlackHole's documented commands are `brew install blackhole-2ch` and `brew install blackhole-16ch`. Install Apple's build tools with `xcode-select --install` if needed. Confirm both BlackHole devices appear in Audio MIDI Setup; leave your normal microphone and speakers selected as defaults.

Keep the checkout in a permanent location. Generated apps and services reference that directory. Only one installation of this project can use its fixed local ports and login-item names on a Mac at a time.

## 1. Clone and install dependencies

Clone this repository using its GitHub **Code** URL, then run the following from the checkout root. Every command below assumes that directory. If you use nvm, run `nvm use` first in every new terminal you use for setup.

```sh
node --version
python3 --version
swiftc --version
npm ci --ignore-scripts
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
mkdir -p .runtime
```

Expected versions are Node 24 or later and Python 3.11 or later. The project installs JavaScript and Python dependencies locally.

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
npm run build:app
```

The extension build creates a unique pairing in `.secrets/mac-companion.json`. Keep that file across rebuilds; the backend accepts only that extension's identity.

`--enable-calling` enables the extension's polling loop. It does **not** enable phone calls while the backend's two calling flags are false.

Generated outputs are `.runtime/dot-phone-extension` and `.runtime/Dot Phone.app`. They are locally signed and remain tied to this checkout.

## 5. Install the background app and load Chrome's extension

For a **first installation**, run:

```sh
.venv/bin/python scripts/install-background-app.py --fresh --install
```

This creates one login item, `dev.dot-phone-call.app`, and starts the backend and tunnel. Look for **Dot Phone** beside the macOS clock. Until Chrome is connected, an offline/disabled-calling status is expected.

The installer records the exact `node` binary on your `PATH` for the login item, and refuses anything older than Node 24. Run it from a terminal where `node --version` shows 24 or later. If you later remove or switch away from that Node installation, rerun this installer.

In Chrome, open `chrome://extensions`, enable Developer mode, choose **Load unpacked**, and select this checkout's `.runtime/dot-phone-extension` folder. Use the Chrome profile where you signed into Dot.

When **Dot Phone audio setup** opens, click **Enable microphone** and allow access. You can close that setup page afterward. Chrome maintains the Twilio bridge in a hidden extension document; no localhost bridge tab is needed. Chrome also needs macOS microphone permission, which Dot's own voice already uses. Calls never change your Mac's default speaker or microphone.

Wait for the app to show Chrome connected. Calling remains disabled until step 9.

## 6. Verify locally without dialing

```sh
.venv/bin/python scripts/verify-setup.py
.venv/bin/python scripts/verify-setup.py --audio
```

The first command checks registration and microphone readiness. The second has the hidden Twilio page play a tone into BlackHole 2ch, pass it through the call audio path and local Opus WebRTC, and record what arrives on BlackHole 16ch. It requires both calling flags to be false and refuses to run during an active call.

Success reports `verified: true`, `dialed: false`, and a recovered frequency near 997 Hz. This checks the local audio path, not the telephone network or a spoken Dot reply. No phone number is dialed.

## 7. Connect your private ChatGPT plugin

Keep the app running. In [ChatGPT Plugins](https://chatgpt.com/plugins), choose **+ → Add custom MCP server**, name it **Dot Phone Local**, choose **Tunnel**, and select your tunnel ID. Review the permissions and create it as a plugin. See [OpenAI's connection instructions](https://developers.openai.com/plugins/deploy/connect-chatgpt).

For this project's local backend, choose **no connector-level authentication**. The tunnel is controlled by your OpenAI account permissions, and its local client injects the backend's generated bearer credential from `.secrets/local-mcp-authorization.txt`. Do not enter your Twilio or OpenAI API keys in the plugin form.

If Dot does not recognize **Dot Phone Local**, check that the plugin was created in the same ChatGPT account and workspace where you use Dot, and that it is enabled there.

Verify discovery of `get_phone_service_status`, `prepare_owner_call`, `start_owner_call`, `get_owner_call`, and `end_owner_call`. Ask Dot to use **Dot Phone Local's `get_phone_service_status` only, without placing a call**. Expect `backend: local`, `mac_online: true`, and registered browser status. Calling is still disabled, so `can_request_native_dot_call: false` is expected at this stage. Do not count setup complete until this tool call succeeds through ChatGPT.

## 8. Configure incoming calls, if wanted

Outgoing calling does not require this step. For incoming calls, create a new **TwiML Bin** in Twilio Console. Twilio will not save an empty Bin, so first save it with the placeholder `<Response></Response>` to obtain its URL. Then render the real instructions with that exact URL:

```sh
node scripts/render-incoming-route.mjs https://handler.twilio.com/twiml/EH_YOUR_BIN_ID
```

Replace `EH_YOUR_BIN_ID` with the actual Bin SID, `EH` plus 32 hexadecimal characters. Replace the placeholder with the contents of `.runtime/incoming-route.xml` and save the Bin again. The XML prompts for six digits and routes to your generated client identity. It does not contain your PIN or API credentials.

Validate the saved instructions, then deliberately switch your Twilio number's incoming route:

```sh
.venv/bin/python scripts/test-incoming-route.py https://handler.twilio.com/twiml/EH_YOUR_BIN_ID
.venv/bin/python scripts/switch-incoming-route.py --url https://handler.twilio.com/twiml/EH_YOUR_BIN_ID --apply
```

These commands fetch/validate TwiML and update routing; they do not dial. The switch saves the previous route in `.secrets/incoming-route-fallback.json`. It changes where incoming calls to that number go, so use a dedicated number or inspect its current use first. Incoming callers must use the saved owner number and correct PIN. A second person's number is not accepted.

## 9. Enable calling and make your first test call

After the local audio check and ChatGPT status tool succeed, turn on both calling flags in `.secrets/local-backend.json`. This command changes only those two values and keeps your `mcp_token`:

```sh
.venv/bin/python -c "import json,pathlib;p=pathlib.Path('.secrets/local-backend.json');c=json.loads(p.read_text());c.update(calling_enabled=True,allow_phone_dialing=True);p.write_text(json.dumps(c,indent=2)+'\n')"
```

Then use **Dot Phone → Restart services** in the menu bar and check readiness again:

```sh
.venv/bin/python scripts/verify-setup.py
```

Ask Dot to check status. Expect **Ready for calls** and `can_request_native_dot_call: true`. To turn calling off again, run the same command with both values set to `False` and restart services.

Now explicitly ask Dot Phone Local to call you. Confirm audio in both directions, then hang up and confirm cleanup. If you configured incoming routing, call your Twilio number from the saved owner phone and enter the PIN. These are real telephone calls. Do not automatically retry a failed or uncertain outgoing request.

## Everyday use and updates

Keep the Mac awake, Chrome open, and Dot signed in. The menu-bar app starts at login. The app's **Open logs** action opens local diagnostics. You do not need `npm start` while the app runs; both would compete for port 8894.

After quitting, relaunch with:

```sh
launchctl kickstart "gui/$(id -u)/dev.dot-phone-call.app"
```

Keep `.secrets/` and the checkout path when updating. Make changes while no call is active. After pulling updates, rerun `npm ci --ignore-scripts` and the pip install from step 1 if the lockfiles changed, then apply what each changed area needs:

| Changed files | What to do |
| --- | --- |
| `browser/` or `chrome-extension/` | Rerun `node scripts/build-mac-extension.mjs --enable-calling --backend=local`, then reload **Dot Phone Call (local)** in `chrome://extensions`. Chrome keeps running the old copy until you reload it. |
| `src/` | **Dot Phone → Restart services**. |
| `mac-app/` | `npm run build:app`, then **Dot Phone → Restart services**. |

Do not run simultaneous calls from two checkouts of this project.

Upgrading from a version that used the native **Dot Phone Call Helper**: rebuild and reload the extension as above, restart services, and rerun `pip install -r requirements.lock`. The old helper is no longer launched. You can delete `.runtime/Dot Phone Call Helper.app` and `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/dev.dot_phone_call.helper.json`.

To stop the login installation while retaining source and private configuration:

```sh
.venv/bin/python scripts/install-background-app.py --restore
```

This removes the app login item; finish any call first. Installations migrated from this project's earlier separate login services are covered in [background app](docs/BACKGROUND-APP.md). To restore your Twilio number's previous incoming routing:

```sh
.venv/bin/python scripts/switch-incoming-route.py --restore --apply
```

## Uninstall

Finish any call, then:

1. Run `.venv/bin/python scripts/install-background-app.py --restore` to remove the login item.
2. If you switched incoming routing, restore it with the command above.
3. Remove **Dot Phone Call (local)** in `chrome://extensions`.
4. If an earlier version registered a Chrome native host, delete `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/dev.dot_phone_call.helper.json`.
5. Delete the checkout. Its `.secrets/` folder holds your Twilio and OpenAI credentials; revoke the Twilio API key and OpenAI runtime key in their consoles if you no longer need them.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Tunnel missing in ChatGPT | Match the account/workspace association and organization-level Tunnels Read + Use permissions. A Platform organization alone may not expose it to your ChatGPT workspace. |
| macOS blocks `tunnel-client` | Confirm you downloaded it from OpenAI's official release, then allow it under System Settings → Privacy & Security and run `.runtime/tunnel-client help quickstart` again. |
| Installer reports Node is too old | Run `nvm use` (or install Node 24+) so `node --version` shows 24 or later, then rerun the installer. |
| App installer fails | Check Node is on PATH, `.runtime/tunnel-client` is executable, the wizard completed, and ports 8894/8896 are unused. Inspect `.runtime/app-backend.log` and `.runtime/app-tunnel.log`. Fix the cause, then rerun the installer. |
| Microphone denied | Open the extension's **Options** page to enable microphone access. Check macOS microphone permission for Chrome. |
| Chrome disconnected | Load the extension in the correct profile and inspect its Errors page. Reload it after rebuilding. |
| `MAC_DEFAULT_OUTPUT_IS_BLACKHOLE` | Your Mac's default speaker is a BlackHole device, so every app's sound would join the call. Choose your normal speakers in System Settings → Sound (a disconnected headset can leave BlackHole selected), then retry. |
| Audio diagnostic fails | Inspect `.runtime/audio-diagnostic-result.json` and the extension's Errors page. Keep calling disabled until it passes. |
| Unexpected music/background audio | Calls route only Dot's tab and the hidden Twilio page to BlackHole. Check that no other app is set to output to BlackHole 2ch. |
| `DOT_OUTPUT_NOT_ROUTED` | Dot connected but its playback could not be sent to BlackHole 2ch. Reload the Dot tab and the extension, then retry. If it persists, Dot's page may have changed how it plays audio. |
| Dot controls unavailable | Confirm Dot voice works manually in Chrome. This integration depends on Dot's browser UI and may need updates when it changes. |
| Phone does not ring | Check Dot's call status, saved number format, Twilio account restrictions, and calling flags. A queued request is not a confirmed call. |

## Development and verification

```sh
npm test
npm run test:app
npm run test:setup
npm run test:tunnel
```

Automated tests use simulated provider calls and audio devices. `test:setup` uses temporary directories and mocked service installation. `test:tunnel` checks the local MCP probe over stdio, not your real OpenAI tunnel. None of these commands dial a phone. Call progress, including how Dot's audio was routed, is logged to `.runtime/app-backend.log`.

See [architecture](docs/ARCHITECTURE.md), [background app](docs/BACKGROUND-APP.md), and [validation history](docs/VALIDATION.md). The history records the original installation's tests; it is not a prerequisite or a source of credentials for your installation. The latest hidden-bridge validation used no phone call. A fresh-account end-to-end install and full reboot have not been independently verified yet.

### Known dependency advisory

The 2026-10-25 clean install reported one high-severity npm advisory against the locked Zod 3 dependency: [GHSA-mrhx-c7xc-m3w5](https://github.com/advisories/GHSA-mrhx-c7xc-m3w5). npm proposes a major-version update. That migration has not been tested here; do not treat a successful setup or unit-test run as resolution of the advisory. The local backend already bounds request bodies, but that is not a dependency patch.
