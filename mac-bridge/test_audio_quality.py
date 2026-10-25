"""Audio continuity regressions. No devices, credentials, network, or phone calls."""
import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import numpy as np

from audio import AudioBridge, BLOCK, decode, encode
from audio_check import DOT_TO_PHONE_RMS
from bridge import Session
from phone_voice import PhoneVoice
from resampling import PhoneResampler


def audio_without_devices():
    with patch('audio.find_device', side_effect=[0, 1]):
        return AudioBridge()


def tone(frequency, amplitude, seconds, rate=8000):
    return (amplitude * np.sin(2 * np.pi * frequency * np.arange(int(rate * seconds)) / rate)).astype('float32')


def rms_db(samples):
    return 20 * np.log10(np.sqrt(np.mean(np.square(samples, dtype='float64'))) + 1e-12)


def shaped(source, chunk=160):
    voice = PhoneVoice()
    return np.concatenate([voice.process(source[i:i + chunk]) for i in range(0, len(source), chunk)])


class VoiceLevelTests(unittest.TestCase):
    def test_quiet_dot_speech_is_raised_toward_phone_level(self):
        audio = audio_without_devices()
        # 997 Hz at -25 dBFS RMS, a quiet Dot reply.
        source = tone(997, .08, 2, 48000)
        out = []
        for block in np.split(source, 100):
            audio.capture.append(block)
            packet = audio.next_packet()
            self.assertEqual(len(packet), 160)
            out.extend(decode(packet))
        settled = np.array(out[8000:])
        self.assertGreater(rms_db(settled) - rms_db(source), 9)
        self.assertLess(float(np.max(abs(settled))), .96)
        self.assertEqual(audio.capture_underrun_samples, 0)
        metrics = audio.metrics()
        self.assertAlmostEqual(metrics['dot_active_level_dbfs'], -25, delta=.5)
        self.assertGreater(metrics['phone_active_level_dbfs'], -17)

    def test_pre_call_tone_check_accepts_leveled_output(self):
        # Mirrors check_bridge_audio: 0.08 x 997 Hz from Dot, 75 packets, skip 200 ms.
        audio = audio_without_devices()
        source = tone(997, .08, 1.5, 48000)
        out = []
        for block in np.split(source, 75):
            audio.capture.append(block)
            out.append(decode(audio.next_packet()))
        rms = float(np.sqrt(np.mean(np.concatenate(out)[1600:] ** 2)))
        low, high = DOT_TO_PHONE_RMS
        self.assertTrue(low * 2 < rms < high / 1.5, rms)

    def test_bass_below_phone_band_cut_and_consonant_band_lifted(self):
        def response(frequency):
            # Equalizer only; the leveler would otherwise normalize each tone.
            return rms_db(PhoneVoice()._equalize(tone(frequency, .05, 2))[8000:])
        reference = response(1000)
        self.assertLess(response(120) - reference, -10)
        self.assertLess(response(200) - reference, -4)
        self.assertGreater(response(2000) - reference, 2)
        self.assertGreater(response(3000) - reference, 4)
        self.assertGreater(response(3400) - reference, 4)
        self.assertLess(abs(response(500) - reference), 3)

    def test_level_is_consistent_across_input_levels(self):
        levels = [rms_db(shaped(tone(997, amplitude, 3))[16000:]) for amplitude in (.04, .1, .3)]
        # A 17.5 dB input range lands within 6 dB at the phone.
        self.assertLess(max(levels) - min(levels), 6)
        self.assertGreater(min(levels), -24)

    def test_silence_stays_silent_and_peaks_are_bounded(self):
        voice = PhoneVoice()
        loud = tone(997, .9, 1)
        out = np.concatenate([voice.process(loud[i:i + 160]) for i in range(0, len(loud), 160)])
        self.assertLessEqual(float(np.max(abs(out))), .950001)
        for _ in range(5):
            voice.process(np.zeros(160))  # Filter ring-out after the tone.
        for _ in range(50):
            self.assertEqual(encode(voice.process(np.zeros(160))).hex(), 'ff' * 160)

    def test_step_aligned_chunking_matches_unbroken_processing(self):
        source = np.random.default_rng(5).normal(0, .1, 8000).astype('float32')
        expected = PhoneVoice().process(source)
        voice = PhoneVoice()
        result, cursor = [], 0
        for size in [40, 160, 80, 1200, 160, 6360]:
            result.append(voice.process(source[cursor:cursor + size]))
            cursor += size
        np.testing.assert_allclose(np.concatenate(result), expected, atol=1e-6)

    def test_steady_tone_has_no_gain_steps_at_packet_boundaries(self):
        out = shaped(tone(997, .1, 2))[4000:]
        reference = .1 * 10 ** (15 / 20) * 2 * np.pi * 997 / 8000
        self.assertLess(float(np.max(abs(np.diff(out)))), reference)

    def test_phone_to_dot_level_is_unchanged(self):
        audio = audio_without_devices()
        reference = PhoneResampler(8000, 48000)
        payload = encode(.08 * np.sin(2 * np.pi * 733 * np.arange(160) / 8000))
        expected = reference.resample_chunk(decode(payload))
        audio.receive(payload)
        np.testing.assert_array_equal(audio.playback.data, expected)


class ContinuityTests(unittest.TestCase):
    def test_continuous_input_has_no_playback_holes_after_startup(self):
        audio = audio_without_devices()
        missing = 0
        for i in range(250):
            audio.receive(encode(np.full(160, .1, dtype='float32')))
            out = audio.playback.take(BLOCK)
            if i >= 10:
                missing += int(np.count_nonzero(abs(out) < .001))
        self.assertEqual(missing, 0, f'{missing/48:.2f} ms of artificial silence')

    def test_short_capture_backlog_preserves_all_speech(self):
        audio = audio_without_devices()
        # A 340 ms backlog must trim only excess over the bound, not all but 20 ms.
        speech = tone(997, .1, .24, 48000)
        for block in np.split(speech, 12):
            audio.capture.append(block)
        audio.encoded.extend(encode(tone(997, .1, .1)))
        packets = [audio.next_packet() for _ in range(12)]
        audible = sum(np.count_nonzero(abs(decode(p)) > .01) for p in packets)
        self.assertGreaterEqual(audible, 1600, 'backlog recovery erased most queued speech')

    def test_jitter_buffer_preserves_samples_during_40ms_arrival_delay(self):
        audio = audio_without_devices()
        payload = encode(np.full(160, .1, dtype='float32'))
        arrival = [max(i + (2 if i % 50 in (20, 21) else 0), 0) for i in range(250)]
        arrival = np.maximum.accumulate(arrival)
        cursor = 0
        output = []
        for i in range(250):
            while cursor < len(arrival) and arrival[cursor] <= i:
                audio.receive(payload)
                cursor += 1
            output.append(audio.playback.take(BLOCK))
        self.assertEqual(audio.playback.underrun_samples, 0)
        self.assertEqual(audio.playback.dropped, 0)
        self.assertTrue(np.all(abs(np.concatenate(output)[4800:]) > .01))

    def test_long_input_stall_rebuffers_instead_of_discarding_partial_word(self):
        audio = audio_without_devices()
        for _ in range(3):
            audio.receive(encode(np.full(160, .1, dtype='float32')))
        for _ in range(8):
            audio.playback.take(BLOCK)
        self.assertEqual(audio.playback.rebuffers, 1)
        self.assertGreater(audio.playback.underrun_samples, 0)
        self.assertEqual(audio.playback.dropped, 0)
        for _ in range(3):
            audio.receive(encode(np.full(160, .1, dtype='float32')))
        self.assertTrue(np.all(abs(audio.playback.take(BLOCK)) > .01))

    def test_network_burst_keeps_speech_and_drains_delay_in_pauses(self):
        audio = audio_without_devices()
        speech = encode(np.full(160, .1, dtype='float32'))
        pause = encode(np.zeros(160, dtype='float32'))
        for _ in range(3):
            audio.receive(speech)
        audio.playback.take(BLOCK)
        # A 500 ms stall delivers 25 speech packets at once; a pause follows.
        for _ in range(25):
            audio.receive(speech)
        played = []
        for _ in range(80):
            audio.receive(pause)
            played.append(audio.playback.take(BLOCK))
        self.assertEqual(audio.playback.dropped, 0)
        self.assertGreaterEqual(sum(np.count_nonzero(abs(x) > .05) for x in played), 27 * BLOCK - 600)
        self.assertGreater(audio.playback.drained, 0)
        self.assertLessEqual(len(audio.playback.data), audio.playback.drain_above + 2 * BLOCK)

    def test_begin_call_excludes_ring_time_drops_and_filter_residue(self):
        audio = audio_without_devices()
        audio.capture_drops = 400
        audio.capture.append(np.ones(BLOCK, dtype='float32'))
        audio.next_packet()
        audio.receive(encode(np.ones(160, dtype='float32')))
        audio.reset_for_call()
        audio.capture.append(np.zeros(BLOCK, dtype='float32'))
        self.assertEqual(audio.next_packet(), b'\xff' * 160)
        self.assertEqual(audio.metrics()['pre_call_capture_drops'], 400)
        self.assertEqual(audio.metrics()['capture_drops'], 0)


class ResamplingTests(unittest.TestCase):
    def test_arbitrary_chunk_boundaries_match_unbroken_filter(self):
        source = np.random.default_rng(41).normal(0, .1, 5003).astype('float32')
        for rates in [(48000, 8000), (8000, 48000)]:
            expected = PhoneResampler(*rates).resample_chunk(source)
            stream = PhoneResampler(*rates)
            result = []
            cursor = 0
            for size in [1, 3, 17, 960, 2, 7, 160, 3853]:
                result.append(stream.resample_chunk(source[cursor:cursor + size]))
                cursor += size
            np.testing.assert_allclose(np.concatenate(result), expected, atol=1e-7)

    def test_fixed_packet_sizes_and_filter_delay_both_directions(self):
        for rates, size, expected in [((48000, 8000), 960, 160), ((8000, 48000), 160, 960)]:
            stream = PhoneResampler(*rates)
            impulse = np.zeros(size, dtype='float32')
            impulse[0] = 1
            out = stream.resample_chunk(impulse)
            self.assertEqual(len(out), expected)
            self.assertEqual(int(np.argmax(abs(out))), int(rates[1] * .005))
            for _ in range(20):
                self.assertEqual(len(stream.resample_chunk(np.zeros(size, dtype='float32'))), expected)

    def test_speech_band_flat_and_alias_images_suppressed(self):
        taps = PhoneResampler(48000, 8000).taps
        bins = np.arange(len(taps))
        def gain(f):
            return 20 * np.log10(abs(np.sum(taps * np.exp(-2j*np.pi*f*bins/48000))))
        self.assertLess(max(abs(gain(f)) for f in range(300, 3401, 100)), .1)
        self.assertLess(max(gain(f) for f in range(4100, 24001, 100)), -70)


class SenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_80ms_sender_stall_does_not_clear_speech(self):
        audio = audio_without_devices()
        audio.next_packet = lambda: encode(np.full(160, .1, dtype='float32'))
        discarded = []
        audio.discard_reply = lambda: discarded.append(True)
        state = Session('https://test.invalid', 'ACtest', 'token', audio)
        state.stream_sid = 'MZtest'
        clock = [1.0]
        messages = []

        async def send_json(message):
            messages.append(message)
            if len(messages) == 1:
                clock[0] += .08
            if len(messages) >= 12:
                state.closed = True

        async def sleep(delay):
            clock[0] += delay

        state.ws = SimpleNamespace(closed=False, send_json=send_json)
        with patch('bridge.time', SimpleNamespace(monotonic=lambda: clock[0])), \
                patch('bridge.asyncio.sleep', sleep):
            await state.send_audio()
        self.assertFalse(discarded, 'scheduler stall discarded locally queued speech')
        self.assertFalse(any(m['event'] == 'clear' for m in messages),
                         'scheduler stall erased Twilio playback')

    async def test_phone_noise_does_not_mute_or_clear_dot(self):
        audio = audio_without_devices()
        audio.start = lambda: None
        audio.next_packet = lambda: encode(np.full(160, .1, dtype='float32'))
        state = Session('https://test.invalid', 'ACtest', 'token', audio)
        state.call_sid = 'CAtest'
        state.call_ready.set()
        state.valid_signature = lambda *args: True
        noise = base64.b64encode(encode(np.full(160, .05, dtype='float32'))).decode()
        incoming = [{'event': 'start', 'start': {
            'callSid': 'CAtest', 'accountSid': 'ACtest', 'streamSid': 'MZtest',
            'mediaFormat': {'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1}}}]
        incoming += [{'event': 'media', 'streamSid': 'MZtest',
                      'media': {'track': 'inbound', 'payload': noise}} for _ in range(25)]
        incoming += [{'event': 'stop'}]
        outgoing = []

        async def receive():
            return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(incoming.pop(0)))
        async def send_json(message): outgoing.append(message)
        async def prepare(request): pass
        async def close(): pass
        ws = SimpleNamespace(prepare=prepare, receive=receive, send_json=send_json,
                             close=close, closed=False)
        with patch('bridge.web.WebSocketResponse', return_value=ws):
            await state.media(None)
        self.assertFalse(any(m['event'] == 'clear' for m in outgoing),
                         'phone noise incorrectly erased Dot speech')
        self.assertLessEqual(getattr(state, 'duck_until', 0), 0,
                             'phone noise incorrectly muted Dot speech')


if __name__ == '__main__':
    unittest.main()
