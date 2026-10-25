> Historical source-project notes. These describe earlier tests and transports, not an activated installation of this project. Use the root README for current status.

# Audio quality verification, 24 October 2026

The bridge now preserves short audio backlogs, leaves interruption detection to
Dot, and uses a fixed-delay resampler. No phone was dialed for this verification.
The measurements below establish improvements inside the bridge. They are not
a perceptual quality score or a measurement of Twilio/carrier performance.

## Changes

- A delayed sender no longer clears Twilio's playback buffer or discards its
  local reply. Outbound buffering is capped at 240 ms; longer backlogs trim only
  excess complete packets. A blocked WebSocket send times out after two seconds.
- The bridge no longer mutes or clears Dot based on incoming volume. Dot's voice
  interface receives the caller audio and owns interruption/turn detection.
  This prevents noise and speakerphone echo from independently erasing replies.
- Incoming audio waits for three 20 ms packets before playback. At steady cadence
  this adds 40 ms of initial waiting. Short arrival delays are absorbed. Longer
  shortages trigger refill, keeping partial packets intact.
- Both converters use a 481-tap Kaiser-windowed sinc FIR with a 3.7 kHz cutoff.
  It runs at the fixed 48 kHz/8 kHz ratio, preserves the speech band through
  3.4 kHz, and has a 5 ms group delay in each direction. No new dependency is
  needed. The filter state persists across packets and resets at call start.
- Playback marks are sent every 500 ms. Their acknowledgments measure network
  round trip plus playback delay, not pure queue depth. Pending marks expire
  after five seconds. Metrics separate pre-call capture drops from live drops
  and report underflows, discarded samples, sender stalls, and mark timing.

## Measured before and after

The baseline was copied from the working files immediately before these changes,
including the previously implemented faster startup. The deterministic fixtures
use no devices or network. All four initial regression tests failed before the
fix and passed after it.

| Fixture | Before | After |
| --- | ---: | ---: |
| 80 ms sender stall | Discarded local reply and sent `clear` | Neither discard nor `clear` |
| 500 ms of steady incoming noise | False interruption and muting | Audio forwarded to Dot without muting |
| Steady incoming audio, silence inserted after 200 ms warmup over a 5 s run | 18.333 ms | 0 ms |
| Same run with periodic 40 ms arrival delays | 47.167 ms | 0 ms |
| Audible content retained from a 340 ms backlog | 20 ms | 235 ms |
| Converter gain at a 3.4 kHz test tone, before mu-law encoding | -7.891 dB | Approximately 0 dB |

The backlog fixture contains 100 ms already encoded plus 240 ms newly captured.
The new cap retains 240 ms of samples, including the filter's initial 5 ms
transient. Thus 235 ms meets the fixture's audible threshold. It still drops
audio beyond the configured bound instead of accumulating unbounded delay.

## Resampling comparison

Each converter received 200 consecutive 20 ms blocks. SoXR delay below is its
maximum reported pending-output delay in this fixture; the FIR value is its
fixed group delay. These exclude device, packetization, jitter-buffer, and
network delays. A FIR output block is available for every input block.

| Converter | 48 kHz to 8 kHz delay | 8 kHz to 48 kHz delay | Gain at 3.4 kHz |
| --- | ---: | ---: | ---: |
| SoXR LQ, previous | 40.0 ms | 57.3 ms | -7.891 dB |
| SoXR MQ | 75.0 ms | 92.0 ms | Approximately 0 dB |
| SoXR HQ | 157.3 ms | 175.0 ms | -0.001 dB |
| SoXR VHQ | 153.3 ms | 170.0 ms | -0.001 dB |
| Fixed FIR, selected | 5.0 ms | 5.0 ms | Approximately 0 dB |

The selected filter suppresses a 4.2 kHz input tone by about 92 dB, less than
SoXR's rejection but sufficient for this speech path. Automated checks require
less than 0.1 dB ripple from 300 to 3400 Hz and more than 70 dB rejection across
the tested stopband. Processing took about 0.06 ms per 20 ms block at the 95th
percentile on this Mac. These are signal/CPU measurements, not perceived-quality
percentages. The filter intentionally rolls off above the phone speech band;
it does not restore wideband audio lost to the 8 kHz transport.

## Verification and artifacts

- 70 Python tests passed, including signed local WebSocket tests, mark handling,
  sender stalls, false interruptions, jitter, long-stall refill, filter response,
  arbitrary chunk boundaries, greeting preservation, cancellation, and cleanup.
- The installed native helper passed both physical BlackHole loopbacks in
  3.88 seconds. RMS was 0.0556 toward the phone side and 0.0260 toward Dot.
  It reported `dialed: false`, `cleanup_ok: true`, and `audio_restored: true`,
  and its process exited with status zero.
- A 6.618-second synthetic phrase generated locally with macOS speech synthesis
  was passed through the old and new bridge in both directions. This is a test
  fixture, not a recording of Dot or a real conversation. No loudness
  normalization was applied separately to the outputs.
- Local artifacts: `.runtime/audio-quality/measurements.json`,
  `python-tests.log`, `source.wav`, `before-dot-to-phone.wav`,
  `after-dot-to-phone.wav`, `before-phone-to-dot.wav`, and `after-phone-to-dot.wav`.
- Reproduce the offline comparison with `scripts/benchmark-audio-quality.py`;
  its help text describes the baseline and speech inputs. Baseline source copies
  are retained in `.runtime/audio-quality/before/` for this comparison only.

The installed app loads these Python sources at helper startup, so the next call
uses the fixes without an extension reload or cloud deployment. Live Dot
interruption behavior and subjective quality through the carrier remain
unverified by these tests. Playback already buffered remotely may still be
heard briefly after Dot stops speaking. Twilio Media Streams remains mono
8 kHz mu-law.

References: [Twilio media playback and marks](https://www.twilio.com/docs/voice/media-streams/websocket-messages),
[Twilio audio conversion guidance](https://help.twilio.com/articles/223180588-Best-Practices-for-Audio-Recordings),
[SoXR streaming delay](https://python-soxr.readthedocs.io/en/latest/soxr.html#soxr.ResampleStream.delay).

## Phone voice volume (2026-10-24)

Dot-to-phone audio now receives +4 dB gain after downsampling and before mu-law
encoding. A smooth limiter starts above 0.75 amplitude and approaches a 0.95
ceiling to protect loud peaks. It adds no buffering and preserves silence.
Phone-to-Dot audio and connection tones are unchanged. This increases level,
not telephone bandwidth, and applies when the next helper starts.

All 96 Python tests passed, including measured encoded gain within 0.1 dB,
unchanged timing and incoming audio, bounded peaks, and chunk continuity.
The installed helper's no-phone BlackHole test measured outgoing RMS 0.0885
(previous test 0.0558, approximately +4.0 dB) and incoming RMS 0.0257.
Cleanup and audio restoration passed; no phone was dialed. Perceived loudness
through the user's handset still needs a normal call to assess.

## Telephone voice shaping and burst-safe caller audio (2026-10-24)

The +4 dB boost above did not fix muffled, quiet Dot speech on the handset.
Speech synthesis puts most of its energy below 300 Hz: in the speech fixture,
69% of the energy was below 300 Hz and the 2-3.4 kHz consonant band sat 20 dB
below the total. Phone earpieces and carrier codecs discard the low band, so
the flat signal spent its headroom on bass the listener never hears. Real calls
also showed Dot peaking at only 0.36-0.51 before the boost.

`phone_voice.py` replaces the fixed boost on the Dot-to-phone path, at 8 kHz:

- 260 Hz second-order high-pass, then a +5 dB high shelf at 2 kHz (+4.8 dB at
  3 kHz, +5 dB at 3.4 kHz).
- A leveler with a 5 ms envelope (10 ms attack, 400 ms release) toward a
  -12 dBFS envelope target, gain limited to -10..+15 dB. It never raises gain
  on audio below -50 dBFS, and it keeps the existing soft limiter.
- Causal IIR filters; no buffering. About 0.1 ms CPU per 20 ms packet.

Offline, at Dot's measured level (fixture attenuated 7 dB, mu-law encoded):

| Measurement | Before | After |
| --- | ---: | ---: |
| Active speech level | -18.0 dBFS | -14.7 dBFS |
| Active level within 300-3400 Hz | -23.0 dBFS | -16.6 dBFS |
| 2-3.4 kHz band relative to total | -20.2 dB | -11.8 dB |
| Samples above limiter knee | 0% | 0.1% |

The 300-3400 Hz level approximates what the handset reproduces. Inputs 7 dB
quieter gained 10.7 dB in that band. Listen with
`.runtime/audio-quality/voice-shaping-before.wav` and `voice-shaping-after.wav`.

The pre-call tone check now accepts Dot-to-phone RMS from 0.02 to 0.45. The
installed helper's no-phone route test measured 0.219 (previously 0.0885) and
phone-to-Dot 0.0259; nothing was dialed and cleanup passed.

Caller audio: the last two inbound calls dropped 4.98 s and 6.32 s of
phone-to-Dot audio. Network stalls delivered bursts that overflowed the 250 ms
queue, so the oldest audio, often speech, was discarded. The queue now
holds up to one second and drains back to 200 ms only by removing quiet 10 ms
chunks (below about -40 dBFS). In a simulated bursty run (stalls of 100-580 ms),
audible caller speech lost fell from 3.18 s to 0.01 s. Mean queue depth went
from 214 to 259 ms. `playback_drained_ms` reports the silence removed.

New call metrics: `dot_active_level_dbfs`, `phone_active_level_dbfs`,
`voice_mean_gain_db`, `voice_limited_pct`, and `playback_drained_ms`.

Not addressed in code: these calls cross an international route (US Twilio
number to an Indian mobile), where carriers may transcode to lower-bitrate
codecs. Voice Insights is not enabled on this account (Summary returns 404),
so the carrier leg's codec and loss are unmeasured. Twilio Media Streams
remains 8 kHz mu-law. Perceived quality needs a normal call to judge.
