/* grep — section tabs, scoped filters, and theme logic */
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

  /* top-level section tabs */
  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-section-panel]"));
  var tabs = Array.prototype.slice.call(document.querySelectorAll("[data-section-tab]"));
  if (!panels.length) return;

  function validSection(id) {
    return panels.some(function (panel) {
      return panel.getAttribute("data-section-panel") === id;
    });
  }

  function setSection(id, updateUrl) {
    if (!validSection(id)) id = panels[0].getAttribute("data-section-panel");
    panels.forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-section-panel") !== id;
    });
    tabs.forEach(function (tab) {
      var active = tab.getAttribute("data-section-tab") === id;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-current", active ? "page" : "false");
    });
    if (updateUrl && window.location.hash !== "#" + id) {
      window.history.replaceState(null, "", "#" + id);
    }
  }

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function (event) {
      event.preventDefault();
      setSection(tab.getAttribute("data-section-tab"), true);
    });
  });
  window.addEventListener("hashchange", function () {
    setSection(window.location.hash.slice(1), false);
  });
  setSection(window.location.hash.slice(1), false);

  /* source/tier filters are independent inside each section */
  panels.forEach(function (panel) {
    var sectionId = panel.getAttribute("data-section-panel");
    var source = "all";
    var tier = "all";
    var stories = Array.prototype.slice.call(panel.querySelectorAll(".story"));
    var countEl = document.getElementById("count-" + sectionId);
    var buttons = Array.prototype.slice.call(panel.querySelectorAll(".chip"));

    function matches(el) {
      var okSource = source === "all" || el.getAttribute("data-source") === source;
      var group = el.getAttribute("data-tier-group");
      var okTier = tier === "all" || group === tier;
      return okSource && okTier;
    }

    function apply() {
      var visible = 0;
      stories.forEach(function (el) {
        var show = matches(el);
        el.hidden = !show;
        if (show) visible++;
      });
      if (countEl) countEl.textContent = String(visible) + " stories";
    }

    buttons.forEach(function (chip) {
      chip.addEventListener("click", function () {
        var isSource = chip.hasAttribute("data-filter-source");
        var attr = isSource ? "data-filter-source" : "data-filter-tier";
        buttons.filter(function (candidate) {
          return candidate.hasAttribute(attr);
        }).forEach(function (candidate) {
          candidate.classList.remove("active");
        });
        chip.classList.add("active");
        if (isSource) {
          source = chip.getAttribute("data-filter-source");
        } else {
          tier = chip.getAttribute("data-filter-tier");
        }
        apply();
      });
    });

    apply();
  });
})();
