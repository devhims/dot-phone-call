// Runs in the page's MAIN world before Dot can cache getUserMedia. No pairing
// credentials or audio samples cross this interface. Unowned calls pass through.
(() => {
  if (globalThis.dotPhoneMicrophone?.version === '0.1.9') return;
  const media = navigator.mediaDevices;
  if (!media?.getUserMedia) return;
  const original = media.getUserMedia;
  let session = null;
  let generation = 0;
  const stop = value => { for (const track of value.tracks) track.stop(); };
  const expired = value => Date.now() >= value.deadline;
  const valid = (track, value) => track.readyState === 'live' && track.enabled && !track.muted
    && track.getSettings().deviceId === value.deviceId;
  const wrapped = async function(constraints) {
    const value = session;
    if (!value || !constraints?.audio) return original.call(media, constraints);
    if (expired(value)) {
      stop(value);
      throw new Error('DOT_MICROPHONE_LEASE_EXPIRED');
    }
    const audio = typeof constraints.audio === 'object' ? {...constraints.audio} : {};
    // An explicit physical-device selection must not override the phone route.
    delete audio.groupId;
    audio.deviceId = {exact:value.deviceId};
    let stream;
    try {
      stream = await original.call(media, {...constraints, audio});
      if (session !== value || expired(value)) throw new Error('CALL_CANCELED');
      const tracks = stream.getAudioTracks();
      if (!tracks.length || tracks.some(track => track.getSettings().deviceId !== value.deviceId)) {
        throw new Error('DOT_MICROPHONE_DEVICE_MISMATCH');
      }
      for (const track of tracks) value.tracks.add(track);
      return stream;
    } catch (error) {
      for (const track of stream?.getTracks() || []) track.stop();
      value.error = /^(DOT_|CALL_)[A-Z_]+$/.test(error.message)
        ? error.message : 'DOT_MICROPHONE_CAPTURE_FAILED';
      throw error;
    }
  };
  media.getUserMedia = wrapped;

  // Experiment: route Dot's playback to BlackHole 2ch for an armed call only,
  // instead of switching the Mac's default output. Records how Dot plays audio.
  const players = new Set(), contexts = new Set();
  const output = {seen_elements:0, seen_contexts:0, routed_elements:0, routed_contexts:0, errors:[]};
  const live = refs => [...refs].map(ref => ref.deref()).filter(Boolean);
  // Dot closes a context after each voice session; closed contexts cannot play
  // and Chrome rejects changing their output, so they are skipped, not errors.
  const closed = target => target.state === 'closed';
  async function route(target, kind) {
    const value = session;
    if (!value?.outputId || value.routed.has(target) || closed(target)) return;
    value.routed.add(target);
    try {
      await target.setSinkId(value.outputId);
      if (session === value) output['routed_' + kind] += 1;
    } catch (error) {
      value.routed.delete(target);
      if (closed(target)) return;  // Closed while routing.
      if (output.errors.length < 8) output.errors.push(kind + ':' + (error?.name || 'Error'));
    }
  }
  function trackPlayer(element) {
    if (!element || players.has(element.__dotPhoneRef)) return;
    element.__dotPhoneRef = new WeakRef(element);
    players.add(element.__dotPhoneRef);
    output.seen_elements += 1;
    void route(element, 'elements');
  }
  if (typeof HTMLMediaElement !== 'undefined' && HTMLMediaElement.prototype.setSinkId) {
    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function(...args) {trackPlayer(this); return play.apply(this, args);};
    const source = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
    if (source?.set) Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {...source,
      set(value) {source.set.call(this, value); if (value) trackPlayer(this);}});
    globalThis.addEventListener?.('play', event => trackPlayer(event.target), true);
  }
  for (const name of ['AudioContext', 'webkitAudioContext']) {
    const Original = globalThis[name];
    if (typeof Original !== 'function' || !Original.prototype.setSinkId) continue;
    globalThis[name] = class extends Original {
      constructor(...args) {
        super(...args);
        contexts.add(new WeakRef(this));
        output.seen_contexts += 1;
        void route(this, 'contexts');
      }
    };
  }
  async function routeExisting() {
    for (const element of live(players)) await route(element, 'elements');
    for (const element of globalThis.document?.querySelectorAll?.('audio,video') ?? []) trackPlayer(element);
    for (const ref of [...contexts]) {
      const context = ref.deref();
      if (!context || closed(context)) contexts.delete(ref);
      else await route(context, 'contexts');
    }
  }
  async function restoreOutput(value) {
    for (const target of value.routed ?? []) {
      try {await target.setSinkId('');} catch {}
    }
    value.routed?.clear();
  }
  globalThis.dotPhoneMicrophone = {
    version:'0.1.9',
    async arm(jobId, deadline, options = {}) {
      if (session) {
        if (session.jobId !== jobId) throw new Error('DOT_MICROPHONE_ALREADY_OWNED');
        return this.status(jobId);
      }
      if (media.getUserMedia !== wrapped) throw new Error('DOT_MICROPHONE_HOOK_REPLACED');
      if (!jobId || !Number.isFinite(deadline) || deadline <= Date.now()
          || deadline > Date.now() + 30 * 60 * 1000) throw new Error('DOT_MICROPHONE_INVALID_LEASE');
      const pendingGeneration = generation;
      const devices = await media.enumerateDevices();
      if (generation !== pendingGeneration) throw new Error('CALL_CANCELED');
      const device = devices.find(d => d.kind === 'audioinput' && /^BlackHole\s+16ch(?:\s+\([^)]*\))?$/i.test(d.label.trim())
        && d.deviceId && d.deviceId !== 'default' && d.deviceId !== 'communications');
      if (!device) {
        const error = new Error(devices.some(d => d.kind === 'audioinput' && d.label)
          ? 'BLACKHOLE_MICROPHONE_NOT_FOUND' : 'DOT_MICROPHONE_PERMISSION_REQUIRED');
        error.diagnostics = {
          input_count:devices.filter(d => d.kind === 'audioinput').length,
          blackhole_inputs:devices.filter(d => d.kind === 'audioinput' && /blackhole/i.test(d.label))
            .map(d => ({label:d.label.slice(0,160), default_alias:['default','communications'].includes(d.deviceId)})),
        };
        throw error;
      }
      // Recheck after the asynchronous device lookup.
      if (session) throw new Error('DOT_MICROPHONE_ALREADY_OWNED');
      const value = {jobId, deadline, deviceId:device.deviceId, label:device.label, tracks:new Set(), error:null, routed:new Set()};
      if (options?.output === 'blackhole-2ch') {
        const speaker = devices.find(d => d.kind === 'audiooutput' && /^BlackHole\s+2ch(?:\s+\([^)]*\))?$/i.test(d.label.trim()));
        if (!speaker?.deviceId) throw new Error('BLACKHOLE_OUTPUT_NOT_FOUND');
        value.outputId = speaker.deviceId;
        output.routed_elements = output.routed_contexts = 0;
        output.errors.length = 0;
      }
      session = value;
      if (value.outputId) await routeExisting();
      value.timer = setTimeout(() => {
        if (session === value) {value.error = 'DOT_MICROPHONE_LEASE_EXPIRED'; stop(value); void restoreOutput(value);}
      }, Math.max(0, deadline - Date.now()));
      return this.status(jobId);
    },
    status(jobId) {
      const value = session;
      if (!value || value.jobId !== jobId) return {microphone_verified:false};
      const error = value.error || (expired(value) ? 'DOT_MICROPHONE_LEASE_EXPIRED' : null)
        || (media.getUserMedia !== wrapped ? 'DOT_MICROPHONE_HOOK_REPLACED' : null);
      return {armed:true, microphone:value.label,
        microphone_verified:!error && [...value.tracks].some(track => valid(track, value)),
        ...(value.outputId ? {output:{device:'BlackHole 2ch', ...output, errors:[...output.errors]},
          output_verified:output.routed_elements + output.routed_contexts > 0 && !output.errors.length} : {}),
        ...(error ? {error} : {})};
    },
    suspend(jobId) {
      if (session && session.jobId !== jobId) throw new Error('DOT_MICROPHONE_OWNER_MISMATCH');
      generation++;
      if (session) {
        session.error = 'CALL_CANCELED';
        session.deadline = 0;
        stop(session);
        void restoreOutput(session);
      }
      return {suspended:true};
    },
    disarm(jobId) {
      if (session && session.jobId !== jobId) throw new Error('DOT_MICROPHONE_OWNER_MISMATCH');
      generation++;
      if (session) {clearTimeout(session.timer); stop(session); void restoreOutput(session); session = null;}
      return {released:true};
    },
  };
})();
