/* grep — filter + theme logic */
(function () {
  "use strict";

  /* theme */
  var KEY = "grep-theme";
  var toggle = document.getElementById("theme-toggle");
  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    if (toggle) toggle.textContent = t === "dark" ? "\u263E" : "\u263C"; /* ☾ / ☼ */
  }
  applyTheme(localStorage.getItem(KEY) || "dark");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      localStorage.setItem(KEY, t);
      applyTheme(t);
    });
  }

  /* filters: source (all|hn|arxiv|github|other|reddit) x tier (all|rec|must) */
  var source = "all", tier = "all";
  var stories = Array.prototype.slice.call(document.querySelectorAll(".story"));
  var countEl = document.getElementById("count");

  function matches(el) {
    var okSource = source === "all" || el.getAttribute("data-source") === source;
    var g = el.getAttribute("data-tier-group");
    var okTier = tier === "all" || g === tier;
    return okSource && okTier;
  }

  function apply() {
    var visible = 0;
    stories.forEach(function (el) {
      var show = matches(el);
      el.hidden = !show;
      if (show) visible++;
    });
    if (countEl) countEl.textContent = String(visible);
  }

  document.querySelectorAll(".chip").forEach(function (chip) {
    chip.addEventListener("click", function () {
      var isSrc = chip.hasAttribute("data-filter-source");
      var key = isSrc ? "data-filter-source" : "data-filter-tier";
      var sameGroup = "data-filter-source";
      if (!isSrc) sameGroup = "data-filter-tier";
      document.querySelectorAll('.chip[data-filter-' + (isSrc ? "source" : "tier") + ']').forEach(function (c) {
        c.classList.remove("active");
      });
      chip.classList.add("active");
      if (isSrc) {
        source = chip.getAttribute("data-filter-source");
        tier = document.querySelector(".chip[data-filter-tier].active").getAttribute("data-filter-tier");
      } else {
        tier = chip.getAttribute("data-filter-tier");
        source = document.querySelector(".chip[data-filter-source].active").getAttribute("data-filter-source");
      }
      apply();
    });
  });

  apply();
})();
