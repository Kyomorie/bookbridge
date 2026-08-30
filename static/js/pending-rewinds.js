(() => {
  'use strict';

  const ROOT_ID = 'pending-rewinds-panel';

  function pct(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `${(number * 100).toFixed(2)}%` : '—';
  }

  function escapeHtml(value) {
    const node = document.createElement('span');
    node.textContent = String(value ?? '');
    return node.innerHTML;
  }

  function relativeExpiry(epochSeconds) {
    const seconds = Math.max(0, Number(epochSeconds || 0) - Date.now() / 1000);
    if (seconds < 60) return 'expires in under a minute';
    if (seconds < 3600) return `expires in ${Math.ceil(seconds / 60)} min`;
    return `expires in ${Math.ceil(seconds / 3600)} h`;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Accept': 'application/json', ...(options.headers || {}) },
      ...options,
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* no body */ }
    if (!response.ok) {
      const error = new Error(payload.error || payload.status || `HTTP ${response.status}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function card(item) {
    const article = document.createElement('article');
    article.className = 'pending-rewind-card';
    article.dataset.rewindId = String(item.id);
    article.innerHTML = `
      <div class="pending-rewind-copy">
        <div class="pending-rewind-title">${escapeHtml(item.book_title)}</div>
        <div class="pending-rewind-progress">
          <span>Audiobookshelf ${pct(item.current_pct)}</span>
          <span aria-hidden="true">→</span>
          <strong>KoSync ${pct(item.proposed_pct)}</strong>
        </div>
        <div class="pending-rewind-meta">Backward progress is paused for confirmation · ${escapeHtml(relativeExpiry(item.expires_at))}</div>
      </div>
      <div class="pending-rewind-actions">
        <button type="button" class="btn btn-success" data-action="approve">Accept rewind</button>
        <button type="button" class="btn btn-muted" data-action="dismiss">Keep current</button>
      </div>
      <div class="pending-rewind-status" role="status" aria-live="polite"></div>`;
    return article;
  }

  function ensureRoot() {
    let root = document.getElementById(ROOT_ID);
    if (root) return root;
    const wrap = document.querySelector('.suggestions-wrap');
    if (!wrap) return null;
    root = document.createElement('section');
    root.id = ROOT_ID;
    root.className = 'pending-rewinds-panel';
    root.hidden = true;
    root.innerHTML = `
      <div class="pending-rewind-heading">
        <div>
          <h3>Pending reading-position rewinds</h3>
          <p>BookBridge kept Audiobookshelf unchanged because KoSync proposed a backward position.</p>
        </div>
      </div>
      <div class="pending-rewind-list"></div>`;
    const header = wrap.querySelector('.page-header');
    if (header && header.nextSibling) header.parentNode.insertBefore(root, header.nextSibling);
    else wrap.prepend(root);
    return root;
  }

  async function load() {
    const root = ensureRoot();
    if (!root) return;
    try {
      const payload = await request('/api/pending-rewinds');
      const items = Array.isArray(payload.items) ? payload.items : [];
      const list = root.querySelector('.pending-rewind-list');
      list.replaceChildren(...items.map(card));
      root.hidden = items.length === 0;
    } catch (error) {
      console.warn('Pending rewinds unavailable:', error);
      root.hidden = true;
    }
  }

  async function decide(button) {
    const cardNode = button.closest('.pending-rewind-card');
    const action = button.dataset.action;
    const id = cardNode?.dataset.rewindId;
    if (!cardNode || !id || !['approve', 'dismiss'].includes(action)) return;

    const status = cardNode.querySelector('.pending-rewind-status');
    const buttons = cardNode.querySelectorAll('button');
    buttons.forEach((node) => { node.disabled = true; });
    status.textContent = action === 'approve' ? 'Revalidating both positions…' : 'Keeping current position…';

    try {
      const payload = await request(`/api/pending-rewinds/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
      status.textContent = payload.status === 'approved' ? 'Rewind applied.' : 'Current position kept.';
      cardNode.remove();
      const root = document.getElementById(ROOT_ID);
      if (root && !root.querySelector('.pending-rewind-card')) root.hidden = true;
    } catch (error) {
      if (error.status === 409) {
        status.textContent = 'This request is stale or expired. No rewind was applied.';
        cardNode.remove();
        const root = document.getElementById(ROOT_ID);
        if (root && !root.querySelector('.pending-rewind-card')) root.hidden = true;
      } else {
        status.textContent = 'Could not apply this decision. Nothing was changed.';
        buttons.forEach((node) => { node.disabled = false; });
      }
    }
  }

  document.addEventListener('click', (event) => {
    const button = event.target.closest('#pending-rewinds-panel button[data-action]');
    if (button) decide(button);
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', load);
  else load();
})();
