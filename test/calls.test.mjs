import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';

const source = readFileSync(new URL('../chrome-extension/calls.js', import.meta.url), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const JOB = {id: randomUUID(), duration_seconds: 60};
const ROUTED = {device: 'BlackHole 2ch', routed_elements: 2, routed_contexts: 1, seen_elements: 2, seen_contexts: 1, errors: []};

// Backend and Dot tab are simulated; no network, browser, or phone is used.
async function fixture({config = {callingEnabled: false}, stored = {}, backend = {}, commands = {}} = {}) {
  const values = {...stored}, requests = [], browser = [], noop = () => {};
  let clock = 1_000_000;
  const chrome = {
    storage: {local: {
      get: async keys => Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map(k => [k, values[k]])),
      set: async v => Object.assign(values, v), remove: async k => {delete values[k];},
    }},
    runtime: {onStartup: {addListener: noop}, sendMessage: async message => backend.offscreen?.(message), openOptionsPage: noop},
    action: {onClicked: {addListener: noop}}, alarms: {create: noop, onAlarm: {addListener: noop}},
  };
  const reply = (action, body) => {
    const handler = backend[action];
    return typeof handler === 'function' ? handler(body) : handler ?? {};
  };
  const context = vm.createContext({
    chrome, crypto: {randomUUID: () => 'extension-instance-0001'}, DOT_PHONE_CONFIG: {origin: 'http://127.0.0.1:8894', token: 't', ...config},
    AbortSignal, JSON, Promise, Error, Array, Object,
    Date: {now: () => clock},
    setTimeout: (fn, ms = 0) => {clock += ms; setImmediate(fn);}, setInterval: noop,
    fetch: async (url, options) => {
      const action = url.split('/').at(-1), body = JSON.parse(options.body);
      requests.push([action, body]);
      return {ok: true, json: async () => reply(action, body)};
    },
    command: async message => {browser.push(message.action); const handler = commands[message.action]; return typeof handler === 'function' ? handler(message) : handler;},
    endOwned: async jobId => {browser.push('end'); return commands.end ?? {ended: true};},
    owned: async () => null, refreshDotTabs: async () => {}, bounded: promise => promise,
    browserErrorCode: error => /^[A-Z_]{1,60}$/.test(error?.message || '') ? error.message : 'BROWSER_COMMAND_FAILED',
    ensureHiddenBridge: async () => backend.hidden ?? {registered: true, microphone_ready: true},
    hiddenBridgeStatus: async () => ({}),
    keepDotTabReady: async () => {browser.push('keep'); return backend.keep ?? null;},
  });
  vm.runInContext(source, context);
  for (let i = 0; i < 5; i++) await flush();
  const updates = () => requests.filter(([a]) => a === 'update').map(([, b]) => b);
  return {values, requests, browser, updates, run: expression => vm.runInContext(expression, context), context};
}

const callingBackend = (states, extra = {}) => {
  let n = 0;
  return {'registered-prepare': {ready: true}, pulse: {}, 'registered-finish': {ended: true}, update: {accepted: true},
    'registered-state': () => states[Math.min(n++, states.length - 1)], ...extra};
};
const run = async (f, job = JOB) => {f.context.job = job; await f.run('runCall(job)');};

test('a disabled build never polls the backend or touches Dot', async () => {
  const f = await fixture();
  assert.deepEqual(f.requests, []);
  assert.deepEqual(f.browser, []);
});

test('a call prepares Dot, starts it after the answer, and finishes in order', async () => {
  const f = await fixture({backend: callingBackend([{connected: false}, {connected: true}]), commands: {
    prepare: {ready: true, tab_id: 7},
    start: {connected: true, microphone_verified: true, output: ROUTED, output_verified: true},
    inspect_owned: {active: false, microphone_verified: true, output: ROUTED, output_verified: true}}});
  await run(f);
  assert.deepEqual(f.browser, ['prepare', 'start', 'inspect_owned', 'end']);
  const actions = f.requests.map(([a]) => a);
  assert.ok(actions.indexOf('registered-prepare') < actions.indexOf('registered-finish'));
  assert.ok(actions.lastIndexOf('registered-finish') < actions.lastIndexOf('update'));
  assert.deepEqual(f.updates().map(u => u.status), ['connected', 'completed']);
  assert.equal(f.updates().at(-1).diagnostics.dot_output.routed_elements, 2);
  assert.equal(f.values.activeCall, undefined);
  assert.equal(f.values.lastCall.code, null);
  assert.ok(f.requests.every(([, body]) => body.instance === 'extension-instance-0001'));
});

test('a Dot player that refuses BlackHole mid-call fails the call and hangs up Dot', async () => {
  const refused = {...ROUTED, errors: ['elements:NotAllowedError']};
  const f = await fixture({backend: callingBackend([{connected: true}]), commands: {
    prepare: {ready: true, tab_id: 7}, start: {connected: true, microphone_verified: true, output: ROUTED},
    inspect_owned: {active: true, microphone_verified: true, output: refused}}});
  await run(f);
  assert.equal(f.browser.at(-1), 'end');
  assert.deepEqual(f.updates().at(-1), {call_id: JOB.id, status: 'failed', code: 'DOT_OUTPUT_NOT_ROUTED',
    diagnostics: {dot_output: refused}, instance: 'extension-instance-0001'});
});

test('the output-routing gate failing at connect never reports connected', async () => {
  const f = await fixture({backend: callingBackend([{connected: true}]), commands: {
    prepare: {ready: true, tab_id: 7}, start: {error: 'DOT_OUTPUT_NOT_ROUTED'}}});
  await run(f);
  assert.deepEqual(f.updates().map(u => [u.status, u.code]), [['failed', 'DOT_OUTPUT_NOT_ROUTED']]);
});

test('a cancellation delivered by pulse ends the call before Dot starts', async () => {
  const f = await fixture({backend: callingBackend([{connected: false}], {pulse: {cancel_call_id: JOB.id}}),
    commands: {prepare: {ready: true, tab_id: 7}}});
  await run(f);
  assert.ok(!f.browser.includes('start'));
  assert.deepEqual(f.updates().map(u => u.status), ['completed']);
});

test('an unanswered call times out after 85 seconds and cleans up', async () => {
  const f = await fixture({backend: callingBackend([{connected: false}]), commands: {prepare: {ready: true, tab_id: 7}}});
  await run(f);
  assert.deepEqual(f.updates().map(u => [u.status, u.code]), [['failed', 'REGISTERED_CALL_TIMEOUT']]);
  assert.equal(f.browser.at(-1), 'end');
});

test('a failed cleanup keeps the call record so a restart retries it', async () => {
  const f = await fixture({backend: callingBackend([{ended: true}], {'registered-finish': {ended: false}}),
    commands: {prepare: {ready: true, tab_id: 7}}});
  await run(f);
  assert.equal(f.updates().at(-1).code, 'REGISTERED_HANGUP_UNCONFIRMED');
  assert.equal(f.values.activeCall.id, JOB.id);
});

test('a worker restart during a call finishes it exactly once', async () => {
  const f = await fixture({stored: {activeCall: {id: JOB.id}}, backend: callingBackend([{}])});
  assert.deepEqual(f.updates().map(u => [u.call_id, u.status, u.code]), [[JOB.id, 'failed', 'EXTENSION_RESTARTED']]);
  assert.ok(f.browser.includes('end'));
  assert.equal(f.values.activeCall, undefined);
});

test('a requested no-phone check runs in the hidden page and reports to the backend once', async () => {
  const id = randomUUID(), result = {verified: true, sink: 'blackhole-16ch', codec: 'audio/opus', peak_hz: 997, rms: .14, active_ms: 2500, phone_dialed: false};
  const f = await fixture({config: {callingEnabled: true, hiddenBridge: true}, backend: {
    'registered-session': {url: 'http://127.0.0.1:8894/bridge/#' + 'a'.repeat(64), audio_diagnostic_id: id},
    hidden: {registered: true, microphone_ready: true}, offscreen: message => message.action === 'audio-test' ? result : undefined,
    pulse: {}, 'audio-diagnostic-result': {recorded: true}}});
  const reported = f.requests.filter(([a]) => a === 'audio-diagnostic-result');
  assert.equal(reported.length, 1);
  assert.equal(reported[0][1].id, id);
  assert.equal(reported[0][1].result.peak_hz, 997);
  await f.run('pollCloud()');
  assert.equal(f.requests.filter(([a]) => a === 'audio-diagnostic-result').length, 1, 'same request ID runs once');
});

test('a failed Dot start sends its timeline to the call log', async () => {
  const f = await fixture({config: {callingEnabled: true}, backend: callingBackend([{connected: true}]), commands: {
    prepare: {ready: true, tab_id: 7, diagnostics: {tab_created: true, page_age_ms: 4100, prepare_ms: 900}},
    start: {error: 'DOT_VOICE_ENDED_BEFORE_CONNECT', diagnostics: {page_age_ms: 7200, active_after_ms: 1800, waited_ms: 14200}}}});
  await run(f);
  for (let i = 0; i < 10; i++) await flush();
  const logged = f.requests.filter(([a]) => a === 'log').flatMap(([, body]) => body.events);
  const named = name => logged.find(e => e.ev === name);
  assert.equal(named('dot_tab_prepared').tab_created, true);
  assert.equal(named('dot_tab_prepared').page_age_ms, 4100);
  assert.equal(named('call_failed').code, 'DOT_VOICE_ENDED_BEFORE_CONNECT');
  assert.equal(named('call_failed').page_age_ms, 7200);
  assert.equal(named('call_loop_ended').reason, 'failed');
  assert.equal(named('call_finished').code, 'DOT_VOICE_ENDED_BEFORE_CONNECT');
  assert.ok(logged.every(e => e.call === JOB.id || e.ev === 'extension_started'));
});

test('idle polls keep the Dot tab ready and log when a tab is opened', async () => {
  const f = await fixture({config: {callingEnabled: true, hiddenBridge: true}, backend: {pulse: {}, keep: {action: 'opened'},
    'registered-session': {url: 'http://127.0.0.1:8894/bridge/#' + 'a'.repeat(64)}}});
  for (let i = 0; i < 10; i++) await flush();
  assert.ok(f.browser.includes('keep'));
  const logged = f.requests.filter(([a]) => a === 'log').flatMap(([, body]) => body.events);
  assert.equal(logged.find(e => e.ev === 'dot_tab_kept_ready')?.action, 'opened');
});
