(function () {
    'use strict';

    const root = document.getElementById('indexDetail');
    if (!root) return;

    const ticker = root.dataset.ticker;
    const ranges = ['1D', '1W', '1M', '3M', '6M', '1Y', '5Y'];
    const state = {
        range: '1D',
        points: [],
        candles: [],
        viewStart: 0,
        viewCount: 0,
        fitted: false,
        selectedTab: 'prices',
        historyRequest: 0
    };
    const numberFormat = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 });
    const compactFormat = new Intl.NumberFormat('en-IN', { notation: 'compact', maximumFractionDigits: 2 });
    const currencyFormat = new Intl.NumberFormat('en-IN', {
        style: 'currency',
        currency: 'INR',
        maximumFractionDigits: 2
    });

    function isFiniteValue(value) {
        return value !== null && value !== '' && Number.isFinite(Number(value));
    }

    function escapeHTML(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, function (char) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char];
        });
    }

    function price(value) {
        return isFiniteValue(value) ? currencyFormat.format(Number(value)) : '—';
    }

    function percent(value) {
        if (!isFiniteValue(value)) return '—';
        const n = Number(value);
        return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
    }

    async function getJSON(url) {
        const response = await fetch(url, { credentials: 'same-origin' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Could not load index data.');
        return data;
    }

    function setText(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value == null ? '' : String(value);
    }

    function setChange(id, value) {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = percent(value);
        el.classList.toggle('stock-positive', isFiniteValue(value) && Number(value) >= 0);
        el.classList.toggle('stock-negative', isFiniteValue(value) && Number(value) < 0);
    }

    function buildRanges() {
        document.querySelectorAll('[data-index-ranges]').forEach(function (container) {
            container.replaceChildren();
            ranges.forEach(function (range) {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'stock-range' + (state.range === range ? ' active' : '');
                button.textContent = range;
                button.setAttribute('aria-pressed', String(state.range === range));
                button.addEventListener('click', function () {
                    if (state.range === range) return;
                    state.range = range;
                    state.viewStart = 0;
                    state.viewCount = 0;
                    state.fitted = false;
                    buildRanges();
                    loadHistory();
                });
                container.append(button);
            });
        });
    }

    function showChartMessage(message, isError) {
        document.querySelectorAll('.stock-chart-empty').forEach(function (element) {
            element.hidden = false;
            element.textContent = message;
            element.classList.toggle('stock-error', Boolean(isError));
        });
    }

    function clearChartMessages() {
        document.querySelectorAll('.stock-chart-empty').forEach(function (element) {
            element.hidden = true;
            element.textContent = '';
            element.classList.remove('stock-error');
        });
    }

    function drawLine(canvas, hoverIndex) {
        if (!canvas || state.selectedTab !== 'prices') return;
        const rect = canvas.getBoundingClientRect();
        if (rect.width < 5 || rect.height < 5 || !state.points.length) return;
        const ratio = window.devicePixelRatio || 1;
        canvas.width = Math.round(rect.width * ratio);
        canvas.height = Math.round(rect.height * ratio);
        const ctx = canvas.getContext('2d');
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.clearRect(0, 0, rect.width, rect.height);
        const styles = getComputedStyle(document.documentElement);
        const grid = styles.getPropertyValue('--chart-grid').trim() || '#eef1f5';
        const color = styles.getPropertyValue('--brand').trim() || '#1f6feb';
        const text = styles.getPropertyValue('--text-muted').trim() || '#8a93a5';
        const allPoints = state.points
            .filter(point => isFiniteValue(point.close))
            .map(point => ({ ...point, close: Number(point.close) }));
        const points = allPoints.slice(state.viewStart, state.viewStart + state.viewCount);
        const values = points.map(point => point.close);
        if (!values.length) return;
        let min = Math.min(...values);
        let max = Math.max(...values);
        if (min === max) { min *= 0.999; max *= 1.001; }
        const left = 64, right = 12, top = 14, bottom = 25;
        const width = rect.width - left - right;
        const height = rect.height - top - bottom;
        if (width <= 0 || height <= 0) return;
        const yFor = value => top + (max - value) / (max - min) * height;
        const xFor = index => left + index / Math.max(1, values.length - 1) * width;
        ctx.font = '11px Inter, sans-serif';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.strokeStyle = grid;
        ctx.fillStyle = text;
        for (let i = 0; i <= 4; i += 1) {
            const y = top + height * i / 4;
            const value = max - (max - min) * i / 4;
            ctx.beginPath();
            ctx.moveTo(left, y);
            ctx.lineTo(rect.width - right, y);
            ctx.stroke();
            ctx.fillText(numberFormat.format(value), left - 7, y);
        }
        ctx.beginPath();
        values.forEach(function (value, index) {
            const x = xFor(index);
            if (index === 0) ctx.moveTo(x, yFor(value));
            else ctx.lineTo(x, yFor(value));
        });
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.stroke();
        if (hoverIndex >= 0 && hoverIndex < values.length) {
            const x = xFor(hoverIndex);
            const y = yFor(values[hoverIndex]);
            ctx.save();
            ctx.strokeStyle = text;
            ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(x, top);
            ctx.lineTo(x, top + height);
            ctx.moveTo(left, y);
            ctx.lineTo(rect.width - right, y);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.arc(x, y, 3, 0, Math.PI * 2);
            ctx.fill();
            ctx.restore();
        }
        canvas._visiblePoints = points;
        canvas._allPoints = allPoints;
        canvas._lineGeometry = {
            left: left,
            right: right,
            top: top,
            width: width,
            height: height,
            gap: width / Math.max(1, values.length - 1),
            n: values.length
        };
    }

    function visibleCandles() {
        const total = state.points.length;
        if (!state.viewCount || state.viewCount > total) {
            state.viewCount = state.fitted ? total : Math.min(total, 120);
        }
        state.viewStart = Math.max(0, Math.min(
            state.viewStart,
            Math.max(0, total - state.viewCount)
        ));
        return state.candles.slice(state.viewStart, state.viewStart + state.viewCount);
    }

    function drawCandles(hoverIndex) {
        const canvas = document.querySelector('[data-index-candles]');
        if (!canvas || state.selectedTab !== 'chart') return;
        const empty = canvas.parentElement.querySelector('.stock-chart-empty');
        if (!state.candles.length || !window.OHLCChart) {
            if (empty) {
                empty.hidden = false;
                empty.textContent = state.candles.length
                    ? 'Candlestick renderer is unavailable. Reload the page to try again.'
                    : 'OHLC candle data is unavailable for this index and period.';
            }
            return;
        }
        if (empty) empty.hidden = true;
        const visible = visibleCandles();
        canvas._visibleCandles = visible;
        canvas._chartGeometry = window.OHLCChart.drawCandles(canvas, visible, { hoverIdx: hoverIndex });
    }

    function renderCharts(hoverIndex) {
        if (!state.points.length) return;
        clearChartMessages();
        visibleCandles();
        drawLine(document.querySelector('[data-index-line]'), hoverIndex == null ? -1 : hoverIndex);
        drawCandles(-1);
    }

    function dateFor(value) {
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value || '');
        return new Intl.DateTimeFormat('en-IN', {
            day: '2-digit',
            month: 'short',
            ...(state.range === '1D' || state.range === '1W'
                ? { hour: '2-digit', minute: '2-digit' }
                : { year: 'numeric' })
        }).format(date);
    }

    async function loadHistory() {
        const requestId = ++state.historyRequest;
        showChartMessage('Loading index history…', false);
        try {
            const data = await getJSON('/api/index/history?ticker=' + encodeURIComponent(ticker) + '&range=' + state.range);
            if (requestId !== state.historyRequest) return;
            state.points = data.points || [];
            state.candles = state.points.filter(point =>
                ['open', 'high', 'low', 'close'].every(key => isFiniteValue(point[key]))
            ).map(point => ({
                t: new Date(point.time).getTime(),
                o: Number(point.open),
                h: Number(point.high),
                l: Number(point.low),
                c: Number(point.close)
            }));
            state.viewStart = 0;
            state.viewCount = Math.min(state.points.length, 120);
            state.fitted = false;
            if (!state.points.length) {
                showChartMessage('Price history is unavailable from the configured market-data provider.', false);
                return;
            }
            const summary = data.summary || {};
            setText('indexDayHigh', price(summary.high));
            setText('indexDayLow', price(summary.low));
            setText('indexPeriodChange', percent(summary.change_pct));
            setText('indexChartCaption', state.range + ' price history · Data from Yahoo Finance');
            renderCharts();
        } catch (error) {
            if (requestId === state.historyRequest) showChartMessage(error.message, true);
        }
    }

    async function loadDetails() {
        try {
            const data = await getJSON('/api/index/details?ticker=' + encodeURIComponent(ticker));
            setText('indexName', data.index);
            setText('indexPrice', price(data.price));
            setText('indexSummaryPrice', price(data.price));
            setText('indexPrevClose', price(data.prev_close));
            setChange('indexChange', data.change_pct);
            setText('indexSession', data.is_open ? 'Market open' : 'Market closed');
            const chartSymbol = ticker === '^NSEI' ? 'NIFTY' : ticker.replace(/^\^/, '');
            document.getElementById('indexTradingView').href =
                'https://www.tradingview.com/chart/?symbol=' + encodeURIComponent('NSE:' + chartSymbol);
        } catch (error) {
            setText('indexSession', error.message);
        }
    }

    async function loadConstituents() {
        const body = document.getElementById('indexConstituents');
        try {
            const data = await getJSON('/api/index/constituents?ticker=' + encodeURIComponent(ticker));
            const rows = data.constituents || [];
            let advances = 0, declines = 0, unchanged = 0;
            rows.forEach(function (item) {
                if (!isFiniteValue(item.change_pct)) return;
                if (Number(item.change_pct) > 0.05) advances += 1;
                else if (Number(item.change_pct) < -0.05) declines += 1;
                else unchanged += 1;
            });
            setText('indexAdvances', advances);
            setText('indexDeclines', declines);
            setText('indexUnchanged', unchanged);
            setText('indexConstituentCount', rows.length);
            setText('indexConstituentAsOf', data.as_of ? 'As of ' + data.as_of : 'Source: ' + data.source);
            setText('indexWeightSource', 'Weights from ' + data.source);
            body.replaceChildren();
            rows.forEach(function (item) {
                const row = document.createElement('tr');
                const suffix = item.exchange === 'BSE' ? '.BO' : '.NS';
                const stockUrl = '/stock/' + encodeURIComponent(item.symbol) + suffix;
                const weight = isFiniteValue(item.weight) ? Number(item.weight) : NaN;
                const change = isFiniteValue(item.change_pct) ? Number(item.change_pct) : NaN;
                const impact = Number.isFinite(weight) && Number.isFinite(change)
                    ? weight * change / 100
                    : null;
                row.innerHTML = '<td><a href="' + escapeHTML(stockUrl) + '">' + escapeHTML(item.name) + '</a></td>' +
                    '<td>' + escapeHTML(item.symbol) + '</td>' +
                    '<td class="is-numeric">' + (Number.isFinite(weight) ? weight.toFixed(2) + '%' : '—') + '</td>' +
                    '<td class="is-numeric">' + price(item.price) + '</td>' +
                    '<td class="is-numeric ' + (change >= 0 ? 'stock-positive' : change < 0 ? 'stock-negative' : '') + '">' + percent(change) + '</td>' +
                    '<td class="is-numeric ' + (impact > 0 ? 'stock-positive' : impact < 0 ? 'stock-negative' : '') + '">' +
                    (Number.isFinite(impact) ? (impact > 0 ? '+' : '') + impact.toFixed(3) + ' pp' : '—') + '</td>' +
                    '<td class="is-numeric">' + (isFiniteValue(item.volume) ? compactFormat.format(Number(item.volume)) : '—') + '</td>';
                body.append(row);
            });
            if (!rows.length) appendMessageRow(body, 'No constituent data was returned.', 7);
        } catch (error) {
            appendMessageRow(body, error.message, 7);
            document.getElementById('indexWeightSource').textContent = 'Weights unavailable';
            setText('indexAdvances', '—');
            setText('indexDeclines', '—');
            setText('indexUnchanged', '—');
            setText('indexConstituentCount', '—');
        }
    }

    function appendMessageRow(body, message, columns) {
        body.replaceChildren();
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = columns;
        cell.className = 'ds-table-empty';
        cell.textContent = message;
        row.append(cell);
        body.append(row);
    }

    function setTab(tab) {
        state.selectedTab = tab.dataset.indexTab;
        document.querySelectorAll('[data-index-tab]').forEach(function (button) {
            const active = button === tab;
            button.classList.toggle('active', active);
            button.setAttribute('aria-selected', String(active));
            document.getElementById(button.getAttribute('aria-controls')).hidden = !active;
        });
        if (state.selectedTab === 'chart') drawCandles(-1);
        if (state.selectedTab === 'prices') drawLine(document.querySelector('[data-index-line]'));
    }

    function changeZoom(zoomIn, ratio) {
        if (!state.points.length) return;
        const oldCount = state.viewCount || Math.min(state.points.length, 120);
        const count = Math.max(10, Math.min(
            state.points.length,
            Math.round(oldCount * (zoomIn ? 0.8 : 1.25))
        ));
        if (count === oldCount) return;
        const anchorRatio = Math.max(0, Math.min(1, ratio == null ? 0.5 : ratio));
        const anchor = state.viewStart + Math.round(oldCount * anchorRatio);
        state.viewCount = count;
        state.viewStart = Math.round(anchor - count * anchorRatio);
        state.fitted = count === state.points.length;
        renderCharts(-1);
    }

    function fitChart() {
        state.viewStart = 0;
        state.viewCount = state.points.length;
        state.fitted = true;
        renderCharts(-1);
    }

    function attachChartInteractions() {
        const canvas = document.querySelector('[data-index-candles]');
        let drag = null;
        let hoverIndex = -1;
        const readout = document.querySelector('[data-index-readout]');
        function showCandle(candle) {
            if (!candle) {
                readout.textContent = 'Drag to pan · Scroll to zoom · Hover for OHLC';
                return;
            }
            const changePct = candle.o ? (candle.c - candle.o) / candle.o * 100 : 0;
            readout.textContent = dateFor(candle.t) + '  O ' + price(candle.o) +
                '  H ' + price(candle.h) + '  L ' + price(candle.l) +
                '  C ' + price(candle.c) + '  ' + percent(changePct);
        }
        canvas.addEventListener('pointerdown', function (event) {
            if (event.button !== 0 || !canvas._chartGeometry) return;
            drag = { id: event.pointerId, x: event.clientX, start: state.viewStart };
            canvas.setPointerCapture(event.pointerId);
        });
        canvas.addEventListener('pointermove', function (event) {
            const geometry = canvas._chartGeometry;
            const visible = canvas._visibleCandles;
            if (!geometry || !visible || !visible.length) return;
            if (drag && drag.id === event.pointerId) {
                const shift = Math.round((event.clientX - drag.x) / geometry.gap);
                state.viewStart = Math.max(0, Math.min(
                    Math.max(0, state.points.length - state.viewCount),
                    drag.start - shift
                ));
                drawCandles(-1);
                showCandle(null);
                return;
            }
            const x = event.clientX - canvas.getBoundingClientRect().left;
            if (x < geometry.padL || x > geometry.padL + geometry.w) return;
            const index = Math.max(0, Math.min(geometry.n - 1, Math.floor((x - geometry.padL) / geometry.gap)));
            if (index !== hoverIndex) {
                hoverIndex = index;
                drawCandles(hoverIndex);
            }
            showCandle(visible[index]);
        });
        function finishDrag(event) {
            if (drag && drag.id === event.pointerId) drag = null;
        }
        canvas.addEventListener('pointerup', finishDrag);
        canvas.addEventListener('pointercancel', finishDrag);
        canvas.addEventListener('pointerleave', function () {
            if (drag) return;
            hoverIndex = -1;
            drawCandles(-1);
            showCandle(null);
        });
        canvas.addEventListener('wheel', function (event) {
            const geometry = canvas._chartGeometry;
            if (!geometry) return;
            event.preventDefault();
            const x = event.clientX - canvas.getBoundingClientRect().left;
            changeZoom(event.deltaY < 0, (x - geometry.padL) / geometry.w);
        }, { passive: false });
        document.querySelectorAll('[data-index-zoom]').forEach(function (button) {
            button.addEventListener('click', function () {
                changeZoom(button.dataset.indexZoom === 'in');
            });
        });
        document.querySelectorAll('[data-index-fit]').forEach(button => button.addEventListener('click', fitChart));
    }

    function attachLineInteractions() {
        const canvas = document.querySelector('[data-index-line]');
        let drag = null;
        let hoverIndex = -1;
        const readout = document.querySelector('[data-index-line-readout]');
        function showPoint(point) {
            if (!point) {
                readout.textContent = 'Drag to pan · Scroll to zoom · Hover for index value';
                return;
            }
            readout.textContent = dateFor(point.time) + '  ' + price(point.close);
        }
        canvas.addEventListener('pointerdown', function (event) {
            if (event.button !== 0 || !canvas._lineGeometry) return;
            drag = { id: event.pointerId, x: event.clientX, start: state.viewStart };
            canvas.setPointerCapture(event.pointerId);
        });
        canvas.addEventListener('pointermove', function (event) {
            const geometry = canvas._lineGeometry;
            const points = canvas._visiblePoints;
            if (!geometry || !points || !points.length) return;
            if (drag && drag.id === event.pointerId) {
                const shift = Math.round((event.clientX - drag.x) / geometry.gap);
                state.viewStart = Math.max(0, Math.min(
                    Math.max(0, canvas._allPoints.length - state.viewCount),
                    drag.start - shift
                ));
                renderCharts(-1);
                showPoint(null);
                return;
            }
            const x = event.clientX - canvas.getBoundingClientRect().left;
            if (x < geometry.left || x > geometry.left + geometry.width) return;
            const index = Math.max(0, Math.min(
                geometry.n - 1,
                Math.round((x - geometry.left) / geometry.gap)
            ));
            if (index !== hoverIndex) {
                hoverIndex = index;
                drawLine(canvas, hoverIndex);
            }
            showPoint(points[index]);
        });
        function finishDrag(event) {
            if (drag && drag.id === event.pointerId) drag = null;
        }
        canvas.addEventListener('pointerup', finishDrag);
        canvas.addEventListener('pointercancel', finishDrag);
        canvas.addEventListener('pointerleave', function () {
            if (drag) return;
            hoverIndex = -1;
            drawLine(canvas, -1);
            showPoint(null);
        });
        canvas.addEventListener('wheel', function (event) {
            const geometry = canvas._lineGeometry;
            if (!geometry) return;
            event.preventDefault();
            const x = event.clientX - canvas.getBoundingClientRect().left;
            changeZoom(event.deltaY < 0, (x - geometry.left) / geometry.width);
        }, { passive: false });
    }

    document.querySelectorAll('[data-index-tab]').forEach(function (tab) {
        tab.addEventListener('click', function () { setTab(tab); });
        tab.addEventListener('keydown', function (event) {
            if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
            event.preventDefault();
            const tabs = Array.from(document.querySelectorAll('[data-index-tab]'));
            const offset = event.key === 'ArrowRight' ? 1 : -1;
            const next = tabs[(tabs.indexOf(tab) + offset + tabs.length) % tabs.length];
            next.focus();
            next.click();
        });
    });
    buildRanges();
    attachChartInteractions();
    attachLineInteractions();
    loadDetails();
    loadHistory();
    loadConstituents();
    window.addEventListener('resize', renderCharts);
})();
