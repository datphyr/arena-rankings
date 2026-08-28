// Rewrite every <time class="local-dt" datetime="...Z"> into the visitor's
// local timezone, keeping the site's 'YYYY-MM-DD, HH:MM' format. The
// server-rendered text (ClickHouse server tz) is the no-JS fallback.
// A MutationObserver covers AJAX-swapped partials (ajax-filters.js).
(function () {
  var fmt = null;

  function getFmt() {
    if (fmt) return fmt;
    fmt = new Intl.DateTimeFormat(undefined, {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false
    });
    return fmt;
  }

  function localizeOne(el) {
    var d = new Date(el.getAttribute('datetime'));
    if (isNaN(d)) { el.setAttribute('data-localized', 'skip'); return; }
    try {
      var p = {};
      getFmt().formatToParts(d).forEach(function (x) { p[x.type] = x.value; });
      var hour = p.hour === '24' ? '00' : p.hour;
      el.textContent = p.year + '-' + p.month + '-' + p.day + ', ' + hour + ':' + p.minute;
    } catch (e) { return; }
    el.setAttribute('data-localized', '1');
  }

  function localize(scope) {
    var els = scope.querySelectorAll('time.local-dt:not([data-localized])');
    if (!els.length) return;
    getFmt();
    for (var i = 0; i < els.length; i++) localizeOne(els[i]);
  }

  // Initial pass + anything swapped in later (AJAX filter updates).
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { localize(document); });
  } else {
    localize(document);
  }

  var mo = new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      var added = muts[i].addedNodes;
      if (!added || !added.length) continue;
      for (var j = 0; j < added.length; j++) {
        var n = added[j];
        if (n.nodeType !== 1) continue;
        if (n.matches && n.matches('time.local-dt')) localizeOne(n);
        else if (n.querySelectorAll) {
          var inner = n.querySelectorAll('time.local-dt');
          for (var k = 0; k < inner.length; k++) localizeOne(inner[k]);
        }
      }
    }
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });
})();