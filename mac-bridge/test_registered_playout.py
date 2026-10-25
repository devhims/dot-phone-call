"""Phone-to-Dot playout jitter buffer. No devices, network, or phone calls."""
import unittest

import numpy as np

from registered_call import LocalAudioPipe
from registered_probe import CHUNK, RATE


def pipe():
    return LocalAudioPipe(extension_id='a' * 32)


def speech(level=.1):
    return np.full(CHUNK, level, dtype='<f4')


def play(audio, blocks=1):
    out = np.empty((CHUNK, 2), dtype='float32')
    played = []
    for _ in range(blocks):
        audio.fill_playout(out, CHUNK)
        played.append(out[:, 0].copy())
    return np.concatenate(played)


class PlayoutTests(unittest.TestCase):
    def test_prebuffers_three_chunks_before_playing(self):
        audio = pipe()
        for _ in range(2):
            audio.queue_audio(speech())
            self.assertFalse(np.any(play(audio)), 'played before the prebuffer filled')
        audio.queue_audio(speech())
        self.assertTrue(np.all(play(audio) > .05))
        self.assertEqual(audio.audio_metrics()['underrun_ms'], 0)

    def test_periodic_40ms_arrival_stalls_play_without_gaps(self):
        audio = pipe()
        # Main-thread or socket stalls delay two chunks every second, then
        # deliver them together. The previous playout spliced in 40 ms of silence.
        ticks = np.arange(250)
        arrivals = np.maximum.accumulate(ticks + np.where(np.isin(ticks % 50, [20, 21]), 2, 0))
        cursor, output = 0, []
        for tick in ticks:
            while cursor < 250 and arrivals[cursor] <= tick:
                audio.queue_audio(speech())
                cursor += 1
            output.append(play(audio))
        played = np.concatenate(output)
        started = int(np.argmax(played > .05))
        self.assertTrue(np.all(played[started:] > .05), 'silence spliced into steady speech')
        metrics = audio.audio_metrics()
        self.assertEqual((metrics['underrun_ms'], metrics['rebuffers'], metrics['dropped_ms']), (0, 0, 0))

    def test_long_stall_rebuffers_whole_chunks_instead_of_partial_splices(self):
        audio = pipe()
        for _ in range(3):
            audio.queue_audio(speech())
        play(audio, 6)
        metrics = audio.audio_metrics()
        self.assertEqual(metrics['rebuffers'], 1)
        self.assertGreater(metrics['underrun_ms'], 0)
        for _ in range(3):
            audio.queue_audio(speech())
        self.assertTrue(np.all(play(audio) > .05))

    def test_burst_keeps_speech_and_drains_delay_during_pauses(self):
        audio = pipe()
        for _ in range(3):
            audio.queue_audio(speech())
        play(audio)
        # A 300 ms scheduling stall delivers 15 chunks at once; a pause follows.
        for _ in range(15):
            audio.queue_audio(speech())
        output = []
        for _ in range(60):
            audio.queue_audio(np.zeros(CHUNK, dtype='<f4'))
            output.append(play(audio))
        heard = sum(int(np.count_nonzero(block > .05)) for block in output)
        self.assertEqual(heard, 17 * CHUNK, 'burst speech was lost')
        metrics = audio.audio_metrics()
        self.assertEqual(metrics['dropped_ms'], 0)
        self.assertGreater(metrics['drained_ms'], 0)
        self.assertLessEqual(len(audio.playout.data), RATE // 10 + 2 * CHUNK)

    def test_hard_bound_limits_sustained_backlog(self):
        audio = pipe()
        for _ in range(40):
            audio.queue_audio(speech())
        self.assertLessEqual(len(audio.playout.data), RATE // 2)
        self.assertGreater(audio.audio_metrics()['dropped_ms'], 0)

    def test_metrics_count_received_and_played_frames(self):
        audio = pipe()
        for _ in range(4):
            audio.queue_audio(speech())
        play(audio, 2)
        metrics = audio.audio_metrics()
        self.assertEqual(metrics['received_frames'], 4 * CHUNK)
        self.assertEqual(metrics['played_frames'], 2 * CHUNK)
        self.assertEqual(set(metrics) >= {'dropped_ms', 'drained_ms', 'underrun_ms', 'rebuffers'}, True)


if __name__ == '__main__':
    unittest.main()
