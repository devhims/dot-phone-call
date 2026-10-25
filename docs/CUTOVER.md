# Local cutover, 25 October 2026

Dot Phone Local now exposes all five calling tools through the Personal Secure MCP Tunnel. The existing Dot Phone hosted plugin remains installed as an explicit fallback. Use the local plugin by name to avoid ambiguity. Never retry an uncertain call through the other service automatically.

The saved Twilio number now uses `https://handler.twilio.com/twiml/EH2c89dfa4e108955273943c91da6c429f`. This Twilio-hosted template collects six digits and routes to the new registered browser client. Caller-number and PIN authorization, queueing, and call control remain on the Mac. No Cloudflare endpoint is used by the primary incoming route.

The backend and tunnel have independent login services, `dev.dot-phone-call.backend` and `dev.dot-phone-call.mcp-tunnel`. Chrome and the Mac must be running. The extension restores the registered-client page after backend restarts. The native audio helper starts only when needed.

## Verified without calling a phone

- The five local tools were discovered by ChatGPT. Status and prepare requests executed successfully through Secure MCP Tunnel.
- The registered Twilio browser client reconnected after backend restart without a user click.
- Signed HTTP POST requests to the actual TwiML Bin returned HTTP 200 for both the PIN-prompt and client-routing branches. Parsed XML contained the expected client identity and supplied test digits. Fetching TwiML does not execute a call.
- Twilio number configuration was read back after switching, including the hosted fallback URL. Existing status callbacks were preserved.
- 83 JavaScript tests pass. The previous 116 Python tests and live two-way Dot audio diagnostic are documented in VALIDATION.md; they were not rerun for this routing-only cutover.
- All 115 original-project source files match the snapshot. The hosted fallback status tool responds, with no active call.

Incoming and outgoing calling are enabled. The owner explicitly approved enabling outgoing calls for future requests on 25 October 2026, with no test call to be placed. The backend was restarted to apply the setting. No phone-number call was placed during this cutover. Actual incoming phone-network behavior remains unverified.

## Fallback and rollback

The old hosted incoming URL is configured as Twilio's webhook-error fallback. This covers failure to retrieve the primary TwiML, not a sleeping Mac or an unavailable registered client. The old hosted plugin can still be used explicitly for outgoing requests.

The complete previous number configuration is saved privately in `.secrets/incoming-route-fallback.json`. To restore it, from this project run:

```sh
.venv/bin/python scripts/switch-incoming-route.py --restore --apply
```

This restores the original incoming routing and callbacks, then verifies them with a fresh API read. It does not place a call or modify the old project. Local calling flags are in `.secrets/local-backend.json`; restart the new backend after any authorized configuration change.

## Subsequent live verification

After the owner authorized a phone test, startup permission and heartbeat issues were fixed and a live outgoing call passed with owner-confirmed clear audio both ways. Tool hangup and device restoration also passed. See VALIDATION.md for the test evidence. Incoming phone-network behavior remains untested.

The owner subsequently confirmed the incoming call test passed. Startup management has since moved to the single Dot Phone menu-bar app; see BACKGROUND-APP.md. The old hosted fallback remains unchanged.
