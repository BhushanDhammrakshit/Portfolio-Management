/* ──────────────────────────────────────────────────────────────────
   stock-suggest.js
   Reusable stock-symbol autocomplete dropdown for any text input.

   Usage:
     <input type="text" data-stock-suggest> ...
   Optional attributes:
     data-stock-suggest-upper="false"   -> do not uppercase picked symbol
     data-stock-suggest-target="#hid"   -> also set value on another input
     data-stock-suggest-on-pick="fnName"-> call window.fnName(item, input)

   Pages can also call:  StockSuggest.attach(inputEl, opts);
   Backend: GET /api/stock/search?q=...  →  {results: [{symbol,name,...}]}
   ────────────────────────────────────────────────────────────────── */
(function () {
    if (window.StockSuggest) return;

    // ── inject styles once ────────────────────────────────────────
    const STYLE_ID = 'stock-suggest-styles';
    if (!document.getElementById(STYLE_ID)) {
        const css = `
        .ss-wrap{position:relative;display:block}
        .ss-pop{
            position:absolute;left:0;right:0;top:calc(100% + 4px);
            background:var(--surface,#fff);
            border:1px solid var(--border,#e5e7eb);
            border-radius:8px;
            box-shadow:0 8px 24px rgba(0,0,0,.14);
            max-height:320px;overflow-y:auto;
            z-index:1080;display:none;
            font-size:.85rem;
            min-width:240px;
        }
        .ss-pop.open{display:block}
        .ss-item{
            display:flex;align-items:center;justify-content:space-between;gap:10px;
            padding:8px 12px;cursor:pointer;
            border-bottom:1px solid var(--border,#eef0f3);
            color:var(--text,#0f172a);
        }
        .ss-item:last-child{border-bottom:none}
        .ss-item:hover,.ss-item.active{background:var(--surface-2,#f1f5f9)}
        .ss-main{display:flex;flex-direction:column;min-width:0;flex:1}
        .ss-sym{font-weight:700;font-size:.86rem}
        .ss-name{
            color:var(--text-muted,#64748b);font-size:.74rem;
            white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
        }
        .ss-tag{
            font-size:.66rem;font-weight:700;letter-spacing:.02em;
            color:var(--text-muted,#64748b);
            background:var(--surface-2,#f1f5f9);
            border:1px solid var(--border,#e5e7eb);
            padding:2px 6px;border-radius:4px;flex-shrink:0;
        }
        .ss-msg{padding:10px 12px;text-align:center;color:var(--text-muted,#64748b);font-size:.8rem}
        `;
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = css;
        document.head.appendChild(style);
    }

    // ── tiny cache (per-page) ─────────────────────────────────────
    const cache = new Map();
    const CACHE_MAX = 60;
    const CACHE_TTL = 5 * 60 * 1000;

    async function fetchSuggest(q) {
        // Cache key is case- and whitespace-insensitive so "rel", "REL", " Rel " all share state.
        const key = q.trim().toLowerCase();
        const hit = cache.get(key);
        const now = Date.now();
        // Only honour cached entries that actually had results — never serve empty results
        // from cache (avoids poisoning when the symbol master was still warming up).
        if (hit && hit.v && hit.v.length && (now - hit.t) < CACHE_TTL) return hit.v;
        const resp = await fetch(`/api/stock/search?q=${encodeURIComponent(q)}`, {
            credentials: 'same-origin',
        });
        if (!resp.ok) {
            cache.delete(key);
            throw new Error(`HTTP ${resp.status}`);
        }
        const data = await resp.json();
        const v = Array.isArray(data.results) ? data.results : [];
        if (v.length) {
            cache.set(key, { t: now, v });
            if (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value);
        } else {
            cache.delete(key);
        }
        return v;
    }

    function escapeHtml(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    function ensureWrap(input) {
        // Wrap the input so we can absolute-position the dropdown right below it.
        if (input.parentElement && input.parentElement.classList.contains('ss-wrap')) {
            return input.parentElement;
        }
        const wrap = document.createElement('span');
        wrap.className = 'ss-wrap';
        // preserve display behavior of the input by inheriting width
        wrap.style.width = '100%';
        const parent = input.parentNode;
        parent.insertBefore(wrap, input);
        wrap.appendChild(input);
        return wrap;
    }

    function attach(input, opts) {
        if (!input || input.__ssAttached) return;
        input.__ssAttached = true;
        opts = opts || {};

        const upper = opts.upper !== false &&
            input.getAttribute('data-stock-suggest-upper') !== 'false';
        const targetSel = opts.target || input.getAttribute('data-stock-suggest-target');
        const onPickName = opts.onPick || input.getAttribute('data-stock-suggest-on-pick');
        const minChars = opts.minChars || 2;
        const debounceMs = opts.debounce || 180;

        input.setAttribute('autocomplete', 'off');
        input.setAttribute('role', 'combobox');
        input.setAttribute('aria-autocomplete', 'list');
        input.setAttribute('aria-expanded', 'false');
        input.setAttribute('spellcheck', 'false');

        const wrap = ensureWrap(input);
        const pop = document.createElement('div');
        pop.className = 'ss-pop';
        pop.setAttribute('role', 'listbox');
        wrap.appendChild(pop);

        const state = { items: [], active: -1, seq: 0, timer: 0, lastQ: '' };

        function open() {
            if (!state.items.length && !pop.innerHTML) return;
            pop.classList.add('open');
            input.setAttribute('aria-expanded', 'true');
        }
        function close() {
            pop.classList.remove('open');
            input.setAttribute('aria-expanded', 'false');
            state.active = -1;
        }
        function render() {
            if (!state.items.length) {
                pop.innerHTML = '<div class="ss-msg">No matches</div>';
                open();
                return;
            }
            pop.innerHTML = state.items.map((it, i) => {
                const sym = escapeHtml(it.symbol || '');
                const name = escapeHtml(it.name || it.company || '');
                const tag = escapeHtml(it.exchange || it.type || '');
                return `<div class="ss-item${i === state.active ? ' active' : ''}" role="option" data-idx="${i}">
                    <div class="ss-main">
                        <span class="ss-sym">${sym}</span>
                        ${name ? `<span class="ss-name">${name}</span>` : ''}
                    </div>
                    ${tag ? `<span class="ss-tag">${tag}</span>` : ''}
                </div>`;
            }).join('');
            open();
        }
        function setActive(i) {
            if (!state.items.length) return;
            state.active = (i + state.items.length) % state.items.length;
            render();
            const el = pop.querySelector('.ss-item.active');
            if (el) el.scrollIntoView({ block: 'nearest' });
        }
        function pick(item) {
            if (!item) return;
            const sym = (item.symbol || '').toString();
            input.value = upper ? sym.toUpperCase() : sym;
            if (targetSel) {
                const tgt = document.querySelector(targetSel);
                if (tgt) {
                    tgt.value = input.value;
                    tgt.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }
            state.items = [];
            pop.innerHTML = '';
            close();
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            if (onPickName && typeof window[onPickName] === 'function') {
                try { window[onPickName](item, input); } catch (_) { /* noop */ }
            }
        }

        input.addEventListener('input', () => {
            const q = (input.value || '').trim();
            state.lastQ = q;
            clearTimeout(state.timer);
            if (q.length < minChars) {
                state.items = [];
                pop.innerHTML = '';
                close();
                return;
            }
            const mySeq = ++state.seq;
            state.timer = setTimeout(async () => {
                try {
                    const items = await fetchSuggest(q);
                    if (mySeq !== state.seq) return;
                    state.items = items.slice(0, 12);
                    state.active = -1;
                    render();
                } catch (e) {
                    if (mySeq !== state.seq) return;
                    state.items = [];
                    pop.innerHTML = '<div class="ss-msg">Search failed</div>';
                    open();
                }
            }, debounceMs);
        });

        input.addEventListener('keydown', (e) => {
            if (!pop.classList.contains('open')) {
                if (e.key === 'ArrowDown' && state.items.length) { open(); e.preventDefault(); }
                return;
            }
            if (e.key === 'ArrowDown') { e.preventDefault(); setActive(state.active + 1); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(state.active - 1); }
            else if (e.key === 'Enter') {
                if (state.active >= 0 && state.items[state.active]) {
                    e.preventDefault();
                    pick(state.items[state.active]);
                }
            } else if (e.key === 'Escape') {
                close();
            }
        });

        input.addEventListener('focus', () => {
            if (state.items.length) open();
        });

        pop.addEventListener('mousedown', (e) => {
            const row = e.target.closest('.ss-item');
            if (!row) return;
            e.preventDefault();
            const idx = parseInt(row.dataset.idx, 10);
            if (!isNaN(idx)) pick(state.items[idx]);
        });

        document.addEventListener('mousedown', (e) => {
            if (!wrap.contains(e.target)) close();
        });
    }

    function autoInit(root) {
        const scope = root || document;
        scope.querySelectorAll('input[data-stock-suggest]').forEach((el) => attach(el));
    }

    // Observe dynamically added inputs (e.g. inside modals rendered later).
    function startObserver() {
        if (!('MutationObserver' in window)) return;
        const mo = new MutationObserver((muts) => {
            for (const m of muts) {
                m.addedNodes.forEach((n) => {
                    if (n.nodeType !== 1) return;
                    if (n.matches && n.matches('input[data-stock-suggest]')) attach(n);
                    if (n.querySelectorAll) {
                        n.querySelectorAll('input[data-stock-suggest]').forEach((el) => attach(el));
                    }
                });
            }
        });
        mo.observe(document.body, { childList: true, subtree: true });
    }

    window.StockSuggest = { attach, autoInit };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => { autoInit(); startObserver(); });
    } else {
        autoInit();
        startObserver();
    }
})();
