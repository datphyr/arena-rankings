/* Arena Rankings — shared table sorting + auto-submit filters.
 *
 * Sorting: any <table class="sortable"> gets clickable headers. Click a header
 * to sort by that column; click again to toggle asc/desc. Numeric columns are
 * detected by the presence of a `data-sort="num"` attribute on the <th> (or by
 * a `.num` class on cells). Default sort can be set with
 * `data-default-sort="colIndex"` and `data-default-dir="desc"` on the table.
 *
 * Filters: any <form class="filters"> with <select> elements auto-submits on
 * change (GET, preserving other fields).
 */
(function () {
  'use strict';

  // ── Sorting ──────────────────────────────────────────────────────────────
  function cellValue(cell) {
    if (cell.dataset.sort !== undefined) return cell.dataset.sort;
    var text = (cell.textContent || '').trim();
    // Treat as numeric only if it's a plain number (no hyphens/dates).
    // ISO dates like 2026-08-09 must stay strings so they sort lexicographically.
    if (/^[0-9]+(\.[0-9]+)?$/.test(text)) return parseFloat(text);
    return text.toLowerCase();
  }

  function sortTable(table, colIdx, dir) {
    var tbody = table.querySelector('tbody');
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.rows);
    var isNum = table.rows[0].cells[colIdx] &&
      (table.rows[0].cells[colIdx].classList.contains('num') ||
       table.rows[0].cells[colIdx].dataset.sort === 'num');
    rows.sort(function (a, b) {
      var av = cellValue(a.cells[colIdx]);
      var bv = cellValue(b.cells[colIdx]);
      var cmp;
      if (isNum) {
        av = parseFloat(String(av).replace(/[^0-9.\-]/g, '')) || 0;
        bv = parseFloat(String(bv).replace(/[^0-9.\-]/g, '')) || 0;
        cmp = av - bv;
      } else {
        cmp = String(av).localeCompare(String(bv), undefined, { numeric: true });
      }
      return dir === 'desc' ? -cmp : cmp;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
    // Update header indicators
    var ths = table.querySelectorAll('thead th');
    ths.forEach(function (th) {
      th.classList.remove('sort-asc', 'sort-desc');
      th.removeAttribute('aria-sort');
    });
    var th = ths[colIdx];
    if (th) {
      th.classList.add(dir === 'desc' ? 'sort-desc' : 'sort-asc');
      th.setAttribute('aria-sort', dir === 'desc' ? 'descending' : 'ascending');
    }
  }

  function initSortable(table) {
    var ths = table.querySelectorAll('thead th');
    var state = { col: -1, dir: 'asc' };
    ths.forEach(function (th, idx) {
      th.style.cursor = 'pointer';
      th.addEventListener('click', function () {
        var isNum = th.classList.contains('num') || th.dataset.sort === 'num';
        var isRank = th.dataset.sort === 'rank' || th.textContent.trim() === '#';
        if (state.col === idx) {
          state.dir = state.dir === 'asc' ? 'desc' : 'asc';
        } else {
          state.col = idx;
          // Default direction: ranks and text ascend, numerics descend
          state.dir = (isRank || !isNum) ? 'asc' : 'desc';
        }
        sortTable(table, idx, state.dir);
      });
    });
    // Apply default sort if specified
    var defCol = table.dataset.defaultSort;
    var defDir = table.dataset.defaultDir || 'desc';
    if (defCol !== undefined && defCol !== '') {
      state.col = parseInt(defCol, 10);
      state.dir = defDir;
      sortTable(table, state.col, state.dir);
    }
  }

  document.querySelectorAll('table.sortable').forEach(initSortable);

  // Expose for re-init after AJAX swaps.
  window.initSortableTables = function (root) {
    (root || document).querySelectorAll('table.sortable').forEach(initSortable);
  };
})();
