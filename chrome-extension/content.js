// Only visible call controls are inspected. No chat text, cookies or audio APIs.
(() => {
  const version = '0.2.1';
  const visible = element => !!element && element.getClientRects().length > 0;
  const control = label => [...document.querySelectorAll('button[aria-label], [role="button"][aria-label]')]
    .find(e => e.getAttribute('aria-label') === label && visible(e));
  function state() {
    if (!location.pathname.startsWith('/dots/')) return {ready:false, active:false, connected:false};
    const hangup = control('Hang up');
    const move = control('Move call controls');
    const hasDraft=[...document.querySelectorAll('[contenteditable="true"], textarea')]
      .some(element=>visible(element) && String(element.value ?? element.textContent ?? '').trim());
    // page_age_ms: a Dot page that has just loaded may show Start call before it can hold a voice session.
    return {version, ready:!!control('Start call'), active:!!hangup, has_draft:hasDraft, page_age_ms:globalThis.performance ? Math.round(performance.now()) : null,
      // Dot renders e.g. "Chase00:24Drag..." without text-node separators.
      connected:!!hangup && !!move && /(?:^|\D)\d{1,3}\s*:\s*[0-5]\d(?!\d)/.test(move.textContent || '')};
  }
  const listener = (message, sender, reply) => {
    if (sender.id !== chrome.runtime.id || !location.pathname.startsWith('/dots/')) return;
    if (message.action === 'dot_phone_v2_inspect') return reply(state());
    if (message.action === 'dot_phone_v2_start') {
      const current = state();
      if (current.active) return reply({error:'DOT_VOICE_ALREADY_ACTIVE'});
      const button = control('Start call');
      if (!button || button.disabled) return reply({error:'DOT_CALL_CONTROL_UNAVAILABLE'});
      button.click();
      return reply({started:true});
    }
    if (message.action === 'dot_phone_v2_end') {
      control('Hang up')?.click();
      return reply({ended:!state().active});
    }
  };
  // Reinjecting updates existing tabs without refreshing their drafts. Versioned
  // messages also prevent an old, unversioned listener from answering first.
  if (globalThis.dotPhoneListener) chrome.runtime.onMessage.removeListener(globalThis.dotPhoneListener);
  globalThis.dotPhoneListener = listener;
  chrome.runtime.onMessage.addListener(listener);
})();
