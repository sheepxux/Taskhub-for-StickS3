// Kimi - read the selected conversation from the side rail when possible.
// The shared bridge falls back to the cleaned browser tab title and detects
// generation only from a visible Stop control.
(function () {
  "use strict";
  window.__TASKHUB_REGISTER({
    source: "Kimi",
    getTitle() {
      const selectors = [
        '[aria-current="page"][href*="/chat/"]',
        '[data-state="active"][href*="/chat/"]',
        '[class*="active"] [class*="conversation"][class*="title"]',
        'main h1',
      ];
      for (const selector of selectors) {
        const el = document.querySelector(selector);
        const title = el && el.textContent ? el.textContent.trim() : "";
        if (title && title.length <= 140 && !/^new chat$/i.test(title)) return title;
      }
      return "";
    },
  });
})();
