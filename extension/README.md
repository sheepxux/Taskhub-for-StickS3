# TaskHub Web Bridge (Chrome/Edge extension)

Browser-based AI tools (Gemini, Lovable, Perplexity) keep task data on their
servers, so the Mac Host can't read a real task title locally — it only sees the
app/browser as "active". This extension runs inside those tabs, reads the
conversation/project title from the page, and pushes it to your local TaskHub
Host (`POST /ingest`). The title then shows up on your StickS3 like any other
source.

**Local-only:** the extension talks to `http://127.0.0.1:5577` (your Host) and
nowhere else. It reads only the open tab's title and a "generating" indicator.
The options page pins the port to `5577` and the Host to `127.0.0.1`/`localhost`
to match the extension's scoped permissions — only the device token is editable.

## Install (load unpacked)

1. Make sure the Host is running (`curl http://127.0.0.1:5577/health`).
2. Chrome/Edge → `chrome://extensions` → enable **Developer mode**.
3. **Load unpacked** → select this `extension/` folder.
4. Click the extension's **Details → Extension options** (or the toolbar icon)
   and set the **device token** to match
   `~/Library/Application Support/StickS3TaskHub/token`. Click **Test
   connection** — it should say `Connected ✓`.
5. Open Gemini / Lovable / Perplexity. Within a few seconds the tab's task
   appears in `/tasks` and on the StickS3.

## How it behaves

- One task per open tab (`id = ext-<source>-<tabId>`); refreshing replaces it.
- While a tab is open, a 30s heartbeat keeps the task alive (Host TTL is 60s).
- Close the tab → the heartbeat stops → the task ages out of the Host on its own.
- Only the **foreground** tab reports, to avoid a wall of background tabs.
- Status is `running` when a Stop/▢ generating control is visible, else `recent`.

## Supported sites

| Site | Title source | Notes |
| --- | --- | --- |
| Gemini | selected conversation, else tab title | |
| Lovable | header project name, else tab title | |
| Perplexity | thread heading, else tab title | complements the Host's local Perplexity adapter |

## Caveats (honest)

- **Selectors are best-effort.** Each site can change its DOM; the per-site
  `getTitle()` selectors may then return nothing and the bridge falls back to the
  cleaned `document.title` (still a real title most of the time). If titles look
  wrong after a site redesign, update `content/<site>.js`.
- **Chrome/Edge (MV3) only.** Safari needs a separate Web Extension wrapper.
- Adding a site = a new `content/<site>.js` (copy an existing one, set `source`
  and `getTitle`) plus a `content_scripts` entry and host permission in
  `manifest.json`.
