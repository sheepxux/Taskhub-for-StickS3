# Browser Extension for Web-Source Task Titles — Feasibility & Workload

## Why this exists

Local CLI/desktop sources (Codex, Claude Code, OpenClaw) write session logs to
disk, so the Host reads **exact** task titles, status, and turn state.

Web products (Gemini, Lovable, plain Perplexity search) keep task data on their
servers. The Host can only see the **browser tab title** via AppleScript, which
is usually just the product name — not the specific conversation/task. There is
no local file to read.

A browser extension is the only way to read the real on-page task title: it runs
inside the page and can read the DOM directly.

## Current Host ingest gap (grounded)

As of v1.1.1 the Host exposes **no inbound task endpoint**. Routes today:

- `GET /health`, `GET /tasks`, `GET /tasks/:id(.json)`, `GET /peers(.json)`,
  `GET /debug/lovable`
- `POST /tasks/:id/open`, `POST /tasks/:id/open-native`

So step one for any extension path is a new **`POST /ingest`** endpoint that
accepts externally-sourced tasks. The existing `task()` schema is already a
clean target:

```json
{ "id", "source", "title", "status", "updated_ms", "subtitle", "detail", "usage" }
```

## Architecture

```
Browser tab (gemini.google.com / lovable.dev / perplexity.ai)
  └─ content script  ── reads DOM: conversation title, generating/idle state
        │  chrome.runtime.sendMessage
        ▼
  background service worker  ── debounces, dedups, POSTs every ~5s
        │  fetch("http://127.0.0.1:5577/ingest", {token})
        ▼
  Host  ── new POST /ingest → ExternalTaskAdapter holds pushed tasks,
           expires them after N seconds of no update → merged into /tasks
        ▼
  StickS3 (unchanged)
```

The extension talks to the Host directly over loopback HTTP (no native-messaging
host needed, simpler than the OS-bridge approach). The Host already binds
`0.0.0.0:5577`; `/ingest` would be loopback-gated + token-gated like `/tasks`.

## Per-site work (the real cost)

Each site needs its own DOM selectors, and they break when the site redesigns.
This is the recurring maintenance tax, not a one-time build.

| Site | Title source | Running/idle signal | Fragility |
| --- | --- | --- | --- |
| Gemini | active conversation list item / `document.title` | streaming response node present | Medium |
| Lovable | project name in header | "Generating…" / spinner node | Medium |
| Perplexity | thread heading | answer-streaming node | Medium |
| ChatGPT (bonus, same pattern) | conversation title | stop-button visible | Medium |

Detecting "needs your input" from the DOM is harder than running/idle and may
be unreliable per-site — recommend shipping running/done first, attention later.

## Effort estimate

| Phase | Scope | Effort |
| --- | --- | --- |
| 1. Host `POST /ingest` + `ExternalTaskAdapter` + expiry + tests | backend | ~0.5 day |
| 2. Extension skeleton (MV3: manifest, bg worker, options page for token) | extension | ~0.5 day |
| 3. First site adapter (one of Gemini/Lovable/Perplexity) end-to-end | content script | ~0.5–1 day |
| 4. Each additional site | content script | ~0.25–0.5 day each |
| 5. Packaging, load-unpacked docs, token pairing UX | docs | ~0.5 day |

**MVP (ingest + extension + one site): ~1.5–2 days.** Full 3-site coverage:
~3–4 days. Ongoing: occasional selector fixes when a site redesigns.

## Trade-offs / honest caveats

- **Chrome/Edge first** (MV3). Safari needs a separate Safari Web Extension
  wrapper + Xcode signing — meaningful extra work; defer.
- **Selectors are brittle.** Each site redesign can silently break a title.
  Without a test/fixture per site, this becomes the same silent-staleness class
  of bug we already hit with the `Claude.app` casing.
- **Permissions.** The extension needs host permissions for each site origin;
  users must trust it reads those pages. Keep it read-only and local-only
  (data goes only to `127.0.0.1`), document that clearly.
- **Only covers tabs that are open.** No open tab → no task, same as today.

## Recommendation

Worth doing, but build the **`POST /ingest` endpoint first** (it's useful on its
own — any script/shortcut could push a task), ship **one** site as a proof, then
expand. Treat per-site selectors as maintained code with fixtures, not
fire-and-forget.
