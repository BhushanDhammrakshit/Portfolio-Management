(function (global) {
    "use strict";

    var INDIA = "en-IN";
    var IST = "Asia/Kolkata";

    function finiteValue(value) {
        if (value === null || value === undefined || value === "") return null;
        var number = typeof value === "number" ? value : Number(value);
        return Number.isFinite(number) ? number : null;
    }

    function missing(reason) {
        var message = reason || "This value is unavailable.";
        var escaped = String(message).replace(/[&<>"']/g, function (character) {
            return {
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;"
            }[character];
        });
        return '<span class="fc-na" title="' + escaped + '">N/A</span>';
    }

    function formatNumber(value, decimals, reason) {
        var number = finiteValue(value);
        if (number === null) return missing(reason);
        var digits = Number.isInteger(decimals) && decimals >= 0 ? decimals : 2;
        return new Intl.NumberFormat(INDIA, {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        }).format(number);
    }

    function formatCurrency(value, signed, reason) {
        var number = finiteValue(value);
        if (number === null) return missing(reason);
        var sign = number < 0 ? "-" : (signed && number > 0 ? "+" : "");
        var amount = new Intl.NumberFormat(INDIA, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        }).format(Math.abs(number));
        return sign + "\u20B9" + amount;
    }

    function formatPercent(value, signed, reason) {
        var number = finiteValue(value);
        if (number === null) return missing(reason);
        var prefix = number < 0 ? "-" : (signed && number >= 0 ? "+" : "");
        return prefix + new Intl.NumberFormat(INDIA, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        }).format(Math.abs(number)) + "%";
    }

    function formatRatio(value, reason) {
        return formatNumber(value, 2, reason);
    }

    function formatDate(value, reason) {
        if (value === null || value === undefined || value === "") return missing(reason);
        var date = value instanceof Date ? value : new Date(value);
        if (Number.isNaN(date.getTime())) return missing(reason);
        return new Intl.DateTimeFormat(INDIA, {
            day: "2-digit",
            month: "short",
            year: "numeric",
            timeZone: IST
        }).format(date);
    }

    function formatTime(value, reason) {
        if (value === null || value === undefined || value === "") return missing(reason);
        var date = value instanceof Date ? value : new Date(value);
        if (Number.isNaN(date.getTime())) return missing(reason);
        return new Intl.DateTimeFormat(INDIA, {
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23",
            timeZone: IST
        }).format(date) + " IST";
    }

    function formatVolume(value, reason) {
        var number = finiteValue(value);
        if (number === null) return missing(reason);
        var absolute = Math.abs(number);
        var compact = absolute >= 10000000
            ? (number / 10000000).toFixed(2) + " Cr"
            : absolute >= 100000
                ? (number / 100000).toFixed(2) + " L"
                : absolute >= 1000
                    ? (number / 1000).toFixed(2) + "K"
                    : new Intl.NumberFormat(INDIA, { maximumFractionDigits: 0 }).format(number);
        return compact;
    }

    global.FCFormat = Object.freeze({
        missing: missing,
        number: formatNumber,
        currency: formatCurrency,
        percent: formatPercent,
        ratio: formatRatio,
        date: formatDate,
        time: formatTime,
        volume: formatVolume
    });
})(window);
