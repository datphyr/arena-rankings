// Generic in-place (AJAX) filter/pagination/sort swapping.
// Any <form class="filters"> submit, .page-link click, or leaderboard
// .sort-link click fetches the same URL with &partial=1 and swaps the
// #results-container content in place — no page reload, no scroll jump.
(function () {
  var container = document.getElementById("results-container");
  if (!container) return;

  function formAction(form) {
    return form.getAttribute("action") || window.location.pathname;
  }

  // Serialize a form, dropping empty values. The submitter button (e.g. the
  // "Clear filters" button) is captured via event.submitter. If a "clear"
  // button was pressed, keep only clear=1 so the URL stays clean.
  function serializeForm(form, submitter) {
    var data = new FormData(form);
    var parts = [];
    var isClear = false;
    if (submitter && submitter.name) {
      data.set(submitter.name, submitter.value || "");
      if (submitter.name === "clear") isClear = true;
    }
    if (isClear) {
      return "clear=1";
    }
    data.forEach(function (value, key) {
      if (value === "" || value === null || value === undefined) return;
      parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(value));
    });
    return parts.join("&");
  }

  function addPartial(urlStr) {
    try {
      var url = new URL(urlStr, window.location.origin);
      url.searchParams.set("partial", "1");
      // Partial renders only results; strip page if we want to keep it simple.
      return url.toString();
    } catch (err) {
      return urlStr;
    }
  }

  function replaceState(urlStr) {
    try {
      // Keep the non-partial URL in the address bar.
      var url = new URL(urlStr, window.location.origin);
      url.searchParams.delete("partial");
      history.replaceState({}, "", url.pathname + url.search + url.hash);
    } catch (err) {}
  }

  function swap(html) {
    var tmp = document.createElement("div");
    tmp.innerHTML = html;
    var next = tmp.querySelector("#results-container");
    if (next) {
      container.replaceWith(next);
      container = next;
      bind();
      // Re-init client-side sortable tables in the swapped content.
      if (window.initSortableTables) window.initSortableTables(next);
    }
  }

  function fetchAndSwap(url) {
    fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) {
        swap(html);
        replaceState(url);
        if (url.indexOf("clear=1") !== -1) resetForms();
      })
      .catch(function (err) {
        console.error("AJAX swap failed:", err);
        // Fall back to a normal navigation so the user still gets results.
        window.location.href = url;
      });
  }

  // After a "clear filters" submit, reset the filter form controls so the UI
  // matches the cleared (unfiltered) state, and hide the clear button.
  // Note: form.reset() resets to the template-baked values (which reflect the
  // last filter), so we explicitly clear each field instead.
  function resetForms() {
    document.querySelectorAll("form.filters").forEach(function (form) {
      form.querySelectorAll("select").forEach(function (sel) {
        sel.selectedIndex = 0;
      });
      form.querySelectorAll("input").forEach(function (inp) {
        if (inp.type === "text" || inp.type === "search" || inp.type === "date") {
          inp.value = "";
        }
      });
      updateClearButton(form);
    });
  }

  // Show the "Clear filters" button only when a real filter is set
  // (mirrors the template's {% if game or player or tournament %} / tier).
  var FILTER_FIELDS = ["game", "player", "tournament", "tier", "p1", "p2", "match"];
  function updateClearButton(form) {
    var clearBtn = form.querySelector('button[name="clear"]');
    if (!clearBtn) return;
    var hasFilter = false;
    form.querySelectorAll("select, input").forEach(function (el) {
      if (FILTER_FIELDS.indexOf(el.name) !== -1 && el.value) hasFilter = true;
    });
    clearBtn.style.display = hasFilter ? "" : "none";
  }

  function handle(url, e) {
    e.preventDefault();
    // Preserve current scroll (default anchors would jump; fetch keeps it).
    var y = window.scrollY;
    fetchAndSwap(addPartial(url));
    // Restore scroll after swap (in case layout shifts).
    window.requestAnimationFrame(function () {
      window.scrollTo(0, y);
    });
  }

  function bind() {
    // Filter form submits.
    var forms = document.querySelectorAll("form.filters");
    forms.forEach(function (form) {
      if (form.__ajaxBound) return;
      form.__ajaxBound = true;
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var base = formAction(form);
        var qs = serializeForm(form, e.submitter);
        var url = qs ? base + "?" + qs : base;
        fetchAndSwap(addPartial(url));
        // Update clear-button visibility based on the new state (unless clearing).
        if (url.indexOf("clear=1") === -1) updateClearButton(form);
      });
      // Select changes trigger the same AJAX submit (was native reload).
      form.querySelectorAll("select").forEach(function (sel) {
        if (sel.__ajaxBound) return;
        sel.__ajaxBound = true;
        sel.addEventListener("change", function () {
          form.dispatchEvent(new Event("submit", { cancelable: true }));
        });
      });
      updateClearButton(form);
    });

    // Pagination links (skip disabled/current).
    var pageLinks = document.querySelectorAll(".page-link:not(.disabled):not(.current)");
    pageLinks.forEach(function (a) {
      if (a.__ajaxBound) return;
      a.__ajaxBound = true;
      a.addEventListener("click", function (e) {
        handle(a.href, e);
      });
    });

    // Leaderboard server-side sort links.
    var sortLinks = document.querySelectorAll("a.sort-link");
    sortLinks.forEach(function (a) {
      if (a.__ajaxBound) return;
      a.__ajaxBound = true;
      a.addEventListener("click", function (e) {
        handle(a.href, e);
      });
    });
  }

  bind();
})();
