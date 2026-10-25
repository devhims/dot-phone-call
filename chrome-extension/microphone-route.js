// Runs in the page's MAIN world before Dot can cache getUserMedia. No pairing
// credentials or audio samples cross this interface. Unowned calls pass through.
(() => {
  if (globalThis.dotPhoneMicrophone?.version === '0.1.7') return;
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
  globalThis.dotPhoneMicrophone = {
    version:'0.1.7',
    async arm(jobId, deadline) {
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
      const value = {jobId, deadline, deviceId:device.deviceId, label:device.label, tracks:new Set(), error:null};
      session = value;
      value.timer = setTimeout(() => {
        if (session === value) {value.error = 'DOT_MICROPHONE_LEASE_EXPIRED'; stop(value);}
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
        ...(error ? {error} : {})};
    },
    suspend(jobId) {
      if (session && session.jobId !== jobId) throw new Error('DOT_MICROPHONE_OWNER_MISMATCH');
      generation++;
      if (session) {
        session.error = 'CALL_CANCELED';
        session.deadline = 0;
        stop(session);
      }
      return {suspended:true};
    },
    disarm(jobId) {
      if (session && session.jobId !== jobId) throw new Error('DOT_MICROPHONE_OWNER_MISMATCH');
      generation++;
      if (session) {clearTimeout(session.timer); stop(session); session = null;}
      return {released:true};
    },
  };
})();
