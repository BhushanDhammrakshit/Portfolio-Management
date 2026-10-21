/* financeCandle — global Chart.js defaults.
   Chart.js is loaded per page (sometimes after async data fetches), so poll
   briefly for it and apply the flat, tabular theme once. */
(function () {
    var tries = 0;
    var timer = null;

    function cssVar(name) {
        var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        return value;
    }

    function apply() {
        if (!window.Chart) return false;
        var Chart = window.Chart;
        var text = cssVar('--text-primary');
        var muted = cssVar('--text-muted');
        var grid = cssVar('--chart-grid');
        var brand = cssVar('--brand');
        var positive = cssVar('--positive');
        var negative = cssVar('--negative');
        var tooltipBg = cssVar('--tooltip-bg');
        var tooltipFg = cssVar('--tooltip-fg');

        Chart.defaults.font.family = "'Inter', 'Segoe UI', Roboto, Arial, sans-serif";
        Chart.defaults.font.size = 11;
        Chart.defaults.color = muted;
        Chart.defaults.borderColor = grid;
        Chart.defaults.animation = false;
        Chart.defaults.responsive = true;
        Chart.defaults.maintainAspectRatio = false;

        if (Chart.defaults.plugins && Chart.defaults.plugins.legend) {
            Chart.defaults.plugins.legend.labels.usePointStyle = true;
            Chart.defaults.plugins.legend.labels.boxWidth = 8;
            Chart.defaults.plugins.legend.labels.boxHeight = 8;
            Chart.defaults.plugins.legend.labels.color = text;
            Chart.defaults.plugins.legend.labels.padding = 12;
        }
        if (Chart.defaults.plugins && Chart.defaults.plugins.tooltip) {
            Chart.defaults.plugins.tooltip.backgroundColor = tooltipBg;
            Chart.defaults.plugins.tooltip.titleColor = tooltipFg;
            Chart.defaults.plugins.tooltip.bodyColor = tooltipFg;
            Chart.defaults.plugins.tooltip.cornerRadius = 4;
            Chart.defaults.plugins.tooltip.padding = 8;
        }
        if (Chart.defaults.scales) {
            ['linear', 'logarithmic', 'category', 'time'].forEach(function (key) {
                var scale = Chart.defaults.scales[key];
                if (!scale) return;
                if (scale.grid) scale.grid.color = grid;
                if (scale.ticks) scale.ticks.color = muted;
                if (scale.border) scale.border.color = grid;
            });
        }

        Chart.defaults.datasets = Chart.defaults.datasets || {};
        Chart.defaults.datasets.doughnut = Chart.defaults.datasets.doughnut || {};
        Chart.defaults.datasets.doughnut.borderWidth = 1;
        Chart.defaults.datasets.doughnut.borderColor = cssVar('--bg-surface');

        window.FC_CHART_COLORS = {
            brand: brand,
            positive: positive,
            negative: negative,
            text: text,
            muted: muted,
            grid: grid,
            palette: [
                cssVar('--chart-1'),
                cssVar('--chart-2'),
                cssVar('--chart-3'),
                cssVar('--chart-4'),
                cssVar('--chart-5'),
                cssVar('--chart-6'),
                cssVar('--chart-accent')
            ]
        };
        return true;
    }

    window.fcApplyChartDefaults = apply;
    if (!apply()) {
        timer = setInterval(function () {
            tries += 1;
            if (apply() || tries > 200) clearInterval(timer);
        }, 50);
    }
})();
