/* PortfolioPro — branded full-screen loader.
 *
 * Lightweight helper paired with the .pp-loader-overlay CSS in style.css.
 * Use for page-level long ops (login submit, AI run, refresh-all, etc.)
 * where a single page-wide indicator is clearer than per-card spinners.
 *
 * API:
 *   PPLoader.show('Refreshing prices…', { sub: 'This may take a few seconds' });
 *   PPLoader.show('Loading…');                 // sub optional
 *   PPLoader.hide();
 *   PPLoader.with(promise, 'Working…');        // auto-hide when settled
 *
 * Multiple show() calls before hide() are reference-counted so nested
 * async tasks don't dismiss the loader prematurely.
 */
(function (global) {
    'use strict';
    if (global.PPLoader) return;

    var refs = 0;
    var rootId = 'ppGlobalLoader';

    function ensure(label, sub) {
        var el = document.getElementById(rootId);
        if (!el) {
            el = document.createElement('div');
            el.id = rootId;
            el.className = 'pp-loader-overlay';
            el.setAttribute('role', 'status');
            el.setAttribute('aria-live', 'polite');
            el.innerHTML =
                '<div class="pp-loader-card">' +
                  '<span class="app-spinner xl text-primary" aria-hidden="true"></span>' +
                  '<div class="pp-loader-label"></div>' +
                  '<div class="pp-loader-sub"></div>' +
                '</div>';
            document.body.appendChild(el);
        }
        el.querySelector('.pp-loader-label').textContent = label || 'Loading…';
        var subEl = el.querySelector('.pp-loader-sub');
        subEl.textContent = sub || '';
        subEl.style.display = sub ? '' : 'none';
        return el;
    }

    function show(label, opts) {
        opts = opts || {};
        refs += 1;
        ensure(label, opts.sub);
    }

    function hide() {
        refs = Math.max(0, refs - 1);
        if (refs > 0) return;
        var el = document.getElementById(rootId);
        if (el && el.parentNode) el.parentNode.removeChild(el);
    }

    function withPromise(promise, label, opts) {
        show(label, opts);
        return Promise.resolve(promise).finally(hide);
    }

    global.PPLoader = { show: show, hide: hide, with: withPromise };
})(window);
