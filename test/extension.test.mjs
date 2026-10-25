import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../chrome-extension/content.js', import.meta.url), 'utf8');

// Minimal DOM fixture for the observed call controls. Run the real content
// script, including its selector and message handler, rather than a copy.
function inspect(timerTag, text, role = null, hidden = false) {
  let listener;
  const element = (tag, label, value, elementRole, invisible = false) => ({
    tagName: tag.toUpperCase(), textContent: value,
    getAttribute: name => ({'aria-label': label, role: elementRole}[name] ?? null),
    getClientRects: () => invisible ? [] : [{}],
  });
  const elements = [element('button', 'Hang up', ''),
    element(timerTag, 'Move call controls', text, role, hidden)];
  const document = {
    querySelectorAll(selector) {
      return elements.filter(el => selector.split(',').some(part => {
        const tag = part.trim().match(/^[a-z]+/)?.[0];
        if (tag && el.tagName !== tag.toUpperCase()) return false;
        return [...part.matchAll(/\[([\w-]+)(?:=["']([^"']+)["'])?\]/g)]
          .every(([,name,value]) => value === undefined
            ? el.getAttribute(name) !== null : el.getAttribute(name) === value);
      }));
    },
  };
  vm.runInNewContext(source, {document, location: {pathname:'/dots/home'},
    chrome: {runtime: {id:'test', onMessage: {addListener(fn) {listener = fn;}}}}});
  let result;
  listener({action:'dot_phone_v2_inspect'}, {id:'test'}, value => {result = value;});
  return result;
}

test('detects a connected timer in Dot’s observed div with a button role', () => {
  assert.equal(inspect('div', 'Chase00:24Drag to a corner', 'button').connected, true);
});
test('detects timer text directly adjoining the Dot name', () => {
  assert.equal(inspect('button', 'Chase00:24Drag to a corner').connected, true);
});
test('does not treat Calling or hidden timers as connected', () => {
  assert.equal(inspect('div', 'Chase Calling…', 'button').connected, false);
  assert.equal(inspect('div', 'Chase00:24', 'button', true).connected, false);
});
