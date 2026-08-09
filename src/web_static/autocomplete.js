// Autocomplete dropdowns for manual filter inputs.
// Any <input data-autocomplete="player|tournament"> gets a suggestion
// dropdown fed by GET /autocomplete?type=...&q=... (case-insensitive
// substring match). Clicking or pressing Enter/Arrow keys fills the input.
(function () {
  function init() {
    document.querySelectorAll("input[data-autocomplete]").forEach(function (input) {
      if (input.__acBound) return;
      input.__acBound = true;
      new Autocomplete(input);
    });
  }

  function Autocomplete(input) {
    this.input = input;
    this.type = input.getAttribute("data-autocomplete");
    this.wrap = document.createElement("div");
    this.wrap.className = "ac-wrap";
    this.list = document.createElement("div");
    this.list.className = "ac-list";
    this.list.style.display = "none";
    this.items = [];
    this.active = -1;
    this.timer = null;

    // Wrap the input so the dropdown can be positioned relative to it.
    input.parentNode.insertBefore(this.wrap, input);
    this.wrap.appendChild(input);
    this.wrap.appendChild(this.list);

    var self = this;
    input.addEventListener("input", function () { self.onInput(); });
    input.addEventListener("focus", function () { if (self.items.length) self.show(); });
    input.addEventListener("blur", function () { setTimeout(function () { self.hide(); }, 150); });
    input.addEventListener("keydown", function (e) { self.onKey(e); });
    this.list.addEventListener("mousedown", function (e) { e.preventDefault(); });
  }

  Autocomplete.prototype.onInput = function () {
    var self = this;
    clearTimeout(this.timer);
    var q = this.input.value.trim();
    if (q.length < 1) { this.hide(); return; }
    this.timer = setTimeout(function () { self.fetch(q); }, 200);
  };

  Autocomplete.prototype.fetch = function (q) {
    var self = this;
    var url = "/autocomplete?type=" + encodeURIComponent(this.type) +
              "&q=" + encodeURIComponent(q) + "&limit=20";
    fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        self.items = data.names || [];
        self.render();
      })
      .catch(function () { self.hide(); });
  };

  Autocomplete.prototype.render = function () {
    var self = this;
    this.list.innerHTML = "";
    if (!this.items.length) { this.hide(); return; }
    this.items.forEach(function (name, i) {
      var div = document.createElement("div");
      div.className = "ac-item" + (i === self.active ? " active" : "");
      div.textContent = name;
      div.addEventListener("mousedown", function () { self.pick(name); });
      div.addEventListener("mouseenter", function () {
        self.active = i;
        self.highlight();
      });
      self.list.appendChild(div);
    });
    this.show();
  };

  Autocomplete.prototype.highlight = function () {
    var items = this.list.querySelectorAll(".ac-item");
    items.forEach(function (el, i) {
      el.classList.toggle("active", i === this.active);
    }, this);
  };

  Autocomplete.prototype.pick = function (name) {
    this.input.value = name;
    this.hide();
    this.input.focus();
  };

  Autocomplete.prototype.show = function () {
    this.list.style.display = "block";
  };

  Autocomplete.prototype.hide = function () {
    this.list.style.display = "none";
    this.active = -1;
  };

  Autocomplete.prototype.onKey = function (e) {
    if (this.list.style.display === "none") return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      this.active = (this.active + 1) % this.items.length;
      this.highlight();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      this.active = (this.active - 1 + this.items.length) % this.items.length;
      this.highlight();
    } else if (e.key === "Enter") {
      if (this.active >= 0 && this.items[this.active]) {
        e.preventDefault();
        this.pick(this.items[this.active]);
      }
    } else if (e.key === "Escape") {
      this.hide();
    }
  };

  // Re-init after AJAX swaps (the results container is replaced in place).
  if (window.MutationObserver) {
    var target = document.getElementById("results-container");
    if (target) {
      var obs = new MutationObserver(function () { init(); });
      obs.observe(target, { childList: true, subtree: true });
    }
  }

  init();
})();
