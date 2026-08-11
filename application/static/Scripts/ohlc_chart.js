/**
 * Reusable interactive candlestick chart used by Intraday Scanner and
 * Volume Alerts. Pure canvas, no external chart library.
 *
 * Usage:
 *   const detach = OHLCChart.mount({
 *       wrap:    document.getElementById('chartWrap'), // container with toolbar+canvas+tooltip
 *       canvas:  document.getElementById('myChart'),
 *       symbol:  'RELIANCE.NS',
 *       initialCandles: [{t,o,h,l,c}, ...],   // optional — if provided, no initial fetch
 *       initialTf: '15m',                     // must match an active toolbar button
 *   });
 *
 * The `wrap` is expected to contain:
 *   .ct-btn[data-tf="5m|15m|1h|1d"]   – timeframe buttons
 *   .ct-btn[data-range="all|40|20"]   – range slice buttons
 *   .ct-btn#ctToggleMA                – MA(5) toggle
 *   #ctReadout                        – text readout span
 *   #ctTfLabel, #ctBarCount, #ctRangeLabel – meta text spans
 *   .candle-tooltip                   – absolute-positioned tooltip div
 */
(function () {
    "use strict";

    const TF_LABELS = { '5m': '5-min candles', '15m': '15-min candles', '1h': '1-hour candles', '1d': 'Daily candles' };
    const fmtN = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 });

    function drawMessage(canvas, text, isError) {
        const dpr = window.devicePixelRatio || 1;
        const cssW = canvas.clientWidth || 600;
        const cssH = canvas.clientHeight || 220;
        canvas.width = Math.round(cssW * dpr);
        canvas.height = Math.round(cssH * dpr);
        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, cssW, cssH);
        const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
        ctx.fillStyle = isError ? '#ef4444' : (isDark ? '#94a3b8' : '#64748b');
        ctx.font = '13px Inter, system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        const lines = String(text).split('\n');
        const lh = 18;
        const startY = cssH / 2 - ((lines.length - 1) * lh) / 2;
        lines.forEach((line, i) => {
            ctx.fillText(line, cssW / 2, startY + i * lh);
        });
    }

    function drawCandles(canvas, candles, opts) {
        opts = opts || {};
        const showMA = !!opts.showMA;
        const hoverIdx = (typeof opts.hoverIdx === 'number') ? opts.hoverIdx : -1;

        const dpr = window.devicePixelRatio || 1;
        const cssW = canvas.clientWidth || 600;
        const cssH = canvas.clientHeight || 220;
        canvas.width = Math.round(cssW * dpr);
        canvas.height = Math.round(cssH * dpr);
        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, cssW, cssH);

        const padL = 44, padR = 8, padT = 8, padB = 18;
        const w = cssW - padL - padR, h = cssH - padT - padB;
        const n = candles.length;
        if (n < 1) return null;
        const gap = w / n;
        const cw = Math.max(2, gap * 0.7);

        let lo = Infinity, hi = -Infinity;
        candles.forEach(c => { lo = Math.min(lo, c.l); hi = Math.max(hi, c.h); });
        if (lo === hi) hi = lo + 1;
        const pad = (hi - lo) * 0.05;
        lo -= pad; hi += pad;
        const range = hi - lo;
        const yOf = v => padT + h - ((v - lo) / range) * h;
        const xOf = i => padL + gap * i + (gap - cw) / 2;

        const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
        const upColor = isDark ? '#22c55e' : '#16a34a';
        const dnColor = '#ef4444';
        const gridColor = isDark ? 'rgba(148,163,184,.15)' : 'rgba(100,116,139,.18)';
        const axisColor = isDark ? '#94a3b8' : '#64748b';

        ctx.strokeStyle = gridColor;
        ctx.fillStyle = axisColor;
        ctx.font = '10px Inter, system-ui, sans-serif';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.lineWidth = 1;
        for (let i = 0; i <= 4; i++) {
            const v = lo + (range * i / 4);
            const y = yOf(v);
            ctx.beginPath();
            ctx.moveTo(padL, y);
            ctx.lineTo(padL + w, y);
            ctx.stroke();
            ctx.fillText(v.toFixed(2), padL - 4, y);
        }

        candles.forEach((c, i) => {
            const x = xOf(i);
            const cx = x + cw / 2;
            const up = c.c >= c.o;
            const color = up ? upColor : dnColor;
            ctx.strokeStyle = color;
            ctx.fillStyle = color;
            ctx.globalAlpha = 0.9;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(cx, yOf(c.h));
            ctx.lineTo(cx, yOf(c.l));
            ctx.stroke();
            ctx.globalAlpha = 1;
            const top = yOf(Math.max(c.o, c.c));
            const bot = yOf(Math.min(c.o, c.c));
            ctx.fillRect(x, top, cw, Math.max(1, bot - top));
        });

        if (showMA && n >= 5) {
            ctx.strokeStyle = isDark ? '#facc15' : '#ca8a04';
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            let started = false;
            for (let i = 4; i < n; i++) {
                const avg = (candles[i].c + candles[i - 1].c + candles[i - 2].c +
                    candles[i - 3].c + candles[i - 4].c) / 5;
                const x = xOf(i) + cw / 2;
                const y = yOf(avg);
                if (!started) { ctx.moveTo(x, y); started = true; }
                else ctx.lineTo(x, y);
            }
            ctx.stroke();
        }

        if (hoverIdx >= 0 && hoverIdx < n) {
            const c = candles[hoverIdx];
            const cx = xOf(hoverIdx) + cw / 2;
            ctx.strokeStyle = isDark ? 'rgba(226,232,240,.45)' : 'rgba(15,23,42,.45)';
            ctx.setLineDash([3, 3]);
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(cx, padT);
            ctx.lineTo(cx, padT + h);
            ctx.stroke();
            const yc = yOf(c.c);
            ctx.beginPath();
            ctx.moveTo(padL, yc);
            ctx.lineTo(padL + w, yc);
            ctx.stroke();
            ctx.setLineDash([]);
        }

        return { padL, padT, w, h, gap, cw, n };
    }

    async function fetchCandles(symbol, tf) {
        const r = await fetch(`/api/intraday/candles?symbol=${encodeURIComponent(symbol)}&tf=${tf}`);
        const data = await r.json();
        if (!r.ok || !data.ok || !data.candles || data.candles.length < 2) {
            throw new Error(data.error || 'no data');
        }
        return data.candles;
    }

    function mount(opts) {
        const wrap = opts.wrap;
        const canvas = opts.canvas;
        const symbol = opts.symbol;
        const tooltip = wrap.querySelector('.candle-tooltip');
        const readout = wrap.querySelector('#ctReadout, [data-role="ct-readout"]');
        const tfLabelEl = wrap.querySelector('#ctTfLabel, [data-role="ct-tf-label"]');
        const barCountEl = wrap.querySelector('#ctBarCount, [data-role="ct-bar-count"]');
        const rangeLabelEl = wrap.querySelector('#ctRangeLabel, [data-role="ct-range-label"]');

        const state = {
            full: opts.initialCandles || [],
            candles: opts.initialCandles || [],
            tf: opts.initialTf || '15m',
            showMA: false,
            hoverIdx: -1,
            geom: null,
            rangeFilter: 'all',
            status: '', // '' | 'loading' | 'error' | 'empty'
            statusMsg: '',
        };

        const applyRange = () => {
            const r = state.rangeFilter;
            state.candles = (r === 'all') ? state.full : state.full.slice(-parseInt(r, 10));
        };
        const updateMeta = () => {
            if (tfLabelEl) tfLabelEl.textContent = TF_LABELS[state.tf] || state.tf;
            if (barCountEl) barCountEl.textContent = state.candles.length;
            if (rangeLabelEl && state.candles.length) {
                const lo = Math.min(...state.candles.map(c => c.l));
                const hi = Math.max(...state.candles.map(c => c.h));
                rangeLabelEl.textContent = `Low ${fmtN(lo)} · High ${fmtN(hi)}`;
            }
        };
        const render = () => {
            applyRange();
            if (state.status && state.status !== 'ok') {
                state.geom = null;
                drawMessage(canvas, state.statusMsg || state.status, state.status === 'error');
            } else if (!state.candles || state.candles.length < 1) {
                state.geom = null;
                drawMessage(canvas, 'No candle data', false);
            } else {
                state.geom = drawCandles(canvas, state.candles, {
                    showMA: state.showMA,
                    hoverIdx: state.hoverIdx,
                });
            }
            updateMeta();
        };

        canvas.onmousemove = (e) => {
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const g = state.geom;
            if (!g) return;
            if (x < g.padL || x > g.padL + g.w) {
                if (state.hoverIdx !== -1) {
                    state.hoverIdx = -1;
                    render();
                    if (tooltip) tooltip.style.display = 'none';
                    if (readout) readout.textContent = 'Hover a candle';
                }
                return;
            }
            const idx = Math.floor((x - g.padL) / g.gap);
            const clamped = Math.max(0, Math.min(g.n - 1, idx));
            if (clamped !== state.hoverIdx) { state.hoverIdx = clamped; render(); }
            const c = state.candles[clamped];
            const up = c.c >= c.o;
            const change = c.c - c.o;
            const pct = (change / c.o) * 100;
            const cls = up ? 'ct-up' : 'ct-dn';
            const arrow = up ? '▲' : '▼';
            const tStr = c.t ? new Date(c.t).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: true }) : `Bar ${clamped + 1}`;
            if (tooltip) {
                tooltip.innerHTML = `
                    <div class="ct-head">${tStr}</div>
                    <div class="ct-row"><b>Open</b><span>${fmtN(c.o)}</span></div>
                    <div class="ct-row"><b>High</b><span>${fmtN(c.h)}</span></div>
                    <div class="ct-row"><b>Low</b><span>${fmtN(c.l)}</span></div>
                    <div class="ct-row"><b>Close</b><span class="${cls}">${fmtN(c.c)}</span></div>
                    <div class="ct-row"><b>Change</b><span class="${cls}">${arrow} ${pct.toFixed(2)}%</span></div>`;
                const tw = tooltip.offsetWidth || 150;
                const wrapRect = wrap.getBoundingClientRect();
                let tx = e.clientX - wrapRect.left + 12;
                let ty = e.clientY - wrapRect.top + 12;
                if (tx + tw > wrap.clientWidth - 6) tx = e.clientX - wrapRect.left - tw - 12;
                tooltip.style.left = tx + 'px';
                tooltip.style.top = ty + 'px';
                tooltip.style.display = 'block';
            }
            if (readout) readout.textContent = `O ${fmtN(c.o)}  H ${fmtN(c.h)}  L ${fmtN(c.l)}  C ${fmtN(c.c)}  ${arrow}${pct.toFixed(2)}%`;
        };

        canvas.onmouseleave = () => {
            state.hoverIdx = -1;
            if (tooltip) tooltip.style.display = 'none';
            // Don't overwrite an error/loading message in the readout
            if (readout && state.status === 'ok') readout.textContent = 'Hover a candle';
            render();
        };

        wrap.querySelectorAll('.ct-btn[data-range]').forEach(btn => {
            btn.onclick = () => {
                wrap.querySelectorAll('.ct-btn[data-range]').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                state.rangeFilter = btn.dataset.range;
                state.hoverIdx = -1;
                if (tooltip) tooltip.style.display = 'none';
                render();
            };
        });

        wrap.querySelectorAll('.ct-btn[data-tf]').forEach(btn => {
            btn.onclick = async () => {
                const tf = btn.dataset.tf;
                if (tf === state.tf || btn.disabled) return;
                const prevText = btn.textContent;
                wrap.querySelectorAll('.ct-btn[data-tf]').forEach(b => { b.disabled = true; });
                btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
                state.status = 'loading';
                state.statusMsg = 'Loading ' + tf + ' candles…';
                if (readout) readout.textContent = state.statusMsg;
                render();
                try {
                    const candles = await fetchCandles(symbol, tf);
                    wrap.querySelectorAll('.ct-btn[data-tf]').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    state.tf = tf;
                    state.full = candles;
                    state.hoverIdx = -1;
                    state.status = 'ok';
                    state.statusMsg = '';
                    if (tooltip) tooltip.style.display = 'none';
                    if (readout) readout.textContent = 'Hover a candle';
                    render();
                } catch (err) {
                    state.status = 'error';
                    state.statusMsg = 'Could not load ' + tf + ': ' + err.message;
                    if (readout) readout.textContent = state.statusMsg;
                    render();
                } finally {
                    btn.innerHTML = prevText;
                    wrap.querySelectorAll('.ct-btn[data-tf]').forEach(b => { b.disabled = false; });
                }
            };
        });

        const maBtn = wrap.querySelector('.ct-btn#ctToggleMA, .ct-btn[data-role="ct-toggle-ma"]');
        if (maBtn) {
            maBtn.onclick = () => {
                state.showMA = !state.showMA;
                maBtn.classList.toggle('active', state.showMA);
                render();
            };
        }

        // If no initial candles supplied, fetch the initial timeframe
        if (state.full.length === 0) {
            state.status = 'loading';
            state.statusMsg = 'Loading candles…';
            if (readout) readout.textContent = state.statusMsg;
            render();
            (async () => {
                try {
                    const candles = await fetchCandles(symbol, state.tf);
                    state.full = candles;
                    state.status = 'ok';
                    state.statusMsg = '';
                    if (readout) readout.textContent = 'Hover a candle';
                    render();
                } catch (err) {
                    state.status = 'error';
                    state.statusMsg = 'Could not load candles\n' + err.message;
                    if (readout) readout.textContent = 'Error: ' + err.message;
                    render();
                }
            })();
        } else {
            state.status = 'ok';
            render();
        }

        return function detach() {
            canvas.onmousemove = null;
            canvas.onmouseleave = null;
        };
    }

    window.OHLCChart = { mount, drawCandles, fetchCandles };
})();
