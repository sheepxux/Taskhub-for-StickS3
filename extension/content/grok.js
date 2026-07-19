// Grok - prefer the active conversation title, then the page heading.
// Running is intentionally conservative: only a visible Stop control counts.
(function () {
  "use strict";
  window.__TASKHUB_REGISTER({
    source: "Grok",
    getTitle() {
      const selectors = [
        '[aria-current="page"][href*="/c/"]',
        '[data-state="active"][href*="/c/"]',
        'a[href*="/c/"][class*="active"]',
        'main h1',
      ];
      for (const selector of selectors) {
        const el = document.querySelector(selector);
        const title = el && el.textContent ? el.textContent.trim() : "";
        if (title && title.length <= 140 && !/^new (chat|conversation)$/i.test(title)) return title;
      }
      return "";
    },
  });
})();
