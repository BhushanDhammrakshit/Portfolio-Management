/* PortfolioPro — progressive rendering helper.
 *
 * Pairs with skeleton.css + ppCache. Typical use:
 *
 *   Progressive.load({
 *     key:    'home:summary',
 *     url:    '/api/portfolio/summary',
 *     target: '#summaryCard',                    // element to populate
 *     ttl:    30_000,
 *     render: (data) => buildSummaryHTML(data),  // returns HTML string OR node
 *     stamp:  '#summaryStamp',                   // optional "as of …" badge
 *   });
 *
 * Behaviour:
 *  - If cached data exists, paints it immediately (no spinner).
 *  - Always revalidates over the network in the background.
 *  - Replaces target HTML with fresh data when it arrives.
 *  - On network error, keeps cached content + sets a small stale badge.
 *  - The target gets `.pp-fade-in` so transitions are smooth.
 *
 * Multiple cards on the same page load in parallel — call once per card
 * after DOMContentLoaded.
 */
(function (global) {
    'use strict';

    function $(sel) { return typeof sel === 'string' ? document.querySelector(sel) : sel; }

    function paint(target, content) {
        if (!target) return;
        if (content == null) return;
        if (typeof content === 'string') {
            target.innerHTML = content;
        } else if (content instanceof Node) {
            target.innerHTML = '';
            target.appendChild(content);
        }
        target.classList.remove('pp-fade-in');
        // restart the animation
        // eslint-disable-next-line no-unused-expressions
        target.offsetWidth;
        target.classList.add('pp-fade-in');
    }

    function setStamp(el, ts, isStale) {
        if (!el || !ts) return;
        var ms = Date.now() - ts;
        var human = (global.ppCache && global.ppCache.ago)
            ? global.ppCache.ago(ms) : Math.round(ms / 1000) + 's ago';
        el.innerHTML =
            '<span class="pp-stale-badge" title="Last updated ' + human + '">' +
            '<i class="fa-regular fa-clock"></i>' +
            (isStale ? 'cached · ' : '') + human + '</span>';
    }

    function load(opts) {
        opts = opts || {};
        var key = opts.key;
        var url = opts.url;
        var target = $(opts.target);
        var stamp = opts.stamp ? $(opts.stamp) : null;
        var ttl = typeof opts.ttl === 'number' ? opts.ttl : 30_000;
        var render = opts.render || function (d) { return JSON.stringify(d); };

        if (!key || !url || !target) {
            console.warn('Progressive.load: key, url, target required');
            return Promise.resolve(null);
        }

        // 1. Paint cached value instantly (even if stale).
        if (global.ppCache) {
            var cachedEntry = global.ppCache.getEntry(key);
            if (cachedEntry) {
                try { paint(target, render(cachedEntry.v)); } catch (e) { /* ignore */ }
                setStamp(stamp, cachedEntry.t, (Date.now() - cachedEntry.t) > ttl);
            }
        }

        // 2. Always revalidate.
        return fetch(url, opts.init || { credentials: 'same-origin' })
            .then(function (r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function (data) {
                if (global.ppCache) global.ppCache.set(key, data);
                try { paint(target, render(data)); } catch (e) { /* ignore */ }
                setStamp(stamp, Date.now(), false);
                if (typeof opts.onFresh === 'function') opts.onFresh(data);
                return data;
            })
            .catch(function (err) {
                if (typeof opts.onError === 'function') opts.onError(err);
                // Leave cached content in place; show stale badge.
                if (global.ppCache && stamp) {
                    var e2 = global.ppCache.getEntry(key);
                    if (e2) setStamp(stamp, e2.t, true);
                }
                return null;
            });
    }

    /* Convenience: replace a skeleton block with rendered HTML.
       Useful when you want to keep the skeleton markup in the template. */
    function replace(target, html) {
        paint($(target), html);
    }

    global.Progressive = { load: load, replace: replace, paint: paint };
})(window);
