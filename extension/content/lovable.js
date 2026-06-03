// Lovable — project name from the header when present, else document.title.
// Generating = a visible spinner/"Generating" control (generic check covers it).
(function () {
  "use strict";
  window.__TASKHUB_REGISTER({
    source: "Lovable",
    getTitle() {
      const sel =
        document.querySelector("header h1") ||
        document.querySelector('[data-testid="project-name"]') ||
        document.querySelector('[class*="project"][class*="name"]');
      const t = sel && sel.textContent ? sel.textContent.trim() : "";
      return t && t.length <= 120 ? t : "";
    },
  });
})();
