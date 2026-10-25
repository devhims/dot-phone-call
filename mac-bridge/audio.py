"""Two isolated virtual audio paths. No recording, speech model or transcription."""
import audioop
import threading
from collections import deque

import numpy as np
import sounddevice as sd
from phone_voice import PhoneVoice
from resampling import PhoneResampler

RATE = 48000
BLOCK = 960  # 20 ms, matched to Twilio's 160 samples at 8 kHz.


def encode(samples):
    pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()
    return audioop.lin2ulaw(pcm, 2)


def decode(payload):
    return np.frombuffer(audioop.ulaw2lin(payload, 2), dtype='<i2').astype(np.float32) / 32768


class SampleQueue:
    """Bounded FIFO; discard old audio instead of accumulating conversation delay.

    With a drain target, delay above the target is removed only from quiet
    10 ms chunks, so network bursts shorten pauses instead of cutting words.
    The capacity remains a hard bound for sustained bursts.
    """
    QUIET_RMS = .01  # About -40 dBFS.

    def __init__(self, capacity=RATE // 4, prebuffer=0, drain_above=None):
        self.data = np.empty(0, dtype=np.float32)
        self.capacity = capacity
        self.lock = threading.Lock()
        self.dropped = self.drained = 0
        self.drain_above = drain_above
        self.prebuffer = prebuffer
        self.priming = bool(prebuffer)
        self.started = False
        self.underrun_samples = self.rebuffers = 0

    def put(self, data):
        with self.lock:
            joined = np.concatenate((self.data, data))
            self.dropped += max(0, len(joined) - self.capacity)
            self.data = joined[-self.capacity:]

    def take(self, size):
        with self.lock:
            out = np.zeros(size, dtype=np.float32)
            if self.prebuffer:
                # Keep short late-arriving packets intact. Refill before resuming,
                # rather than repeatedly splicing silence into partial packets.
                if self.priming and len(self.data) < max(size, self.prebuffer):
                    if self.started:
                        self.underrun_samples += size
                    return out
                self.priming = False
                self.started = True
                if len(self.data) < size:
                    self.underrun_samples += size
                    self.rebuffers += 1
                    self.priming = True
                    return out
            if self.drain_above is not None:
                self._drain_quiet(size)
            count = min(size, len(self.data))
            out[:count] = self.data[:count]
            self.data = self.data[count:]
            return out

    def _drain_quiet(self, size):
        chunk = RATE // 100
        # Look past audio already due now; remove at most one packet per call.
        removed = 0
        while removed < size and len(self.data) - chunk >= size + self.drain_above:
            head = self.data[:chunk]
            if float(np.sqrt(np.mean(head * head))) >= self.QUIET_RMS:
                break
            self.data = self.data[chunk:]
            self.drained += chunk
            removed += chunk

    def clear(self):
        with self.lock:
            self.data = np.empty(0, dtype=np.float32)
            self.priming = bool(self.prebuffer)
            self.started = False


def find_device(name, direction):
    matches = [(i, d) for i, d in enumerate(sd.query_devices())
               if d['name'] == name and d[f'max_{direction}_channels'] >= 2]
    if len(matches) != 1:
        raise RuntimeError(f'Expected one {direction} device named {name}; found {len(matches)}')
    return matches[0][0]


class AudioBridge:
    def __init__(self):
        # Dot speaker -> BlackHole 2ch -> capture.
        # Phone -> playback -> BlackHole 16ch -> Dot microphone.
        self.capture_device = find_device('BlackHole 2ch', 'input')
        self.play_device = find_device('BlackHole 16ch', 'output')
        if self.capture_device == self.play_device:
            raise RuntimeError('Audio directions must use different devices')
        # Three 20 ms packets absorb modest arrival jitter. At steady cadence,
        # the first packet waits 40 ms while the next two arrive. International
        # calls deliver bursts after network stalls; keep up to one second of
        # burst, then return to 200 ms by shortening the caller's pauses.
        self.playback = SampleQueue(capacity=RATE, prebuffer=3 * BLOCK, drain_above=RATE // 5)
        self.capture = deque(maxlen=12)
        self.lock = threading.Lock()
        self.down = PhoneResampler(RATE, 8000)
        self.up = PhoneResampler(8000, RATE)
        self.voice = PhoneVoice()
        self.encoded = bytearray()
        self.running = False
        self.capture_frames = self.phone_frames = self.xruns = self.capture_drops = 0
        self.peak_dot = self.peak_phone = 0.0
        self.pre_call_capture_drops = 0
        self.encoded_dropped = self.capture_underrun_samples = 0

    def start(self):
        if self.running:
            return
        def capture(indata, frames, timing, status):
            if status:
                self.xruns += 1
            if not self.running:
                return
            mono = indata.mean(axis=1).copy()
            self.peak_dot = max(self.peak_dot, float(np.max(np.abs(mono))))
            self.capture_frames += frames
            with self.lock:
                if len(self.capture) == self.capture.maxlen:
                    self.capture_drops += 1
                self.capture.append(mono)

        def playback(outdata, frames, timing, status):
            if status:
                self.xruns += 1
            samples = self.playback.take(frames) if self.running else np.zeros(frames)
            outdata[:] = samples[:, None]

        self.input = sd.InputStream(device=self.capture_device, samplerate=RATE,
                                    channels=2, dtype='float32', blocksize=BLOCK,
                                    latency='low', callback=capture)
        try:
            self.output = sd.OutputStream(device=self.play_device, samplerate=RATE,
                                         channels=2, dtype='float32', blocksize=BLOCK,
                                         latency='low', callback=playback)
            self.running = True
            self.output.start()
            self.input.start()
        except BaseException:
            self.close()
            raise

    def receive(self, payload):
        pcm = decode(payload)
        self.phone_frames += len(pcm)
        self.peak_phone = max(self.peak_phone, float(np.max(np.abs(pcm), initial=0)))
        self.playback.put(self.up.resample_chunk(pcm))
        return float(np.sqrt(np.mean(pcm * pcm))) if len(pcm) else 0

    def next_packet(self):
        with self.lock:
            blocks = list(self.capture)
            self.capture.clear()
        for block in blocks:
            self.encoded.extend(encode(self.voice.process(self.down.resample_chunk(block))))
        # Preserve short stalls; trim only excess over 240 ms on longer stalls.
        # Keep complete 20 ms packets instead of throwing away the whole queue.
        excess = max(0, len(self.encoded) - 12 * 160)
        if excess:
            trimmed = ((excess + 159) // 160) * 160
            del self.encoded[:trimmed]
            self.encoded_dropped += trimmed
        if len(self.encoded) < 160:
            self.capture_underrun_samples += 160
            return b'\xff' * 160
        packet = bytes(self.encoded[:160])
        del self.encoded[:160]
        return packet

    def discard_reply(self):
        with self.lock:
            self.capture.clear()
        self.encoded.clear()
        self.down.clear()
        self.voice.clear()

    def reset_for_call(self):
        self.playback.clear()
        self.up.clear()
        self.discard_reply()
        self.pre_call_capture_drops += self.capture_drops
        self.capture_frames = self.phone_frames = self.xruns = self.capture_drops = 0
        self.peak_dot = self.peak_phone = 0.0
        self.playback.dropped = self.playback.drained = 0
        self.voice.reset_metrics()
        self.playback.underrun_samples = self.playback.rebuffers = 0
        self.encoded_dropped = self.capture_underrun_samples = 0

    def close(self):
        self.running = False
        self.playback.clear()
        for name in ('input', 'output'):
            stream = getattr(self, name, None)
            if stream is not None:
                setattr(self, name, None)
                try:
                    stream.abort()
                finally:
                    stream.close()

    def metrics(self):
        return dict(dot_audio_seconds=round(self.capture_frames/RATE, 2),
                    phone_audio_seconds=round(self.phone_frames/8000, 2),
                    dot_peak=round(self.peak_dot, 4), phone_peak=round(self.peak_phone, 4),
                    audio_xruns=self.xruns, capture_drops=self.capture_drops,
                    pre_call_capture_drops=self.pre_call_capture_drops,
                    playback_dropped_samples=self.playback.dropped,
                    playback_drained_ms=round(self.playback.drained/RATE*1000, 2),
                    playback_underrun_ms=round(self.playback.underrun_samples/RATE*1000, 2),
                    playback_rebuffers=self.playback.rebuffers,
                    encoded_dropped_ms=round(self.encoded_dropped/8, 2),
                    capture_underrun_ms=round(self.capture_underrun_samples/8, 2),
                    resampler=self.down.name, resampler_delay_ms=self.down.delay_ms,
                    **self.voice.metrics())
