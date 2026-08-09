// In-place (AJAX) sorting for the home page Top Players table.
// Clicking Elo / Glicko-2 swaps the table data without a page reload.
(function () {
  var tableWrap = document.querySelector(".dash-grid .table-wrap");
  if (!tableWrap) return;

  var links = tableWrap.querySelectorAll("a.sort-link[data-sort]");
  if (!links.length) return;

  links.forEach(function (link) {
    link.addEventListener("click", function (e) {
      e.preventDefault();
      var sort = link.getAttribute("data-sort");
      if (!sort) return;

      // Update URL without reloading (so refresh/share keeps the sort).
      try {
        var url = new URL(window.location.href);
        url.searchParams.set("sort", sort);
        history.replaceState({}, "", url.toString());
      } catch (err) {}

      // Fetch the new table HTML and swap it in place.
      fetch("/top-players?sort=" + encodeURIComponent(sort))
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.text();
        })
        .then(function (html) {
          tableWrap.innerHTML = html;
          // Re-bind the new sort links.
          bindLinks(tableWrap);
        })
        .catch(function (err) {
          console.error("top-players sort failed:", err);
        });
    });
  });

  function bindLinks(container) {
    var newLinks = container.querySelectorAll("a.sort-link[data-sort]");
    newLinks.forEach(function (l) {
      l.addEventListener("click", function (e) {
        e.preventDefault();
        var s = l.getAttribute("data-sort");
        if (!s) return;
        try {
          var u = new URL(window.location.href);
          u.searchParams.set("sort", s);
          history.replaceState({}, "", u.toString());
        } catch (err) {}
        fetch("/top-players?sort=" + encodeURIComponent(s))
          .then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.text();
          })
          .then(function (html) {
            container.innerHTML = html;
            bindLinks(container);
          })
          .catch(function (err) {
            console.error("top-players sort failed:", err);
          });
      });
    });
  }
})();
