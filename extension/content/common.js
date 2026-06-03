// TaskHub Web Bridge — shared content-script machinery.
//
// A per-site script (gemini.js / lovable.js / perplexity.js) calls
// window.__TASKHUB_REGISTER(config). This module then watches the page and,
// whenever the derived {title, status} changes, sends it to the background
// service worker, which forwards it to the local TaskHub Host via POST /ingest.
//
// Design: lead with document.title (stable across redesigns) and use a generic
// "is generating" heuristic. If the site-specific bits break, we still report a
// real conversation title — degrading gracefully instead of going blank.
(function () {
  "use strict";
  if (window.__TASKHUB_BRIDGE_LOADED) return;
  window.__TASKHUB_BRIDGE_LOADED = true;

  const POLL_MS = 4000;
  let cfg = null;
  let lastKey = "";

  // Strip a trailing product suffix like " - Gemini" / " | Perplexity".
  function cleanTitle(raw, source) {
    let t = (raw || "").replace(/[‎‏‪-‮]/g, "").trim();
    // common separators followed by the product name at the end
    const seps = [" - ", " | ", " — ", " · ", " • "];
    for (const sep of seps) {
      const idx = t.toLowerCase().lastIndexOf(sep + source.toLowerCase());
      if (idx > 0) { t = t.slice(0, idx).trim(); break; }
    }
    // also drop a bare leading/trailing product name
    if (t.toLowerCase() === source.toLowerCase()) t = "";
    // notification count prefix e.g. "(3) Foo"
    t = t.replace(/^\(\d+\)\s*/, "").trim();
    return t;
  }

  // Generic "model is generating" detector: a visible Stop control.
  function genericIsGenerating() {
    const nodes = document.querySelectorAll(
      'button,[role="button"],[aria-label]'
    );
    for (const el of nodes) {
      const label = (
        (el.getAttribute && el.getAttribute("aria-label")) ||
        el.textContent ||
        ""
      ).trim().toLowerCase();
      if (!label || label.length > 40) continue;
      if (
        label === "stop" ||
        label.includes("stop generating") ||
        label.includes("stop response") ||
        label.includes("stop streaming") ||
        label.includes("正在生成") ||
        label.includes("停止")
      ) {
        // must be actually visible
        const r = el.getBoundingClientRect && el.getBoundingClientRect();
        if (!r || (r.width > 0 && r.height > 0)) return true;
      }
    }
    return false;
  }

  function compute() {
    let title = "";
    try {
      title = cfg.getTitle ? cfg.getTitle() : "";
    } catch (_) {}
    if (!title) title = cleanTitle(document.title, cfg.source);

    let generating = false;
    try {
      generating = cfg.isGenerating ? cfg.isGenerating() : genericIsGenerating();
    } catch (_) {
      generating = genericIsGenerating();
    }
    const status = generating ? "running" : "recent";
    return { title: title || "", status };
  }

  function tick() {
    if (!cfg) return;
    if (document.hidden) return; // only report the foreground tab's work
    const { title, status } = compute();
    if (!title) return; // nothing meaningful to show
    const key = cfg.source + "|" + title + "|" + status;
    if (key === lastKey) return;
    lastKey = key;
    try {
      chrome.runtime.sendMessage({
        type: "taskhub-task",
        source: cfg.source,
        title: title,
        status: status,
        url: location.href.split("#")[0],
      });
    } catch (_) {
      // background may be asleep; next tick retries
    }
  }

  window.__TASKHUB_REGISTER = function (config) {
    cfg = config || {};
    if (!cfg.source) return;
    // initial + steady poll; MutationObserver makes it feel instant on changes
    setTimeout(tick, 1500);
    setInterval(tick, POLL_MS);
    try {
      const obs = new MutationObserver(() => {
        // debounce via the key check inside tick()
        tick();
      });
      obs.observe(document.documentElement, {
        subtree: true,
        childList: true,
        characterData: true,
      });
    } catch (_) {}
    document.addEventListener("visibilitychange", tick);
  };
})();
