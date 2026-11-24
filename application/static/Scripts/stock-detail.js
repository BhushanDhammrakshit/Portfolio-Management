(function () {
    'use strict';

    const root = document.getElementById('stockDetail');
    if (!root) return;

    const symbol = root.dataset.symbol;
    const exchange = root.dataset.exchange || 'NSE';
    const apiSymbol = symbol + (exchange === 'BSE' ? '.BO' : '.NS');
    const ranges = ['1D', '1W', '1M', '3M', '6M', '1Y', '5Y'];
    const state = {
        range: '1D',
        points: [],
        summary: null,
        updates: null,
        loaded: new Set(),
        chartRequest: 0,
        chartView: { start: 0, count: 0, fitted: false }
    };

    const currency = new Intl.NumberFormat('en-IN', {
        style: 'currency',
        currency: 'INR',
        maximumFractionDigits: 2
    });
    const compact = new Intl.NumberFormat('en-IN', {
        notation: 'compact',
        maximumFractionDigits: 2
    });

    function formatPrice(value) {
        return isFiniteNumber(value) ? currency.format(value) : '—';
    }

    function formatNumber(value) {
        return isFiniteNumber(value) ? Number(value).toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—';
    }

    function isFiniteNumber(value) {
        return value !== null && value !== '' && Number.isFinite(Number(value));
    }

    function formatPercent(value) {
        return isFiniteNumber(value) ? Number(value).toFixed(2) + '%' : '—';
    }

    function displayDate(value) {
        if (!value) return 'Date unavailable';
        const date = new Date(value);
        return Number.isNaN(date.getTime())
            ? String(value)
            : new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
    }

    function setText(id, value) {
        const element = document.getElementById(id);
        if (element) element.textContent = value == null ? '' : String(value);
    }

    async function getJson(url) {
        const response = await fetch(url, { credentials: 'same-origin' });
        let payload = {};
        try {
            payload = await response.json();
        } catch (e) {
            if (response.ok) throw new Error('The server returned an unreadable response.');
        }
        if (!response.ok) {
            if (response.status === 402 || payload.error === 'upgrade_required') {
                throw new Error('This data is available on an upgraded plan.');
            }
            throw new Error(payload.detail || payload.error || 'Could not load stock data.');
        }
        return payload;
    }

    function setChange(value, element) {
        element.classList.remove('stock-positive', 'stock-negative');
        if (!isFiniteNumber(value)) {
            element.textContent = '';
            return;
        }
        const number = Number(value);
        element.textContent = (number > 0 ? '+' : '') + number.toFixed(2) + '%';
        element.classList.add(number >= 0 ? 'stock-positive' : 'stock-negative');
    }

    function buildRangeControls() {
        document.querySelectorAll('[data-range-controls]').forEach(function (container) {
            container.replaceChildren();
            ranges.forEach(function (range) {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'stock-range' + (range === state.range ? ' active' : '');
                button.dataset.range = range;
                button.setAttribute('aria-pressed', String(range === state.range));
                button.textContent = range;
                button.addEventListener('click', function () {
                    if (state.range === range) return;
                    state.range = range;
                    buildRangeControls();
                    loadHistory();
                });
                container.append(button);
            });
        });
    }

    function setChartMessage(message, isError) {
        document.querySelectorAll('.stock-chart-empty').forEach(function (node) {
            node.hidden = false;
            node.textContent = message;
            node.classList.toggle('stock-error', Boolean(isError));
        });
    }

    function clearChartMessage() {
        document.querySelectorAll('.stock-chart-empty').forEach(function (node) {
            node.hidden = true;
            node.classList.remove('stock-error');
            node.textContent = '';
        });
    }

    function drawCharts() {
        const points = state.points;
        if (!points.length) return;
        clearChartMessage();
        document.querySelectorAll('[data-price-chart]').forEach(function (canvas) {
            drawPrice(canvas, points);
        });
        document.querySelectorAll('[data-candle-chart]').forEach(function (canvas) {
            if (canvas.closest('.stock-panel').hidden) return;
            canvas._candleGeometry = drawCandlestick(canvas, points, -1);
        });
        document.querySelectorAll('[data-volume-chart]').forEach(function (canvas) {
            drawVolume(canvas, points);
        });
        document.querySelectorAll('[data-candle-volume-chart]').forEach(function (canvas) {
            const candles = canvas.closest('.stock-card').querySelector('[data-candle-chart]')._candles || [];
            drawVolume(canvas, candles.map(function (candle) { return { volume: candle.v }; }));
        });
    }

    function toCandles(points) {
        return points
            .filter(function (point) {
                return ['open', 'high', 'low', 'close'].every(function (key) {
                    return isFiniteNumber(point[key]);
                });
            })
            .map(function (point) {
                return {
                    t: new Date(point.time).getTime(),
                    o: Number(point.open),
                    h: Number(point.high),
                    l: Number(point.low),
                    c: Number(point.close),
                    v: Number(point.volume) || 0
                };
            });
    }

    function drawCandlestick(canvas, points, hoverIdx) {
        const allCandles = toCandles(points);
        if (!state.chartView.count || state.chartView.count > allCandles.length) {
            state.chartView.count = state.chartView.fitted
                ? allCandles.length
                : Math.min(allCandles.length, 120);
        }
        state.chartView.start = Math.max(0, Math.min(
            state.chartView.start,
            Math.max(0, allCandles.length - state.chartView.count)
        ));
        const candles = allCandles.slice(
            state.chartView.start,
            state.chartView.start + state.chartView.count
        );
        const empty = canvas.parentElement.querySelector('.stock-chart-empty');
        if (!candles.length || !window.OHLCChart) {
            if (empty) {
                empty.hidden = false;
                empty.textContent = candles.length
                    ? 'Candlestick chart is unavailable.'
                    : 'OHLC candle data is not available for this period.';
            }
            return null;
        }
        if (empty) empty.hidden = true;
        const geometry = window.OHLCChart.drawCandles(canvas, candles, { hoverIdx: hoverIdx });
        canvas._candles = candles;
        canvas._allCandles = allCandles;
        return geometry;
    }

    function renderCandleChart(hoverIdx) {
        const canvas = document.querySelector('[data-candle-chart]');
        if (!canvas || canvas.closest('.stock-panel').hidden) return;
        canvas._candleGeometry = drawCandlestick(canvas, state.points, hoverIdx == null ? -1 : hoverIdx);
        const volume = document.querySelector('[data-candle-volume-chart]');
        if (volume) {
            drawVolume(volume, (canvas._candles || []).map(function (candle) {
                return { volume: candle.v };
            }));
        }
    }

    function changeChartZoom(direction, anchorRatio) {
        const canvas = document.querySelector('[data-candle-chart]');
        const allCandles = canvas && canvas._allCandles;
        if (!allCandles || !allCandles.length) return;
        const oldCount = state.chartView.count;
        const newCount = Math.max(10, Math.min(
            allCandles.length,
            Math.round(oldCount * (direction > 0 ? 0.8 : 1.25))
        ));
        if (newCount === oldCount) return;
        const ratio = Math.max(0, Math.min(1, anchorRatio == null ? 0.5 : anchorRatio));
        const anchor = state.chartView.start + Math.round(oldCount * ratio);
        state.chartView.count = newCount;
        state.chartView.start = Math.round(anchor - newCount * ratio);
        state.chartView.fitted = newCount === allCandles.length;
        renderCandleChart(-1);
    }

    function fitChartToRange() {
        const canvas = document.querySelector('[data-candle-chart]');
        const allCandles = canvas && canvas._allCandles;
        if (!allCandles || !allCandles.length) return;
        state.chartView = { start: 0, count: allCandles.length, fitted: true };
        renderCandleChart(-1);
    }

    function prepareCanvas(canvas) {
        const rect = canvas.getBoundingClientRect();
        if (rect.width < 5 || rect.height < 5) return null;
        const ratio = window.devicePixelRatio || 1;
        canvas.width = Math.round(rect.width * ratio);
        canvas.height = Math.round(rect.height * ratio);
        const context = canvas.getContext('2d');
        context.setTransform(ratio, 0, 0, ratio, 0, 0);
        return { context: context, width: rect.width, height: rect.height };
    }

    function chartColors() {
        const styles = getComputedStyle(document.documentElement);
        return {
            text: styles.getPropertyValue('--text-muted').trim() || '#8A93A5',
            grid: styles.getPropertyValue('--chart-grid').trim() || '#EEF1F5',
            line: styles.getPropertyValue('--brand').trim() || '#1F6FEB',
            fill: styles.getPropertyValue('--brand-soft').trim() || '#EAF2FE',
            volume: styles.getPropertyValue('--chart-3').trim() || '#93B7F5'
        };
    }

    function drawPrice(canvas, points) {
        const prepared = prepareCanvas(canvas);
        if (!prepared) return;
        const ctx = prepared.context;
        const width = prepared.width;
        const height = prepared.height;
        const colors = chartColors();
        const left = 58;
        const right = 12;
        const top = 14;
        const bottom = 34;
        const plotWidth = width - left - right;
        const plotHeight = height - top - bottom;
        const values = points.map(function (point) { return Number(point.close); }).filter(Number.isFinite);
        if (!values.length || plotWidth <= 0 || plotHeight <= 0) return;
        let min = Math.min.apply(null, values);
        let max = Math.max.apply(null, values);
        const pad = (max - min) * 0.12 || Math.max(Math.abs(max) * 0.002, 0.1);
        min -= pad;
        max += pad;
        const yFor = function (value) { return top + (max - value) / (max - min) * plotHeight; };
        const xFor = function (index) {
            return left + (points.length <= 1 ? 0 : index / (points.length - 1) * plotWidth);
        };

        ctx.clearRect(0, 0, width, height);
        ctx.font = '11px Inter, sans-serif';
        ctx.textBaseline = 'middle';
        ctx.lineWidth = 1;
        for (let i = 0; i <= 4; i += 1) {
            const y = top + i * plotHeight / 4;
            const labelValue = max - i * (max - min) / 4;
            ctx.strokeStyle = colors.grid;
            ctx.beginPath();
            ctx.moveTo(left, y);
            ctx.lineTo(width - right, y);
            ctx.stroke();
            ctx.fillStyle = colors.text;
            ctx.textAlign = 'right';
            ctx.fillText(labelValue.toLocaleString('en-IN', { maximumFractionDigits: 2 }), left - 8, y);
        }

        ctx.beginPath();
        values.forEach(function (value, index) {
            const x = xFor(index);
            const y = yFor(value);
            if (index === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.lineTo(xFor(values.length - 1), top + plotHeight);
        ctx.lineTo(xFor(0), top + plotHeight);
        ctx.closePath();
        const gradient = ctx.createLinearGradient(0, top, 0, top + plotHeight);
        gradient.addColorStop(0, colors.fill);
        gradient.addColorStop(1, 'rgba(255,255,255,0)');
        ctx.fillStyle = gradient;
        ctx.fill();

        ctx.beginPath();
        values.forEach(function (value, index) {
            const x = xFor(index);
            const y = yFor(value);
            if (index === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.strokeStyle = colors.line;
        ctx.lineWidth = 2;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.stroke();

        ctx.fillStyle = colors.text;
        ctx.textAlign = 'left';
        ctx.textBaseline = 'alphabetic';
        ctx.fillText(displayDate(points[0].time).split(',')[0], left, height - 7);
        ctx.textAlign = 'right';
        ctx.fillText(displayDate(points[points.length - 1].time).split(',')[0], width - right, height - 7);
        canvas._chartPoints = points;
        canvas._chartBounds = { left: left, right: right, top: top, bottom: bottom };
    }

    function drawVolume(canvas, points) {
        const prepared = prepareCanvas(canvas);
        if (!prepared) return;
        const ctx = prepared.context;
        const width = prepared.width;
        const height = prepared.height;
        const volumes = points.map(function (point) { return Number(point.volume) || 0; });
        const max = Math.max.apply(null, volumes);
        const colors = chartColors();
        ctx.clearRect(0, 0, width, height);
        if (!max) return;
        const barWidth = Math.max(1, width / volumes.length * 0.65);
        volumes.forEach(function (volume, index) {
            const x = index / Math.max(volumes.length, 1) * width;
            const barHeight = volume / max * (height - 5);
            ctx.fillStyle = colors.volume;
            ctx.fillRect(x, height - barHeight, barWidth, barHeight);
        });
    }

    function attachChartTooltips() {
        document.querySelectorAll('[data-price-chart]').forEach(function (canvas) {
            canvas.addEventListener('pointermove', function (event) {
                const points = canvas._chartPoints;
                const bounds = canvas._chartBounds;
                if (!points || !bounds || !points.length) return;
                const rect = canvas.getBoundingClientRect();
                const x = event.clientX - rect.left;
                const ratio = Math.max(0, Math.min(1, (x - bounds.left) / (rect.width - bounds.left - bounds.right)));
                const index = Math.round(ratio * (points.length - 1));
                const point = points[index];
                const tooltip = canvas.parentElement.querySelector('.stock-chart-tooltip');
                if (!tooltip) return;
                tooltip.textContent = displayDate(point.time) + ' · ' + formatPrice(point.close);
                tooltip.style.left = Math.min(Math.max(4, x + 12), rect.width - 225) + 'px';
                tooltip.style.top = '12px';
                tooltip.hidden = false;
            });
            canvas.addEventListener('pointerleave', function () {
                const tooltip = canvas.parentElement.querySelector('.stock-chart-tooltip');
                if (tooltip) tooltip.hidden = true;
            });
        });
        document.querySelectorAll('[data-candle-chart]').forEach(function (canvas) {
            let hoverIdx = -1;
            let drag = null;
            function setReadout(candle) {
                const readout = document.querySelector('[data-candle-readout]');
                if (!readout) return;
                if (!candle) {
                    readout.textContent = 'Drag to pan · Scroll to zoom · Hover for OHLC';
                    return;
                }
                const changePct = candle.o ? (candle.c - candle.o) / candle.o * 100 : 0;
                const timeOptions = ['1D', '1W'].includes(state.range)
                    ? { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }
                    : { day: '2-digit', month: 'short', year: 'numeric' };
                const time = new Intl.DateTimeFormat('en-IN', timeOptions).format(new Date(candle.t));
                readout.textContent = time + '  O ' + formatPrice(candle.o) +
                    '  H ' + formatPrice(candle.h) + '  L ' + formatPrice(candle.l) +
                    '  C ' + formatPrice(candle.c) + '  ' +
                    (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + '%';
            }
            canvas.addEventListener('pointerdown', function (event) {
                if (event.button !== 0 || !canvas._candleGeometry) return;
                drag = {
                    pointerId: event.pointerId,
                    x: event.clientX,
                    start: state.chartView.start
                };
                canvas.setPointerCapture(event.pointerId);
            });
            canvas.addEventListener('pointermove', function (event) {
                const candles = canvas._candles;
                if (!candles || !candles.length) return;
                const rect = canvas.getBoundingClientRect();
                const geometry = canvas._candleGeometry;
                if (!geometry) return;
                if (drag && drag.pointerId === event.pointerId) {
                    const shift = Math.round((event.clientX - drag.x) / geometry.gap);
                    state.chartView.start = Math.max(0, Math.min(
                        Math.max(0, canvas._allCandles.length - state.chartView.count),
                        drag.start - shift
                    ));
                    renderCandleChart(-1);
                    setReadout(null);
                    return;
                }
                const x = event.clientX - rect.left;
                if (x < geometry.padL || x > geometry.padL + geometry.w) return;
                const index = Math.max(0, Math.min(geometry.n - 1, Math.floor((x - geometry.padL) / geometry.gap)));
                if (index !== hoverIdx) {
                    hoverIdx = index;
                    renderCandleChart(hoverIdx);
                }
                const candle = candles[index];
                setReadout(candle);
            });
            function finishDrag(event) {
                if (drag && drag.pointerId === event.pointerId) drag = null;
            }
            canvas.addEventListener('pointerup', finishDrag);
            canvas.addEventListener('pointercancel', finishDrag);
            canvas.addEventListener('wheel', function (event) {
                const geometry = canvas._candleGeometry;
                if (!geometry) return;
                event.preventDefault();
                const rect = canvas.getBoundingClientRect();
                const ratio = (event.clientX - rect.left - geometry.padL) / geometry.w;
                changeChartZoom(event.deltaY < 0 ? 1 : -1, ratio);
            }, { passive: false });
            canvas.addEventListener('pointerleave', function () {
                if (drag) return;
                hoverIdx = -1;
                renderCandleChart(-1);
                setReadout(null);
            });
        });
        document.querySelectorAll('[data-chart-zoom]').forEach(function (button) {
            button.addEventListener('click', function () {
                changeChartZoom(button.dataset.chartZoom === 'in' ? 1 : -1);
            });
        });
        document.querySelectorAll('[data-chart-reset]').forEach(function (button) {
            button.addEventListener('click', fitChartToRange);
        });
    }

    async function loadHistory() {
        const requestId = ++state.chartRequest;
        setChartMessage('Loading price history…', false);
        try {
            const data = await getJson('/api/stock/history?symbol=' + encodeURIComponent(apiSymbol) + '&range=' + encodeURIComponent(state.range));
            if (requestId !== state.chartRequest) return;
            state.points = data.points || [];
            state.summary = data.summary || null;
            state.chartView = { start: 0, count: 0, fitted: false };
            if (!state.points.length) {
                setChartMessage('Price history is not available from the configured market-data provider.', false);
                return;
            }
            setText('priceChartCaption', state.range + ' price history');
            setText('periodChangeLabel', state.range + ' change');
            const latest = state.points[state.points.length - 1].close;
            setText('stockPrice', formatPrice(latest));
            setText('priceSummary', formatPrice(latest));
            if (state.summary) {
                setText('periodLow', formatPrice(state.summary.low));
                setText('periodHigh', formatPrice(state.summary.high));
                setText('periodVolume', compact.format(state.summary.volume || 0));
                setText('periodChange', (Number(state.summary.change) > 0 ? '+' : '') + formatPrice(state.summary.change));
                setChange(state.summary.change_pct, document.getElementById('stockChange'));
                const priceSummary = document.getElementById('periodChange');
                priceSummary.classList.toggle('stock-positive', Number(state.summary.change) >= 0);
                priceSummary.classList.toggle('stock-negative', Number(state.summary.change) < 0);
            }
            drawCharts();
        } catch (error) {
            if (requestId === state.chartRequest) setChartMessage(error.message, true);
        }
    }

    async function loadInfo() {
        try {
            const info = await getJson('/api/stock/info?symbol=' + encodeURIComponent(apiSymbol));
            setText('stockName', info.name || symbol);
            setText('stockExchange', info.exchange || exchange);
            setText('stockSymbol', info.symbol || symbol);
            setText('stockSector', info.sector || '');
            if (isFiniteNumber(info.price) && !state.points.length) {
                setText('stockPrice', formatPrice(info.price));
                setText('priceSummary', formatPrice(info.price));
            }
        } catch (error) {
            setText('stockSector', error.message);
        }
    }

    function appendEmptyRow(body, columns, message, className) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = columns;
        cell.className = className || 'ds-table-empty';
        cell.textContent = message;
        row.append(cell);
        body.append(row);
    }

    function fundamentalsTable(category, metrics, items) {
        const card = document.createElement('section');
        card.className = 'stock-table-card stock-fundamentals-card';
        const heading = document.createElement('h3');
        heading.textContent = category;
        const scroll = document.createElement('div');
        scroll.className = 'stock-table-scroll';
        const table = document.createElement('table');
        table.className = 'ds-table stock-table';
        table.setAttribute('aria-label', category + ' fundamentals');
        const head = document.createElement('thead');
        const header = document.createElement('tr');
        ['Metric', 'Value'].forEach(function (label, index) {
            const cell = document.createElement('th');
            cell.textContent = label;
            if (index === 1) cell.className = 'is-numeric';
            header.append(cell);
        });
        head.append(header);
        const body = document.createElement('tbody');
        items.forEach(function (item) {
            const path = item[1].split('.');
            const value = (metrics[path[0]] || {})[path[1]];
            const row = document.createElement('tr');
            const labelCell = document.createElement('td');
            labelCell.textContent = item[0];
            const valueCell = document.createElement('td');
            valueCell.className = 'is-numeric';
            valueCell.textContent = value == null ? '—' : item[2](value);
            row.append(labelCell, valueCell);
            body.append(row);
        });
        table.append(head, body);
        scroll.append(table);
        card.append(heading, scroll);
        return card;
    }

    let fundamentalsLayoutFrame = 0;
    function layoutFundamentalsTables() {
        if (fundamentalsLayoutFrame) cancelAnimationFrame(fundamentalsLayoutFrame);
        fundamentalsLayoutFrame = requestAnimationFrame(function () {
            fundamentalsLayoutFrame = 0;
            const grid = document.getElementById('fundamentalsGrid');
            const styles = getComputedStyle(grid);
            const rowHeight = parseFloat(styles.gridAutoRows);
            const rowGap = parseFloat(styles.rowGap);
            if (!rowHeight || !Number.isFinite(rowHeight)) return;
            grid.querySelectorAll('.stock-fundamentals-card').forEach(function (card) {
                card.style.gridRowEnd = 'auto';
            });
            grid.querySelectorAll('.stock-fundamentals-card').forEach(function (card) {
                const rows = Math.ceil((card.getBoundingClientRect().height + rowGap) / (rowHeight + rowGap));
                card.style.gridRowEnd = 'span ' + Math.max(1, rows);
            });
        });
    }

    async function loadFundamentals() {
        const grid = document.getElementById('fundamentalsGrid');
        grid.replaceChildren();
        const loading = fundamentalsTable('Fundamentals', {}, []);
        appendEmptyRow(loading.querySelector('tbody'), 2, 'Loading fundamentals…');
        grid.append(loading);
        layoutFundamentalsTables();
        try {
            const data = await getJson('/api/fundamentals/' + encodeURIComponent(apiSymbol));
            grid.replaceChildren();
            const payload = data.fundamentals || {};
            const metrics = payload.metrics || {};
            const groups = [
                ['Valuation', [
                    ['Trailing P/E', 'valuation.pe_trailing', formatNumber],
                    ['Forward P/E', 'valuation.pe_forward', formatNumber],
                    ['Price / book', 'valuation.pb', formatNumber],
                    ['EV / EBITDA', 'valuation.ev_ebitda', formatNumber],
                    ['Dividend yield', 'valuation.div_yield_pct', formatPercent],
                    ['Market cap', 'valuation.market_cap', function (v) { return '₹' + compact.format(v); }]
                ]],
                ['Profitability', [
                    ['ROE', 'profitability.roe_pct', formatPercent],
                    ['ROA', 'profitability.roa_pct', formatPercent],
                    ['ROCE', 'profitability.roce_pct', formatPercent],
                    ['Operating margin', 'profitability.operating_margin_pct', formatPercent],
                    ['Net margin', 'profitability.net_margin_pct', formatPercent]
                ]],
                ['Growth', [
                    ['Revenue growth (YoY)', 'growth.revenue_yoy_pct', formatPercent],
                    ['Revenue CAGR (3Y)', 'growth.revenue_cagr_3y_pct', formatPercent],
                    ['EPS growth (YoY)', 'growth.eps_yoy_pct', formatPercent],
                    ['Earnings growth (QoQ)', 'growth.earnings_qoq_pct', formatPercent]
                ]],
                ['Financial health', [
                    ['Debt / equity', 'financial_health.de', formatNumber],
                    ['Interest coverage', 'financial_health.interest_coverage', formatNumber],
                    ['Current ratio', 'financial_health.current_ratio', formatNumber],
                    ['Free cash flow', 'financial_health.fcf', function (v) { return '₹' + compact.format(v); }]
                ]],
                ['Ownership', [
                    ['Insider holding', 'ownership.insider_holding_pct', formatPercent],
                    ['Institutional holding', 'ownership.institutional_holding_pct', formatPercent]
                ]]
            ];
            groups.forEach(function (group) {
                grid.append(fundamentalsTable(group[0], metrics, group[1]));
            });
            layoutFundamentalsTables();
            setText('fundamentalsAsOf', payload.as_of ? 'As of ' + payload.as_of : '');
            state.loaded.add('fundamentals');
        } catch (error) {
            grid.replaceChildren();
            const card = fundamentalsTable('Fundamentals', {}, []);
            appendEmptyRow(card.querySelector('tbody'), 2, error.message, 'ds-table-empty stock-error');
            grid.append(card);
            layoutFundamentalsTables();
        }
    }

    function renderUpdates(type) {
        const container = document.getElementById('stockUpdates');
        container.replaceChildren();
        const updates = state.updates && state.updates[type] || [];
        if (!updates.length) {
            const message = type === 'actions'
                ? 'No corporate actions found in the available data.'
                : 'No company updates are available in the indexed feeds.';
            appendEmptyRow(container, 4, message);
            return;
        }
        updates.forEach(function (item) {
            const safeUrl = (function () {
                try {
                    const url = new URL(item.url);
                    return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
                } catch (e) {
                    return '';
                }
            })();
            const row = document.createElement('tr');
            const dateCell = document.createElement('td');
            dateCell.textContent = displayDate(item.published_at || item.date);
            const typeCell = document.createElement('td');
            typeCell.textContent = item.type || item.source || 'Update';
            const updateCell = document.createElement('td');
            const title = document.createElement(safeUrl ? 'a' : 'span');
            title.textContent = item.title || item.type || 'Company update';
            if (safeUrl) {
                title.href = safeUrl;
                title.target = '_blank';
                title.rel = 'noopener noreferrer';
            }
            if (item.content) {
                const content = document.createElement('div');
                content.className = 'stock-update-content';
                content.textContent = item.content;
                updateCell.append(title, content);
            } else {
                updateCell.append(title);
            }
            if (item.value != null) {
                const value = document.createElement('div');
                value.className = 'stock-update-content';
                value.textContent = 'Value: ' + formatNumber(item.value);
                updateCell.append(value);
            }
            const sourceCell = document.createElement('td');
            sourceCell.textContent = item.source || '—';
            row.append(dateCell, typeCell, updateCell, sourceCell);
            container.append(row);
        });
    }

    async function loadUpdates() {
        const container = document.getElementById('stockUpdates');
        container.replaceChildren();
        appendEmptyRow(container, 4, 'Loading company updates…');
        try {
            state.updates = await getJson('/api/stock/updates?symbol=' + encodeURIComponent(apiSymbol));
            const activeTab = document.querySelector('[data-update-type].active');
            renderUpdates(activeTab ? activeTab.dataset.updateType : 'news');
            state.loaded.add('updates');
        } catch (error) {
            container.replaceChildren();
            appendEmptyRow(container, 4, error.message, 'ds-table-empty stock-error');
        }
    }

    function renderShareholders(data) {
        const pattern = document.getElementById('shareholdingPattern');
        pattern.replaceChildren();
        (data.pattern || []).forEach(function (item) {
            const row = document.createElement('tr');
            const category = document.createElement('td');
            category.textContent = item.category;
            const holding = document.createElement('td');
            holding.className = 'is-numeric';
            holding.textContent = formatPercent(item.holding_pct);
            row.append(category, holding);
            pattern.append(row);
        });
        if (!(data.pattern || []).length) {
            appendEmptyRow(pattern, 2, 'Ownership data is not available.');
        }
        setText('shareholdingAsOf', data.as_of ? 'As of ' + data.as_of : '');

        const head = document.getElementById('holdersHead');
        const body = document.getElementById('holdersBody');
        head.replaceChildren();
        body.replaceChildren();
        const holders = data.holders || [];
        if (!holders.length) {
            appendEmptyRow(body, 5, 'No named institutional or mutual fund holders are available from the current provider.');
            return;
        }
        const keys = Object.keys(holders[0]);
        const header = document.createElement('tr');
        keys.forEach(function (key) {
            const cell = document.createElement('th');
            cell.textContent = key.replace(/_/g, ' ');
            header.append(cell);
        });
        head.append(header);
        holders.forEach(function (holder) {
            const row = document.createElement('tr');
            keys.forEach(function (key) {
                const cell = document.createElement('td');
                const value = holder[key];
                cell.textContent = value == null ? '—' : (typeof value === 'number' ? formatNumber(value) : String(value));
                row.append(cell);
            });
            body.append(row);
        });
    }

    async function loadShareholding() {
        const pattern = document.getElementById('shareholdingPattern');
        pattern.textContent = 'Loading shareholding…';
        try {
            const data = await getJson('/api/investing-tools/shareholding?symbol=' + encodeURIComponent(apiSymbol));
            if (data.error) throw new Error(data.error);
            renderShareholders(data);
            state.loaded.add('shareholding');
        } catch (error) {
            pattern.replaceChildren();
            appendEmptyRow(pattern, 2, error.message, 'ds-table-empty stock-error');
            document.getElementById('holdersBody').replaceChildren();
            appendEmptyRow(document.getElementById('holdersBody'), 5, 'Shareholder data could not be loaded.');
        }
    }

    async function loadOi() {
        const summary = document.getElementById('oiSummary');
        const body = document.getElementById('oiRows');
        summary.replaceChildren();
        appendEmptyRow(summary, 4, 'Loading open interest…');
        try {
            const data = await getJson('/api/stock/oi?symbol=' + encodeURIComponent(apiSymbol));
            summary.replaceChildren();
            body.replaceChildren();
            if (!data.available) {
                appendEmptyRow(summary, 4, data.message || 'Open interest is unavailable.');
                appendEmptyRow(body, 5, 'No option-chain data available.');
                state.loaded.add('oi');
                return;
            }
            const summaryRow = document.createElement('tr');
            [formatPrice(data.spot), formatNumber(data.pcr), formatPrice(data.support), formatPrice(data.resistance)].forEach(function (value) {
                const cell = document.createElement('td');
                cell.className = 'is-numeric';
                cell.textContent = value;
                summaryRow.append(cell);
            });
            summary.append(summaryRow);
            setText('oiExpiry', data.expiry ? 'Expiry ' + data.expiry : '');
            const rows = data.rows || [];
            if (!rows.length) {
                appendEmptyRow(body, 5, 'No strike-level open interest is available.');
            } else {
                rows.forEach(function (item) {
                    const row = document.createElement('tr');
                    [
                        item.strike,
                        (item.ce || {}).oi,
                        (item.ce || {}).oi_chg,
                        (item.pe || {}).oi,
                        (item.pe || {}).oi_chg
                    ].forEach(function (value, index) {
                        const cell = document.createElement('td');
                        cell.textContent = index === 0 ? formatPrice(value) : formatNumber(value);
                        row.append(cell);
                    });
                    body.append(row);
                });
            }
            state.loaded.add('oi');
        } catch (error) {
            summary.replaceChildren();
            appendEmptyRow(summary, 4, error.message, 'ds-table-empty stock-error');
            body.replaceChildren();
            appendEmptyRow(body, 5, 'Option-chain data could not be loaded.');
        }
    }

    function selectTab(tab) {
        document.querySelectorAll('.stock-tab').forEach(function (button) {
            const active = button === tab;
            button.classList.toggle('active', active);
            button.setAttribute('aria-selected', String(active));
            const panel = document.getElementById(button.getAttribute('aria-controls'));
            if (panel) panel.hidden = !active;
        });
        const name = tab.dataset.tab;
        if (name === 'charts') drawCharts();
        if (!state.loaded.has(name)) {
            if (name === 'updates') loadUpdates();
            else if (name === 'fundamentals') loadFundamentals();
            else if (name === 'shareholding') loadShareholding();
            else if (name === 'oi') loadOi();
            else state.loaded.add(name);
        }
    }

    document.querySelectorAll('.stock-tab').forEach(function (tab) {
        tab.addEventListener('click', function () { selectTab(tab); });
        tab.addEventListener('keydown', function (event) {
            if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
            event.preventDefault();
            const tabs = Array.from(document.querySelectorAll('.stock-tab'));
            const offset = event.key === 'ArrowRight' ? 1 : -1;
            const next = tabs[(tabs.indexOf(tab) + offset + tabs.length) % tabs.length];
            next.focus();
            selectTab(next);
        });
    });

    document.querySelectorAll('[data-update-type]').forEach(function (button) {
        button.addEventListener('click', function () {
            document.querySelectorAll('[data-update-type]').forEach(function (tab) {
                const active = tab === button;
                tab.classList.toggle('active', active);
                tab.setAttribute('aria-selected', String(active));
            });
            if (!state.updates) loadUpdates();
            else renderUpdates(button.dataset.updateType);
        });
    });

    buildRangeControls();
    attachChartTooltips();
    loadInfo();
    loadHistory();
    window.addEventListener('resize', function () {
        window.requestAnimationFrame(drawCharts);
        layoutFundamentalsTables();
    });
})();
