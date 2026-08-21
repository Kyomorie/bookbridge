(function () {
    'use strict';

    function setExpanded(button, panel, expanded) {
        button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        button.textContent = expanded ? 'Hide position' : 'Show position';
        panel.hidden = !expanded;
    }

    function setLoading(panel) {
        panel.dataset.state = 'loading';
        panel.querySelector('[data-position-preview-title]').textContent = 'Current reading position';
        panel.querySelector('[data-position-preview-meta]').textContent = 'Resolving saved position…';
        panel.querySelector('[data-position-preview-before]').textContent = '';
        panel.querySelector('[data-position-preview-after]').textContent = '';
        panel.querySelector('[data-position-preview-marker]').hidden = true;
        panel.querySelector('[data-position-preview-message]').textContent = '';
    }

    function renderPayload(panel, payload) {
        const state = payload && payload.status ? payload.status : 'error';
        const source = payload && payload.source ? String(payload.source) : 'BookBridge';
        const confidence = payload && payload.confidence ? String(payload.confidence) : 'Unavailable';
        const percentage = payload && Number.isFinite(Number(payload.percentage))
            ? `${Number(payload.percentage).toFixed(1)}%`
            : '';
        const meta = [source, percentage, confidence].filter(Boolean).join(' · ');
        const before = payload && payload.before ? String(payload.before) : '';
        const after = payload && payload.after ? String(payload.after) : '';
        const hasText = Boolean(before || after);

        panel.dataset.state = state;
        panel.querySelector('[data-position-preview-title]').textContent =
            state === 'approximate' ? 'Approximate reading position' :
            state === 'unavailable' ? 'Reading position unavailable' :
            'Current reading position';
        panel.querySelector('[data-position-preview-meta]').textContent = meta;
        panel.querySelector('[data-position-preview-before]').textContent = before ? `…${before}` : '';
        panel.querySelector('[data-position-preview-after]').textContent = after ? `${after}…` : '';
        panel.querySelector('[data-position-preview-marker]').hidden = !hasText;
        panel.querySelector('[data-position-preview-message]').textContent =
            payload && payload.message ? String(payload.message) : '';
    }

    function renderError(panel) {
        panel.dataset.state = 'error';
        panel.querySelector('[data-position-preview-title]').textContent = 'Reading position unavailable';
        panel.querySelector('[data-position-preview-meta]').textContent = 'Could not load the preview';
        panel.querySelector('[data-position-preview-before]').textContent = '';
        panel.querySelector('[data-position-preview-after]').textContent = '';
        panel.querySelector('[data-position-preview-marker]').hidden = true;
        panel.querySelector('[data-position-preview-message]').textContent =
            'Try again. Your saved progress was not changed.';
    }

    async function loadPreview(panel, absId) {
        setLoading(panel);
        try {
            const response = await fetch(
                `/api/books/${encodeURIComponent(absId)}/position-preview`,
                { cache: 'no-store', headers: { Accept: 'application/json' } }
            );
            const payload = await response.json().catch(function () { return {}; });
            if (!response.ok) {
                throw new Error('preview request failed');
            }
            renderPayload(panel, payload);
        } catch (_error) {
            // Deliberately do not log payloads or excerpts: the response contains
            // copyrighted book text and should not leak into browser diagnostics.
            renderError(panel);
        }
    }

    document.addEventListener('click', function (event) {
        const button = event.target.closest('[data-position-preview-toggle]');
        if (!button) return;

        const card = button.closest('.book-card');
        if (!card) return;
        const panelId = button.getAttribute('aria-controls');
        const panel = panelId ? document.getElementById(panelId) : null;
        const absId = card.dataset.absId || '';
        if (!panel || !absId) return;

        const isExpanded = button.getAttribute('aria-expanded') === 'true';
        if (isExpanded) {
            setExpanded(button, panel, false);
            return;
        }

        setExpanded(button, panel, true);
        loadPreview(panel, absId);
    });
})();
