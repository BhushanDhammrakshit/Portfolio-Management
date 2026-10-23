(function () {
    "use strict";

    function csvValue(value) {
        return '"' + String(value).replace(/"/g, '""') + '"';
    }

    function initializeTable(component) {
        var table = component.querySelector(".ds-table");
        var body = table && table.tBodies[0];
        if (!table || !body) return;

        var search = component.querySelector("[data-table-search]");
        var info = component.querySelector("[data-table-page-info]");
        var previous = component.querySelector("[data-table-prev]");
        var next = component.querySelector("[data-table-next]");
        var pageSize = Number(component.getAttribute("data-page-size")) || 20;
        var page = 0;
        var allRows = Array.from(body.rows);
        var emptyRows = allRows.filter(function (row) {
            return row.querySelector(".ds-table-empty") !== null;
        });
        var originalRows = allRows.filter(function (row) {
            return row.querySelector(".ds-table-empty") === null;
        });
        var filteredRows = originalRows.slice();
        var sortColumn = -1;
        var sortDirection = 1;
        var noMatches = document.createElement("tr");
        var noMatchesCell = document.createElement("td");
        noMatchesCell.className = "ds-table-empty";
        noMatchesCell.colSpan = table.tHead.rows[0].cells.length;
        noMatchesCell.textContent = "No matching rows.";
        noMatches.appendChild(noMatchesCell);

        function sorted(rows) {
            if (sortColumn < 0) return rows;
            var collator = new Intl.Collator("en-IN", { numeric: true, sensitivity: "base" });
            return rows.slice().sort(function (left, right) {
                var leftText = left.cells[sortColumn].textContent.trim();
                var rightText = right.cells[sortColumn].textContent.trim();
                var leftNumber = Number(leftText.replace(/[₹,%+\s,]/g, ""));
                var rightNumber = Number(rightText.replace(/[₹,%+\s,]/g, ""));
                var bothNumeric = leftText !== "" && rightText !== "" &&
                    Number.isFinite(leftNumber) && Number.isFinite(rightNumber);
                var comparison = bothNumeric
                    ? leftNumber - rightNumber
                    : collator.compare(leftText, rightText);
                return comparison * sortDirection;
            });
        }

        function render() {
            var start = page * pageSize;
            var end = start + pageSize;
            Array.from(body.rows).forEach(function (row) {
                row.hidden = filteredRows.indexOf(row) < start || filteredRows.indexOf(row) >= end;
                if (!row.querySelector(".ds-table-empty")) row.tabIndex = 0;
            });
            emptyRows.forEach(function (row) { row.hidden = originalRows.length > 0; });
            noMatches.hidden = filteredRows.length > 0 || originalRows.length === 0;
            if (!noMatches.isConnected && originalRows.length > 0) body.appendChild(noMatches);
            if (info) {
                var first = filteredRows.length ? start + 1 : 0;
                var last = Math.min(end, filteredRows.length);
                info.textContent = first + "–" + last + " of " + filteredRows.length;
            }
            if (previous) previous.disabled = page === 0;
            if (next) next.disabled = end >= filteredRows.length;
        }

        if (search) {
            search.addEventListener("input", function () {
                var query = search.value.trim().toLocaleLowerCase();
                filteredRows = sorted(originalRows.filter(function (row) {
                    return row.textContent.toLocaleLowerCase().indexOf(query) !== -1;
                }));
                page = 0;
                render();
            });
        }

        if (previous) previous.addEventListener("click", function () {
            if (page > 0) page -= 1;
            render();
        });
        if (next) next.addEventListener("click", function () {
            if ((page + 1) * pageSize < filteredRows.length) page += 1;
            render();
        });

        body.addEventListener("keydown", function (event) {
            if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
            var currentRow = event.target.closest("tr");
            var visibleRows = filteredRows.slice(page * pageSize, (page + 1) * pageSize);
            var index = visibleRows.indexOf(currentRow);
            if (index < 0) return;
            var direction = event.key === "ArrowDown" ? 1 : -1;
            var adjacentRow = visibleRows[index + direction];
            if (!adjacentRow) return;
            event.preventDefault();
            adjacentRow.focus();
        });

        component.querySelectorAll("[data-table-sort]").forEach(function (control) {
            control.addEventListener("click", function () {
                var column = Number(control.getAttribute("data-table-sort"));
                sortDirection = sortColumn === column ? sortDirection * -1 : 1;
                sortColumn = column;
                component.querySelectorAll(".ds-table thead th").forEach(function (heading, index) {
                    heading.setAttribute("aria-sort", index !== column
                        ? "none"
                        : (sortDirection === 1 ? "ascending" : "descending"));
                });
                filteredRows = sorted(filteredRows);
                page = 0;
                render();
            });
        });

        component.querySelectorAll("[data-column-index]").forEach(function (control) {
            control.addEventListener("change", function () {
                var index = Number(control.getAttribute("data-column-index"));
                Array.from(table.rows).forEach(function (row) {
                    if (row.cells[index]) row.cells[index].hidden = !control.checked;
                });
            });
        });

        var density = component.querySelector("[data-table-density]");
        if (density) {
            density.addEventListener("click", function () {
                var compact = table.classList.toggle("is-compact");
                density.setAttribute("aria-pressed", String(compact));
            });
        }

        var exportButton = component.querySelector("[data-table-export]");
        if (exportButton) {
            exportButton.addEventListener("click", function () {
                var visibleColumns = Array.from(table.rows[0].cells).map(function (cell, index) {
                    return { index: index, hidden: cell.hidden };
                }).filter(function (column) {
                    return !column.hidden;
                });
                var lines = [visibleColumns.map(function (column) {
                    return csvValue(table.rows[0].cells[column.index].textContent.trim());
                }).join(",")];
                filteredRows.forEach(function (row) {
                    lines.push(visibleColumns.map(function (column) {
                        return csvValue(row.cells[column.index].textContent.trim());
                    }).join(","));
                });

                var blob = new Blob(["\uFEFF" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
                var url = URL.createObjectURL(blob);
                var link = document.createElement("a");
                link.href = url;
                link.download = "data.csv";
                link.click();
                URL.revokeObjectURL(url);
            });
        }

        render();
    }

    document.querySelectorAll("[data-ds-table]").forEach(initializeTable);
})();
