/* PortfolioPro client-side cache
 * Persists API responses across navigations AND browser sessions so
 * revisiting a page does NOT show a loading spinner if any prior
 * payload exists. Users can always tap the page's Refresh / Scan
 * button to fetch fresh data; cached entries also carry a timestamp
 * so the UI can show "Last updated at …".
 *
 * Storage: localStorage. Auto-evicts oldest entry on quota errors.
 * Each entry: { t: <ms epoch>, v: <any JSON-serialisable value> }
 *
 * Public API:
 *   ppCache.get(key, maxAgeMs)  -> value | null  (omit maxAgeMs for any age)
 *   ppCache.set(key, value)
 *   ppCache.del(key)
 *   ppCache.age(key)            -> ms since cached, or Infinity
 *   ppCache.getEntry(key)       -> { t, v } | null  (raw record incl. timestamp)
 *   ppCache.fetchJSON(key, url, opts)
 *       opts: {
 *         ttl,             // ms freshness window (default 60_000)
 *         init,            // fetch() init (method, headers, body, ...)
 *         onCache(value),  // called immediately with cached value if fresh
 *         onFresh(value),  // called when network response arrives
 *         onError(err),    // called on network failure (cache still used)
 *         revalidate,      // bool: also hit network even when cache is fresh
 *       }
 *     Returns a Promise that resolves with the value finally rendered
 *     (fresh if network succeeded, otherwise the cached value).
 */
(function (global) {
    'use strict';

    var PREFIX = 'pp:';

    function _key(k) { return PREFIX + k; }

    function _safeParse(s) {
        try { return JSON.parse(s); } catch (e) { return null; }
    }

    var _store = (function () {
        try {
            var k = '__pp_test__';
            window.localStorage.setItem(k, '1');
            window.localStorage.removeItem(k);
            return window.localStorage;
        } catch (e) {
            try { return window.sessionStorage; } catch (e2) { return null; }
        }
    })();

    function _evictOldest() {
        if (!_store) return;
        try {
            var oldest = null, oldestKey = null;
            for (var i = 0; i < _store.length; i++) {
                var k = _store.key(i);
                if (!k || k.indexOf(PREFIX) !== 0) continue;
                var rec = _safeParse(_store.getItem(k));
                if (rec && (oldest === null || rec.t < oldest)) {
                    oldest = rec.t; oldestKey = k;
                }
            }
            if (oldestKey) _store.removeItem(oldestKey);
        } catch (e) { /* ignore */ }
    }

    function getEntry(key) {
        if (!_store) return null;
        try {
            var rec = _safeParse(_store.getItem(_key(key)));
            if (!rec || typeof rec.t !== 'number') return null;
            return rec;
        } catch (e) { return null; }
    }

    function get(key, maxAgeMs) {
        var rec = getEntry(key);
        if (!rec) return null;
        if (typeof maxAgeMs === 'number' && (Date.now() - rec.t) > maxAgeMs) return null;
        return rec.v;
    }

    function set(key, value) {
        if (!_store) return;
        var payload = JSON.stringify({ t: Date.now(), v: value });
        try {
            _store.setItem(_key(key), payload);
        } catch (e) {
            // Likely QuotaExceeded — evict and retry once.
            _evictOldest();
            try { _store.setItem(_key(key), payload); } catch (e2) { /* give up */ }
        }
    }

    function del(key) {
        if (!_store) return;
        try { _store.removeItem(_key(key)); } catch (e) { /* ignore */ }
    }

    function age(key) {
        var rec = getEntry(key);
        if (!rec) return Infinity;
        return Date.now() - rec.t;
    }

    function fetchJSON(key, url, opts) {
        opts = opts || {};
        var ttl = typeof opts.ttl === 'number' ? opts.ttl : 60000;
        var cached = get(key, ttl);
        var hasFresh = cached !== null;

        if (hasFresh && typeof opts.onCache === 'function') {
            try { opts.onCache(cached); } catch (e) { /* user code */ }
        }

        // If cached is fresh AND caller doesn't want revalidation, short-circuit.
        if (hasFresh && !opts.revalidate) {
            return Promise.resolve(cached);
        }

        return fetch(url, opts.init || undefined)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                set(key, data);
                if (typeof opts.onFresh === 'function') {
                    try { opts.onFresh(data); } catch (e) { /* user code */ }
                }
                return data;
            })
            .catch(function (err) {
                if (typeof opts.onError === 'function') {
                    try { opts.onError(err); } catch (e) { /* user code */ }
                }
                if (hasFresh) return cached;
                throw err;
            });
    }

    /* Format helper: "3m ago", "just now", "2h ago", or absolute time for >24h. */
    function ago(ms) {
        if (!isFinite(ms) || ms < 0) return '';
        var s = Math.round(ms / 1000);
        if (s < 10) return 'just now';
        if (s < 60) return s + 's ago';
        var m = Math.round(s / 60);
        if (m < 60) return m + 'm ago';
        var h = Math.round(m / 60);
        if (h < 24) return h + 'h ago';
        var d = new Date(Date.now() - ms);
        return d.toLocaleString('en-IN');
    }

    global.ppCache = {
        get: get,
        set: set,
        del: del,
        age: age,
        getEntry: getEntry,
        ago: ago,
        fetchJSON: fetchJSON,
    };
})(window);
