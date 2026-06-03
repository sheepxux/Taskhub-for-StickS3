// Gemini — title comes from the active conversation list item when available,
// otherwise the cleaned document.title. Generating = generic Stop-button check.
(function () {
  "use strict";
  window.__TASKHUB_REGISTER({
    source: "Gemini",
    getTitle() {
      // The selected conversation in the side rail, if the DOM exposes it.
      const sel =
        document.querySelector('[data-test-id="conversation"].selected') ||
        document.querySelector('.conversation.selected') ||
        document.querySelector('[aria-current="page"] .conversation-title');
      const t = sel && sel.textContent ? sel.textContent.trim() : "";
      return t && t.length <= 120 ? t : "";
    },
  });
})();
