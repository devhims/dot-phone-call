import {test} from 'node:test';
import assert from 'node:assert/strict';
import {assessProbe} from '../prototype/opus/verdict.js';

const fixture = () => ({phone_dialed: false,
  reference: {source: {997:-38,6000:-38}, local_opus: {997:-38,6000:-38.3}},
  caller_to_receiver: {997:-38,6000:-38.3}, receiver_to_caller: {733:-38,6500:-38.3},
  blackhole2_return: {997:-38,6000:-38.3}, blackhole16_return: {733:-41.8,6500:-42.1},
  caller_stats: ['inbound-rtp','outbound-rtp'].map(direction => ({direction,codec:'audio/opus',clockRate:48000,packets:250})),
  receiver_stats: ['inbound-rtp','outbound-rtp'].map(direction => ({direction,codec:'audio/opus',clockRate:48000,packets:250})),
});
test('wideband requires audible measured tones as well as Opus negotiation', () => {
  assert.equal(assessProbe(fixture()).verified, true);
  const report = fixture(); report.caller_to_receiver[6000] = -114;
  const result = assessProbe(report);
  assert.equal(result.checks.opus_negotiated, true);
  assert.equal(result.checks.two_way_transport, true);
  assert.equal(result.checks.cloud_wideband, false);
  assert.equal(result.verified, false);
});
test('a broken control or silent virtual route cannot pass', () => {
  for (const field of ['blackhole2_return', 'blackhole16_return']) {
    const report = fixture(); report[field] = {997:-200,6000:-200};
    assert.equal(assessProbe(report).verified, false);
  }
  const report = fixture(); report.reference.local_opus = {997:-200,6000:-200};
  assert.equal(assessProbe(report).verified, false);
});
test('partial stats, fallback codec and phone dialing cannot pass the no-phone proof', () => {
  for (const mutate of [r => r.caller_stats.pop(), r => r.receiver_stats[0].codec = 'audio/PCMU',
    r => r.phone_dialed = true, r => r.caller_stats[0].packets = 0]) {
    const report = fixture(); mutate(report); assert.equal(assessProbe(report).verified, false);
  }
});
test('high-frequency attenuation fails even above the audible floor', () => {
  const report = fixture(); report.caller_to_receiver[6000] = -60;
  assert.equal(assessProbe(report).verified, false);
});
