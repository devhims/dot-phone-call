import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';

const support = {
  plugin_transport: 'streamable-http',
  native_dot_audio: 'not_integrated',
  can_place_native_dot_call: false,
  additional_llm: false,
  reason: 'A supported connection to Dot native audio has not been established. Tool discovery alone does not establish audio access.',
};
// Strip platform RPC disposal symbols at the JSON protocol boundary.
const result = data => {
  const text = JSON.stringify(data);
  return {content:[{type:'text',text}],structuredContent:JSON.parse(text)};
};

function callControls(call) {
  if (!call.id || !['queued','starting','ringing','connected'].includes(call.status)) return call;
  return {...call, hangup:{tool:'end_owner_call',arguments:{call_id:call.id},
    instruction:'Invoke this tool when the owner asks to hang up, disconnect, cancel, or end this call. Saying OK does not disconnect it.'}};
}

export function makeMcpServer({ readStatus = async () => ({ configured: false }), nativeStatus, startCall, getCall, endCall } = {}) {
  const server = new McpServer({ name: 'dot-phone-call', version: '0.3.1' }, {
    instructions: 'Use start_owner_call to call the owner with this Dot’s native voice through the Mac companion. Use a unique request_key for each intentional call and reuse it when checking an uncertain result. Queued means accepted, not connected. Use get_owner_call for progress. Do not retry with a new key after failure unless the owner requests another call. When the owner asks to hang up, end, or stop the current phone call, invoke end_owner_call immediately. Omit call_id to end the current owner call, including in a voice session that does not know the ID. A spoken acknowledgment does not hang up. If tools are unavailable in voice mode, say so instead of claiming the call has ended. prepare_owner_call is read-only. Never substitute Personal Calls or another voice model. The helper rings the phone after local audio and bridge checks, starts Dot voice when the owner answers, and restores audio after hangup.',
  });
  server.registerTool('get_phone_service_status', {
    title: 'Check the phone plugin connection',
    description: 'Check whether this plugin is reachable and the existing phone service is enabled. Returns native Dot audio readiness separately. Never places a call or invokes a language model.',
    inputSchema: {},
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, async () => {
    try { return result({ ...support, ...(nativeStatus ? await nativeStatus() : {}), phone_service: await readStatus() }); }
    catch { return { ...result({ ...support, phone_service: { configured: true, reachable: false }, error: 'Phone service status is unavailable.' }), isError: true }; }
  });
  server.registerTool('prepare_owner_call', {
    title: 'Check native Dot call readiness, without dialing',
    description: 'Preflight the owner-only calling workflow. Check whether the Mac companion is online and ready without dialing. No text-to-speech or separate model is substituted.',
    inputSchema: { request_key: z.string().min(1).max(100) },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  }, async () => {
    const ready = nativeStatus ? await nativeStatus() : support;
    return result({ ...ready, status:ready.can_request_native_dot_call ? 'preflight_required' : 'blocked', call_placed:false,
      code:ready.can_request_native_dot_call ? 'AUDIO_PREFLIGHT_REQUIRED' : nativeStatus ? 'MAC_COMPANION_UNAVAILABLE' : 'NATIVE_DOT_AUDIO_NOT_INTEGRATED' });
  });
  if (startCall && getCall && endCall) {
    server.registerTool('start_owner_call', {
      title:'Call me with Dot’s native voice',
      description:'Place one call to the saved owner through the paired Mac. Automatically starts this account’s Dot browser voice session and restores audio after hangup. Requires the Mac companion and Chrome extension online. No additional LLM, TTS, arbitrary phone number, or question text. Reuse request_key on retries. Returns queued status and a hangup tool with this call’s ID. Use that tool when asked to disconnect; do not only acknowledge. Check get_owner_call for progress.',
      inputSchema:{request_key:z.string().min(8).max(100),duration_seconds:z.number().int().min(20).max(600).default(120)},
      annotations:{readOnlyHint:false,destructiveHint:false,idempotentHint:true,openWorldHint:true},
    }, async input => result(callControls(await startCall(input))));
    server.registerTool('get_owner_call', {
      title:'Check Dot phone call progress',description:'Read a previously requested call. Does not dial or retry.',
      inputSchema:{call_id:z.string().uuid()},
      annotations:{readOnlyHint:true,destructiveHint:false,idempotentHint:true,openWorldHint:false},
    }, async input => result(callControls(await getCall(input))));
    server.registerTool('end_owner_call', {
      title:'End the Dot phone call',description:'Hang up now when the owner asks to end or stop the phone call. Omit call_id to end the current owner call, or supply an ID for a specific call. Do not merely acknowledge verbally. The helper also closes browser voice and restores audio devices.',
      inputSchema:{call_id:z.string().uuid().optional()},
      annotations:{readOnlyHint:false,destructiveHint:false,idempotentHint:true,openWorldHint:true},
    }, async input => result(await endCall(input)));
  }
  return server;
}
