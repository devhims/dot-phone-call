// Pure state machine. Its owner Durable Object commits each operation atomically.
export const TERMINAL = new Set(['completed','failed','canceled','expired']);
const active = job => !TERMINAL.has(job.status);
const offlineReason = 'Keep Chrome open with calling enabled in the paired extension. The Mac audio helper starts on demand.';
const challengeWords = new Set('purple yellow silver orange garden window mountain river rabbit tiger eagle dolphin'.split(' '));
function audioDiagnostics(value) {
  if (!value || value.stage !== 'browser_audio_preflight' || value.phone_dialed !== false) return;
  const expected = typeof value.expected_response === 'string' ? value.expected_response.split(' ') : [];
  if (expected.length !== 3 || new Set(expected).size !== 3 || expected.some(w => !challengeWords.has(w))) return;
  if (!Array.isArray(value.heard_challenge_words) || value.heard_challenge_words.length > 12
      || value.heard_challenge_words.some(w => !expected.includes(w))) return;
  if (!Number.isFinite(value.reply_peak) || value.reply_peak < 0 || value.reply_peak > 1
      || !Number.isFinite(value.active_seconds) || value.active_seconds < 0 || value.active_seconds > 120) return;
  return {stage:'browser_audio_preflight',phone_dialed:false,
    expected_response:expected.join(' '),heard_challenge_words:value.heard_challenge_words.slice(),
    reply_peak:value.reply_peak,active_seconds:value.active_seconds};
}
function bridgeDiagnostics(value) {
  if (!value || value.stage !== 'public_bridge_preflight' || value.phone_dialed !== false) return;
  if (!['dns_lookup_failed','tls_failed','http_error','connection_failed','timeout','unexpected_response'].includes(value.reason)
      || !Number.isInteger(value.attempts) || value.attempts < 1 || value.attempts > 100
      || !Number.isFinite(value.elapsed_seconds) || value.elapsed_seconds < 0 || value.elapsed_seconds > 60) return;
  const result={stage:value.stage,phone_dialed:false,reason:value.reason,
    attempts:value.attempts,elapsed_seconds:value.elapsed_seconds};
  if (value.http_status !== undefined) {
    if (value.reason !== 'http_error' || !Number.isInteger(value.http_status) || value.http_status < 100 || value.http_status > 599) return;
    result.http_status=value.http_status;
  }
  return result;
}
export function operate(state, action, input = {}, now = Date.now()) {
  state.jobs ??= [];
  state.jobs = state.jobs.filter(j => now-j.created_at < 86400000);
  for (const job of state.jobs) {
    if (active(job) && now > job.deadline) { job.status = 'expired'; job.updated_at = now; }
  }
  const current = () => state.jobs.find(active);
  const online = () => !!state.mac?.ready && now-state.mac.seen_at < 20000;
  const guarded = () => state.mac?.audio_guard_version === 1;
  const view = job => job ? {...job, request_key: undefined, stream_url: undefined} : {status:'not_found'};
  if (action.startsWith('inbound_')) {
    state.inbound ??= {};
    for (const [sid, entry] of Object.entries(state.inbound)) {
      if (now-entry.created_at > 86400000) delete state.inbound[sid];
    }
    state.pin_failures = (state.pin_failures ?? []).filter(t => now-t < 900000);
    const sid = input.inbound_call_sid;
    if (action !== 'inbound_ready' && !/^CA[0-9a-f]{32}$/.test(sid ?? '')) return {status:'denied'};
    const entry = state.inbound[sid];
    const job = state.jobs.find(j => j.inbound_call_sid === sid);
    if (action === 'inbound_status') {
      if (['completed','failed','busy','no-answer','canceled'].includes(input.status)) {
        state.inbound[sid] = {...entry,created_at:entry?.created_at??now,closed:true};
        if (job && active(job)) {
          job.cancel_requested = true;
          if (job.status === 'queued') job.status = 'canceled';
        }
      }
      return {accepted:true};
    }
    if (action === 'inbound_ready') {
      const owned = state.jobs.find(j => j.id === input.call_id && j.instance === input.instance && j.direction === 'inbound');
      if (!owned || !active(owned) || owned.cancel_requested || now-owned.created_at > 90000
          || state.inbound[owned.inbound_call_sid]?.closed) return {accepted:false};
      if (!/^wss:\/\/[a-z0-9-]+\.trycloudflare\.com\/media\/[a-f0-9]{48}$/.test(input.stream_url ?? '')) return {accepted:false};
      if (owned.stream_url && owned.stream_url !== input.stream_url) return {accepted:false};
      owned.stream_url = input.stream_url;
      return {accepted:true};
    }
    if (action === 'inbound_wait') {
      if (!entry || entry.closed || !job || !active(job) || job.cancel_requested) return {status:'ended'};
      if (now-job.created_at > 90000) {
        job.cancel_requested = true;
        if (job.status === 'queued') job.status = 'canceled';
        return {status:'timeout'};
      }
      return job.stream_url ? {status:'ready',stream_url:job.stream_url} : {status:'waiting'};
    }
    if (entry?.closed || (entry && now-entry.created_at > 120000)) return {status:'denied'};
    if (job) return active(job) && !job.cancel_requested ? {status:'waiting'} : {status:'denied'};
    if (state.pin_failures.length >= 10 || (entry?.attempts ?? 0) >= 3) return {status:'denied'};
    if (!online() || !guarded()) return {status:'offline'};
    if (state.mac.busy || current()) return {status:'busy'};
    if (action === 'inbound_open') {
      if (!entry) {
        if (Object.keys(state.inbound).length >= 200) return {status:'denied'};
        state.inbound[sid] = {created_at:now,attempts:0};
      }
      return {status:'pin_required'};
    }
    if (action === 'inbound_pin') {
      if (!entry) return {status:'denied'};
      entry.attempts++;
      if (input.pin_ok !== true) {
        state.pin_failures.push(now);
        return {status:entry.attempts < 3 && state.pin_failures.length < 10 ? 'pin_required' : 'denied'};
      }
      const queued = operate(state,'start', {request_key:'inbound:'+sid,duration_seconds:600},now);
      if (queued.status !== 'queued') return {status:'busy'};
      const incoming = state.jobs.find(j => j.id === queued.id);
      incoming.direction = 'inbound';
      incoming.inbound_call_sid = sid;
      incoming.deadline = now+90000;
      return {status:'waiting'};
    }
    return {status:'denied'};
  }
  if (action === 'status') return {
    inbound_supported:true,
    native_dot_audio:'mac_blackhole', additional_llm:false, mac_online:online(),
    reason:online() ? 'The helper checks the local audio route and public bridge before dialing, then starts Dot when the phone answers. Spoken audio diagnostics run separately.' : offlineReason,
    can_request_native_dot_call:online() && guarded() && !state.mac.busy && !current(),
    can_place_native_dot_call:false,
    audio_preflight_required:true,
    audio_guard_available:guarded(),
    active_call:current() ? view(current()) : null,
  };
  if (action === 'start') {
    const existing = state.jobs.find(j => j.request_key === input.request_key);
    if (existing) return {...view(existing), duplicate:true};
    if (!online()) return {status:'blocked', code:'MAC_COMPANION_OFFLINE', reason:offlineReason};
    if (!guarded()) return {status:'blocked', code:'AUDIO_CHECK_UPDATE_REQUIRED'};
    if (state.mac.busy || current()) return {status:'blocked', code:'CALL_ALREADY_ACTIVE'};
    if (state.jobs.length >= 60) return {status:'blocked', code:'DAILY_CALL_LIMIT'};
    const job = {id:crypto.randomUUID(), request_key:input.request_key,
      duration_seconds:input.duration_seconds, status:'queued', created_at:now,
      updated_at:now, deadline:now+45000, cancel_requested:false};
    state.jobs.push(job);
    return {...view(job), call_placed:false};
  }
  if (action === 'get') return view(state.jobs.find(j => j.id === input.call_id));
  if (action === 'end') {
    const job = input.call_id === undefined ? current() : state.jobs.find(j => j.id === input.call_id);
    if (job && active(job)) {
      job.cancel_requested = true;
      if (job.status === 'queued') job.status = 'canceled';
      job.updated_at = now;
    }
    return view(job);
  }
  if (action === 'pulse') {
    if (state.mac && state.mac.instance !== input.instance && now-state.mac.seen_at < 20000)
      return {error:'ANOTHER_COMPANION_CONNECTED'};
    state.mac = {instance:input.instance, ready:input.ready===true, busy:input.busy===true,
      audio_guard_version:input.audio_guard_version===1 ? 1 : 0, seen_at:now};
    const job = current();
    if (job?.status === 'queued' && input.ready && !input.busy && guarded()) {
      job.status = 'starting';
      job.updated_at = now;
      job.deadline = now+(job.duration_seconds+240)*1000;
      job.instance = input.instance;
      return {job:view(job)}; // At most once: never resend a claimed call.
    }
    return {cancel_call_id:job?.cancel_requested ? job.id : null};
  }
  if (action === 'update') {
    const job = state.jobs.find(j => j.id === input.call_id && j.instance === input.instance);
    if (!job || TERMINAL.has(job.status)) return {accepted:false};
    const order = {starting:0,ringing:1,connected:2,completed:3,failed:3,canceled:3};
    if (!(input.status in order) || order[input.status] < order[job.status]) return {accepted:false};
    job.status = input.status; job.updated_at = now;
    if (typeof input.code === 'string' && /^[A-Z_]{1,60}$/.test(input.code)) job.code=input.code;
    if (input.status === 'failed' && ['DOT_AUDIO_REPLY_MISMATCH','DOT_AUDIO_SILENT'].includes(input.code)) {
      const diagnostics=audioDiagnostics(input.diagnostics);
      if (diagnostics) job.diagnostics=diagnostics;
    }
    if (input.status === 'failed' && input.code === 'PUBLIC_BRIDGE_UNREACHABLE') {
      const diagnostics=bridgeDiagnostics(input.diagnostics);
      if (diagnostics) job.diagnostics=diagnostics;
    }
    return {accepted:true};
  }
  throw new Error('Unknown operation');
}
