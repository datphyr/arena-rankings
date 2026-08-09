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
    var num = parseFloat(text.replace(/[^0-9.\-]/g, ''));
    if (!isNaN(num) && /[0-9]/.test(text)) return num;
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
        if (state.col === idx) {
          state.dir = state.dir === 'asc' ? 'desc' : 'asc';
        } else {
          state.col = idx;
          state.dir = 'asc';
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

  // ── Auto-submit filters ─────────────────────────────────────────────────
  document.querySelectorAll('form.filters').forEach(function (form) {
    form.querySelectorAll('select').forEach(function (sel) {
      sel.addEventListener('change', function () { form.submit(); });
    });
  });
})();
