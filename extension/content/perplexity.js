// Perplexity — the thread heading is the best title; fall back to document.title.
// Generating = a visible Stop control (generic check).
(function () {
  "use strict";
  window.__TASKHUB_REGISTER({
    source: "Perplexity",
    getTitle() {
      const sel =
        document.querySelector("h1") ||
        document.querySelector('[class*="thread"] [class*="title"]') ||
        document.querySelector('[data-testid="thread-title"]');
      const t = sel && sel.textContent ? sel.textContent.trim() : "";
      return t && t.length <= 140 ? t : "";
    },
  });
})();
