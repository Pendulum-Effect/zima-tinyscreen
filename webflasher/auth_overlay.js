// Shared PIN-authentication plumbing (extracted from dashboard.html in
// 0.9.5.2 so the WIZARD gets it too -- its server-side flash/configure
// path hits the same gated endpoints, and a PIN-enabled box was
// silently breaking it with unexplained 401s).
//
// Load this BEFORE any script that calls fetch(). Every API call on the
// page then goes through the wrapper below: when the server answers
// 401 + auth_required (the optional dashboard PIN is enabled and this
// browser has no session), a PIN overlay appears; after a successful
// login the ORIGINAL request is retried once, so no call site anywhere
// needs to know auth exists. Concurrent 401s share one overlay via a
// single promise.
//
// Self-contained on purpose: injects its own styles (with fallbacks for
// any theme variable a page doesn't define) so including one script tag
// is the entire integration.
(() => {
  'use strict';
  if (window.__tinyscreenAuthOverlay) return;  // idempotent
  window.__tinyscreenAuthOverlay = true;

  const style = document.createElement('style');
  style.textContent = [
    '.pin-overlay {',
    '  position: fixed; inset: 0; z-index: 200;',
    '  background: rgba(6, 14, 17, 0.88); backdrop-filter: blur(3px);',
    '  display: flex; align-items: center; justify-content: center;',
    '}',
    '.pin-box {',
    '  background: var(--panel, #101c22); border: 1px solid #17313a; border-radius: 14px;',
    '  padding: 26px; width: min(340px, calc(100vw - 48px));',
    '  display: flex; flex-direction: column; gap: 10px;',
    '  color: var(--text, #dfe9ec); font-size: 14px;',
    '}',
    '.pin-box .pin-title { font-weight: 700; font-size: 16px; }',
    '.pin-input {',
    '  width: 100%; box-sizing: border-box; padding: 11px 12px;',
    '  border-radius: 10px; border: 1px solid #24424d;',
    '  background: var(--panel-raised, #142027); color: var(--text, #dfe9ec);',
    '  font-size: 15px; letter-spacing: 2px;',
    '}',
    '.pin-input:focus { outline: none; border-color: var(--teal, #4fd1c5); }',
    '.pin-error:not(:empty) { color: var(--err, #ff9a7a); }',
    '.pin-unlock {',
    '  padding: 11px 24px; border-radius: 10px; border: none; cursor: pointer;',
    '  background: var(--teal, #4fd1c5); color: #06201d; font-weight: 700; font-size: 14.5px;',
    '}',
    '.pin-unlock:disabled { background: #2c3f3d; color: #6a827e; cursor: not-allowed; }',
  ].join('\n');
  document.head.appendChild(style);

  const rawFetch = window.fetch.bind(window);
  let pinPrompt = null;  // in-flight overlay promise, shared

  window.fetch = async (input, init) => {
    const res = await rawFetch(input, init);
    if (res.status !== 401) return res;
    let payload = null;
    try { payload = await res.clone().json(); } catch (e) { /* not ours */ }
    if (!payload || !payload.auth_required) return res;
    const unlocked = await requirePin();
    return unlocked ? rawFetch(input, init) : res;
  };

  function requirePin() {
    if (pinPrompt) return pinPrompt;
    pinPrompt = new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'pin-overlay';
      overlay.innerHTML =
        '<div class="pin-box">' +
        '  <div class="pin-title">Dashboard locked</div>' +
        '  <div>Enter the PIN to continue.</div>' +
        '  <input type="password" class="pin-input" autocomplete="current-password" />' +
        '  <div class="pin-error"></div>' +
        '  <button class="pin-unlock">Unlock</button>' +
        '</div>';
      document.body.appendChild(overlay);
      const input = overlay.querySelector('.pin-input');
      const err = overlay.querySelector('.pin-error');
      const btn = overlay.querySelector('.pin-unlock');
      input.focus();

      const attempt = async () => {
        if (!input.value) return;
        btn.disabled = true;
        err.textContent = '';
        try {
          // rawFetch on purpose: the wrapper must never intercept its
          // own login call.
          const r = await rawFetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-TinyScreen-Request': '1' },
            body: JSON.stringify({ pin: input.value }),
          });
          const data = await r.json();
          if (data.ok) {
            overlay.remove();
            pinPrompt = null;
            resolve(true);
            return;
          }
          err.textContent = data.error || 'Wrong PIN.';
          input.value = '';
          input.focus();
        } catch (e) {
          err.textContent = 'Could not reach the app: ' + e.message;
        } finally {
          btn.disabled = false;
        }
      };
      btn.addEventListener('click', attempt);
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') attempt(); });
    });
    return pinPrompt;
  }
})();
