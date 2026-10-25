// Diagnostic only: copy a bounded sample of generated probe audio and pass every
// encoded frame through unchanged. Never enabled for the saved-number route.
const states = {};
self.onrtctransform = event => {
  const {readable, writable, options} = event.transformer;
  const state = states[options.direction] = {frames: 0, configurations: {}, packets: []};
  readable.pipeThrough(new TransformStream({transform(frame, controller) {
    const bytes = new Uint8Array(frame.data);
    state.frames++;
    if (bytes.length) {
      const configuration = bytes[0] >> 3;
      state.configurations[configuration] = (state.configurations[configuration] || 0) + 1;
      if (state.frames > 100 && state.frames <= 150) {
        state.packets.push(btoa(String.fromCharCode(...bytes)));
      }
    }
    controller.enqueue(frame);
    if (state.frames % 50 === 0) self.postMessage({direction: options.direction, ...state});
  }})).pipeTo(writable).catch(error => self.postMessage({error: error.message}));
};
