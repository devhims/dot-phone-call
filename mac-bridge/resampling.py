"""Fixed-rate, fixed-delay conversion for the 48 kHz / 8 kHz phone paths.

A 481-tap windowed-sinc FIR preserves the speech band through 3.4 kHz and
suppresses frequencies above 4 kHz. Each direction has a 5 ms filter delay.
Unlike FFT-based streaming resamplers, output does not arrive in large bursts.
Only the two integer-ratio conversions used by AudioBridge are supported.
"""
import numpy as np


class PhoneResampler:
    delay_ms = 5
    name = 'fir481-kaiser8.6-3700hz'

    def __init__(self, in_rate, out_rate):
        if (in_rate, out_rate) not in {(48000, 8000), (8000, 48000)}:
            raise ValueError('PhoneResampler only supports 48 kHz <-> 8 kHz')
        self.upsample = out_rate > in_rate
        position = np.arange(481) - 240
        taps = (2 * 3700 / 48000 * np.sinc(2 * 3700 / 48000 * position)
                * np.kaiser(481, 8.6))
        taps /= taps.sum()
        self.taps = (taps * (6 if self.upsample else 1)).astype('float32')
        self.clear()

    def clear(self):
        self.history = np.zeros(480, dtype='float32')
        self.phase = 0

    def resample_chunk(self, samples):
        samples = np.asarray(samples, dtype='float32')
        if samples.ndim != 1:
            raise ValueError('Expected mono samples')
        if not len(samples):
            return np.empty(0, dtype='float32')
        if self.upsample:
            expanded = np.zeros(len(samples) * 6, dtype='float32')
            expanded[::6] = samples
            samples = expanded
        joined = np.concatenate((self.history, samples))
        filtered = np.convolve(joined, self.taps, mode='valid')
        self.history = joined[-480:].copy()
        if self.upsample:
            return filtered
        output = filtered[self.phase::6].copy()
        self.phase = (self.phase - len(samples)) % 6
        return output
