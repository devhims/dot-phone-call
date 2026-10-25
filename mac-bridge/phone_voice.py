"""Telephone voice conditioning for Dot-to-phone audio at 8 kHz.

Speech synthesizers put most of their energy below 300 Hz. Phone earpieces and
carrier codecs discard that band, so a flat signal spends its headroom on bass
the listener never hears while consonants (2-3.4 kHz) stay quiet. This stage:

1. high-passes below the telephone band, freeing headroom,
2. lifts the 2-3.4 kHz consonant band for clarity,
3. levels speech toward a consistent telephone loudness, and
4. soft-limits peaks.

It is causal and sample-continuous across packets and adds no buffering.
"""
import math

import numpy as np

RATE = 8000
HIGHPASS_HZ = 260
PRESENCE_HZ, PRESENCE_DB = 2000, 5.0  # High shelf: full lift from ~3 kHz.
# Target for the leveler's fast envelope. Measured on speech, this places the
# active speech level near -15 dBFS (about -12 dBm0), a firm telephone level.
TARGET_DBFS = -12.0
MAX_GAIN_DB, MIN_GAIN_DB = 15.0, -10.0
# Typical Dot envelope, so the first words of a call start near final gain.
INITIAL_LEVEL_DBFS = -20.0
GATE_DBFS = -50.0  # Never raise gain on silence or noise below this level.
STEP = 40  # 5 ms leveler resolution.
ATTACK_MS, RELEASE_MS = 10.0, 400.0
KNEE, CEILING = .75, .95


def _highpass(frequency, q):
    w = 2 * math.pi * frequency / RATE
    alpha = math.sin(w) / (2 * q)
    cos = math.cos(w)
    b = [(1 + cos) / 2, -(1 + cos), (1 + cos) / 2]
    a = [1 + alpha, -2 * cos, 1 - alpha]
    return [x / a[0] for x in b], [x / a[0] for x in a[1:]]


def _high_shelf(frequency, gain_db):
    # RBJ cookbook shelf with slope 1.
    w = 2 * math.pi * frequency / RATE
    amplitude = 10 ** (gain_db / 40)
    cos = math.cos(w)
    root = 2 * math.sqrt(amplitude) * math.sin(w) / 2 * math.sqrt(2)
    b = [amplitude * ((amplitude + 1) + (amplitude - 1) * cos + root),
         -2 * amplitude * ((amplitude - 1) + (amplitude + 1) * cos),
         amplitude * ((amplitude + 1) + (amplitude - 1) * cos - root)]
    a = [(amplitude + 1) - (amplitude - 1) * cos + root,
         2 * ((amplitude - 1) - (amplitude + 1) * cos),
         (amplitude + 1) - (amplitude - 1) * cos - root]
    return [x / a[0] for x in b], [x / a[0] for x in a[1:]]


def soft_limit(samples):
    """Linear below the knee, smoothly approaching the ceiling; stateless."""
    magnitude = np.abs(samples)
    limited = KNEE + (CEILING - KNEE) * np.tanh(
        np.maximum(magnitude - KNEE, 0) / (CEILING - KNEE))
    return np.sign(samples) * np.where(magnitude <= KNEE, magnitude, limited)


class PhoneVoice:
    name = f'hp{HIGHPASS_HZ}-presence{PRESENCE_DB:g}db-level{TARGET_DBFS:g}'

    def __init__(self):
        self.sections = [_highpass(HIGHPASS_HZ, .7071),
                         _high_shelf(PRESENCE_HZ, PRESENCE_DB)]
        self.attack = 1 - math.exp(-STEP / RATE * 1000 / ATTACK_MS)
        self.release = 1 - math.exp(-STEP / RATE * 1000 / RELEASE_MS)
        self.clear()
        self.reset_metrics()

    def clear(self):
        self.state = [[0.0, 0.0] for _ in self.sections]
        self.level = INITIAL_LEVEL_DBFS
        self.gain_db = TARGET_DBFS - INITIAL_LEVEL_DBFS

    def reset_metrics(self):
        self.input_energy = self.output_energy = 0.0
        self.active_samples = self.limited_samples = self.total_samples = 0
        self.gain_sum = 0.0
        self.gain_steps = 0

    def _equalize(self, samples):
        out = samples.tolist()
        for ((b0, b1, b2), (a1, a2)), state in zip(self.sections, self.state):
            z1, z2 = state
            for i, x in enumerate(out):
                # Transposed direct form II.
                y = b0 * x + z1
                z1 = b1 * x - a1 * y + z2
                z2 = b2 * x - a2 * y
                out[i] = y
            state[0], state[1] = z1, z2
        return np.asarray(out)

    def process(self, samples):
        samples = np.asarray(samples, dtype='float64')
        if not len(samples):
            return np.empty(0, dtype='float32')
        shaped = self._equalize(samples)
        gains = np.empty(len(shaped))
        start = self.gain_db
        # Gain updates every 5 ms of input; ramps keep each step click-free.
        cursor = 0
        while cursor < len(shaped):
            end = min(cursor + STEP, len(shaped))
            block = shaped[cursor:end]
            power = float(np.mean(block * block))
            level = 10 * math.log10(power + 1e-20)
            if level > GATE_DBFS:
                coefficient = self.attack if level > self.level else self.release
                self.level += (level - self.level) * coefficient
                self.input_energy += float(np.sum(samples[cursor:end] ** 2))
                self.active_samples += end - cursor
                active = True
            else:
                active = False
            target = min(MAX_GAIN_DB, max(MIN_GAIN_DB, TARGET_DBFS - self.level))
            gains[cursor:end] = np.linspace(start, target, end - cursor + 1)[1:]
            start = self.gain_db = target
            self.gain_sum += target
            self.gain_steps += 1
            if active:
                self.output_energy += float(np.sum((block * 10 ** (target / 20)) ** 2))
            cursor = end
        out = soft_limit(shaped * 10 ** (gains / 20))
        self.limited_samples += int(np.count_nonzero(np.abs(out) > KNEE))
        self.total_samples += len(out)
        return out.astype('float32')

    def metrics(self):
        def dbfs(energy):
            if not self.active_samples:
                return None
            return round(10 * math.log10(energy / self.active_samples + 1e-20), 1)
        return dict(voice_shaping=self.name,
                    dot_active_level_dbfs=dbfs(self.input_energy),
                    phone_active_level_dbfs=dbfs(self.output_energy),
                    voice_mean_gain_db=round(self.gain_sum / self.gain_steps, 1) if self.gain_steps else 0,
                    voice_limited_pct=round(100 * self.limited_samples / self.total_samples, 2)
                    if self.total_samples else 0)
