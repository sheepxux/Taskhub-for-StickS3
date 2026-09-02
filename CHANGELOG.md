# Changelog

## Unreleased

### Added

- **Terminal-free first run for M5Burner users.** The public firmware no longer
  boots into a `USB Setup` screen that needs `provision_sticks3.sh`:
  - *Wi-Fi captive portal*: with no Wi-Fi stored, the Stick opens a hotspot
    `TaskHub-XXXX` (QR on screen), a phone that joins it gets a page listing
    nearby networks; the choice, password and UI language are saved to NVS
    (`wifi_saved`). iOS/Android/Windows captive-portal probes are redirected so
    the page pops up by itself; `192.168.4.1` works as a fallback.
  - *Code pairing*: once on Wi-Fi with no token, the Stick finds the Host via
    UDP discovery (`pair: true`), shows a 4-digit code (also on the phone page)
    and polls `POST /pair`. The Host hands the token out only after that code
    is approved on the Mac through the loopback-only `/pair/approve`. Wrong
    codes are rejected, requests expire after 5 min, and a code rolled by a
    device reboot invalidates a previous approval. Hold **A** for 3s on the
    pairing screen to redo Wi-Fi; both buttons at power-on still wipe all.
  - `host/taskhub_pair.py`: Mac-side prompt (terminal or `--gui` osascript
    dialogs, reads `/dev/tty` so it works under `curl | bash`).
  - `scripts/install.sh`: one-line Host installer for a fresh Mac (tarball
    download, no git needed, detects missing Command Line Tools) that ends by
    asking for the pairing code.
  - macOS `.pkg`: ships `taskhub_pair.py`, adds `/usr/local/bin/taskhub-pair`,
    `TaskHub Host.app` now opens the pairing dialog (or diagnostics), and
    `postinstall` launches it so the installer ends on "type the code".
  - `packaging/m5burner/`: publishing guide, listing text (EN/ZH) and a
    generated cover; `build_m5burner_public.sh` now writes
    `dist/m5burner/TaskHub-StickS3-v<version>.bin` + SHA-256 and refuses to
    publish a binary that contains any string from the local `secrets.h`.
  - Firmware reports `TASKHUB_FW_VERSION` (2.3.0) when pairing; USB serial
    status now includes `stage`, `wifi_configured`, `ap_ssid`, `pair_code`.
- README (EN/ZH) Quick Start is now the 3-step end-user path (M5Burner → phone
  Wi-Fi → one command); source/USB/manual paths moved to INSTALL.md.

- New Host adapters for three more agent families:
  - **Cline / Roo Code / Kilo Code** (`ClineAdapter`): reads each host
    editor's `globalStorage/<extension>/tasks/<id>/ui_messages.json` across
    VS Code, VS Code Insiders, Cursor, Windsurf, VSCodium, Antigravity and
    Trae. An unanswered `ask` is an exact WAIT (tool/command approval,
    follow-up question, plan review), `api_req_started` is RUN,
    `completion_result` is DONE, `api_req_failed`/`error` is FAIL, and token
    counts + cost come from the request metadata. Opens the host editor via
    the new `app-name` open action.
  - **Gemini CLI / Qwen Code** (`GeminiCliAdapter`): parses
    `tmp/<projectHash>/chats/session-*.json` under `~/.gemini` / `~/.qwen`,
    maps the project hash back to a folder via `projects.json`, and derives
    RUN/WAIT (tool `awaiting_approval`)/DONE/FAIL from the last message.
  - **GitHub Copilot CLI** (`CopilotCliAdapter`): parses
    `~/.copilot/session-state/<id>/events.jsonl` + `workspace.yaml`.
  Both CLI adapters gate RUN on a live CLI process (`cli_process_running`),
  falling back to a 2-minute freshness window when the process is not
  visible. All three are fixture-tested (`host/tests/test_extension_and_cli_adapters.py`);
  the formats were reconstructed from public sources rather than captured
  on this machine, so real-world reports are welcome.
- StickS3 source icons and colors for Cline-family, Copilot and Qwen rows
  (Gemini CLI reuses the Gemini mark).
- `install_task_hub.sh` now waits for `/health`, pulls `/diagnostics.json`
  and prints any adapter blocked by macOS TCC together with the exact Python
  binary launchd runs (the one to add under Full Disk Access).
- StickS3 FAIL alert: a task entering `fail` now wakes the screen and plays a
  falling two-note buzz, distinct from the WAIT double beep and the DONE
  chime. Tunable via `ALERT_ON_FAIL` / `ALERT_FAIL_HZ`.
- The top-bar battery icon shows a bolt (and a blue level bar) while the
  StickS3 is charging over USB.

### Fixed

- The Host advertised the wrong address when a VPN/proxy was active.
  `local_ip_hint()` used the connect-to-8.8.8.8 trick, which returns the tunnel
  IP (`198.18.0.1` for Clash/Surge fake-IP, `100.x` for Tailscale); a Stick on
  the LAN cannot reach that. Discovery and pairing now answer with the address
  of the interface facing the requesting device (`local_ip_for_peer`), and
  `/health` falls back to the default-route LAN interface, skipping tunnel and
  CGNAT ranges.
- Host cold start no longer makes the Stick's first fetch time out. The Host
  begins filling its task cache as soon as it is listening (`Hub.warm_cache`),
  a Stick request that arrives while that first scan runs waits for it
  (up to `TASK_HUB_COLD_START_WAIT_MS`, default 6s, under the Stick's 8s
  HTTP timeout) instead of launching a second full scan, and if the scan is
  still going the Host answers empty with `syncing: true` so the Stick
  re-polls a moment later. A cold Codex/Claude scan on a busy machine is
  ~8-10s; the warm path is ~50ms.
- The TCC hint now mentions the other common cause: an agent's data folder
  (e.g. `~/.cursor`) symlinked onto an external volume, which a launchd
  process cannot read without Full Disk Access.
- StickS3 alerts are now edge-triggered per task instead of on the global
  "any WAIT" / "any RUN" flags. Previously a second session entering WAIT
  while another was already waiting never rang, and a task finishing while a
  WAIT was pending never chimed. The firmware keeps a per-task status table in
  RTC memory (survives deep sleep), diffs each refresh against it, plays at
  most one alert per refresh (WAIT > FAIL > DONE), and jumps the screen to the
  task that changed.
- macOS TCC app-data denials (common for the launchd-run Host on macOS 15+)
  no longer read as "adapter ok, 0 tasks". Cursor's global-storage DB and
  `~/.cursor/projects` reads now record permission failures, `/diagnostics`
  flags the adapter with a Full Disk Access error, and the on-device Cursor
  fallback row says "needs Full Disk Access" instead of "no recent agent
  session". INSTALL.md documents the grant.
- `install_task_hub.sh` and `install_whisper_server.sh` now copy whisper
  models onto the internal disk instead of symlinking into the repo. A repo
  on an external volume (unmounted at login) or under a TCC-protected folder
  like `~/Desktop` made launchd's whisper-server crash-loop on a model it
  could not open, flooding its log.
- LaunchAgent logs (`task_hub.log`, `whisper-server.log`) are trimmed at
  startup once they exceed ~10MB (recent tail kept in `*.prev`), and the
  whisper agent gained a 30s `ThrottleInterval` so a crash loop cannot flood
  the disk.

### Changed

- Codex rollout scans are now incremental: the parser memoises its state and
  file offset per session, so a growing rollout only folds in appended lines.
  Active Codex sessions in the hundreds of MB used to be re-parsed from byte
  zero on every `/tasks` refresh, costing multiple seconds per request; the
  same refresh now costs milliseconds.

### Added

- Added a detailed Cursor adapter. It reads Cursor's composer index
  (`state.vscdb`) and the per-project agent transcripts under
  `~/.cursor/projects` for chat titles, workspace folders, turn state,
  real context-token usage, and DONE/FAIL turn endings. WAIT (pending
  approval or an unanswered AskQuestion) is confirmed by a live-counter
  stall, so an agent that is still generating is never mis-flagged while
  a genuinely blocked one alerts within about two minutes. BtnB opens the
  Cursor app and voice input can target it.

### Changed

- Claude Code plan approvals (`ExitPlanMode`) now count as WAIT, same as
  `AskUserQuestion` — Claude Code 2.x stops and waits for the user to approve
  the plan, which is attention, not RUN.
- Codex state-DB reads now probe the newer optional columns (`name`,
  `first_user_message`) added around 0.146 and fall back to them when
  `title`/`preview` are empty, while remaining compatible with older schemas.

## v2.2.0 — 2026-07-19

### Added

- Added a detailed WorkBuddy adapter that reads local session transcripts,
  task titles, working folders, pending tool calls, explicit user questions,
  completion state, and token data when present.
- Added Kimi local conversation-state tracking with renderer CPU and visible
  controls as fallbacks, plus a conservative Grok desktop-app fallback. An
  open app alone reports `REC`.
- Extended the Chrome/Edge Web Bridge with Kimi and Grok conversation-title
  extraction and generation-state reporting.
- Added 12x12 StickS3 source icons for Kimi, WorkBuddy, and Grok.
- Added a secret-free macOS Host `.pkg` build pipeline with a diagnostics app,
  random per-user token creation, LaunchAgent registration, optional Developer
  ID signing/notarization, and a `taskhub-provision` command.
- Added `setup.sh --rotate-token`, which writes a newly generated token to the
  connected StickS3 before replacing the Host token, avoiding a half-rotated
  installation when USB provisioning fails.

### Changed

- WorkBuddy now clears unmatched calls from older resolved turns and expires
  stale questions, preventing completed sessions from remaining in `WAIT`.
- Updated the StickS3 WorkBuddy icon from generic green to its teal brand color.
- Local adapter scans now run concurrently, and OpenClaw's slow CLI fallback is
  opt-in. Warm full scans on the reference Mac dropped from several seconds to
  roughly 500ms while preserving the StickS3 stale-while-revalidate fast path.
- New Host installs no longer fall back to the public `dev-token`; generated
  token files are permissioned to the current user, and provisioning passes
  Wi-Fi/token values through the environment instead of process arguments.
- Public M5Burner firmware and `secrets.h.example` now compile with an empty
  token until setup generates a random one, so no shared default credential is
  shipped in release binaries.

- Claude Code now discovers recent CLI transcripts directly, including custom
  conversation titles and working folders, even when no Claude Desktop session
  metadata record exists.
- Codex detection now recognizes the ChatGPT-embedded Codex runtime and the
  current app-server `turn_started`, `turn_completed`, `turn_interrupted`, and
  `turn_failed` events while retaining compatibility with older rollout events.
- WorkBuddy task titles now use the transcript's dedicated `ai-title` event
  instead of falling back to date-based working-directory names.
- Kimi now cross-checks persisted conversation status with the app daemon's
  active-turn state, so a completed task cannot stay pinned in `RUN`; renderer
  CPU no longer creates a running state by itself.

- StickS3 now shows the last successful compact task snapshot immediately after
  deep-sleep wake, then replaces it with fresh Host data when Wi-Fi sync
  completes. This makes screen-off wake feel much faster without keeping Wi-Fi
  alive.
- The StickS3 `/tasks?format=stick` Host endpoint now uses a stale-while-
  revalidate path: if a recent task snapshot exists, the device gets it in a
  few milliseconds while the Host refreshes Claude/Codex/browser adapters in
  the background. Firmware follows up automatically when the Host reports that
  a background sync is still in progress.
- Voice recording buffer allocation is now lazy, so ordinary wake-to-check
  flows no longer allocate the large PSRAM audio buffer during boot.
- Eco mode is now the default firmware power profile: lower screen brightness,
  shorter screen-on windows, slower active refresh while awake, 120-second
  active wake cadence, and finite WAIT attention hold before sleeping again.
- Low-battery behavior is more aggressive: dimmer backlight, 20-minute wake
  cadence, and 45-second WAIT attention hold before returning to sleep.

## v2.1.0 — 2026-07-02

### Added

- `/diagnostics` local Host page with adapter health, task status counts,
  voice-mode readiness, Accessibility permission, cache sizes, and peer
  discovery summary.
- `/diagnostics.json` for machine-readable support checks. The snapshot omits
  token values and task titles so it can be shared more safely during debugging.
- Regression tests for diagnostics status normalization, home-path redaction,
  and privacy-safe snapshots.

### Changed

- Documentation now points users to `/diagnostics` as the first stop when task
  status, voice input, or multi-device discovery looks wrong.

## v2.0.4 — 2026-07-02

### Fixed

- Improved voice input injection reliability by waiting longer for the target
  app to activate, letting Electron/WebView apps consume the paste before the
  clipboard can be restored, keeping the transcript on the clipboard by default
  for reliable paste delivery, and reporting injection failures without logging
  the spoken transcript.
- The macOS Host LaunchAgent now runs in the Aqua interactive session so
  voice-mode paste/send keystrokes actually reach the target app.
- StickS3 now shows `Type failed` / `输入失败` when transcription succeeds but
  macOS input injection fails, instead of implying the text was typed.
- The first boot task fetch now uses the same startup retry window as manual
  refreshes, reducing false `Cannot read tasks` screens while the Host warms up.
- Verified Codex `0.142.5` and the Claude Desktop bundled Claude Code
  `2.1.197` storage/process shape.

## v2.0.3 — 2026-06-26

### Fixed

- Updated the local Claude Code CLI compatibility baseline to `2.1.193` and
  verified Codex remains current at `0.142.2`.
- Added a Claude Desktop fallback row so the Host still shows Claude as idle
  when the app is open but no local Claude Code task metadata is available.
- Increased the StickS3 HTTP startup window and added a short boot retry loop,
  preventing a false `Cannot read tasks` screen while the Mac Host warms its
  first task scan.
- Relaxed a Codex adapter timestamp boundary test that could fail on exact
  second rollover without indicating a product regression.

## v2.0.2 — 2026-06-20

### Added

- StickS3 firmware UI language setting: fixed device text defaults to English
  and can be switched to Chinese with `TASKHUB_LANG=zh`, `--lang zh`, or USB
  provisioning payload field `lang`.
- StickS3 voice mode now auto-sends by default after pasting the transcript.
  `VOICE_AUTO_SEND`, `TASKHUB_VOICE_SEND`, and `--voice-send off` keep a
  paste-only review mode available.

### Fixed

- Claude Code local-agent sessions created by newer Claude builds are now read
  from their per-session `.claude/projects` transcript root, restoring
  RUN/WAIT/DONE instead of falling back to REC.
- Voice input fallback now resolves the local whisper model after the Host is
  installed into `~/Library/Application Support/StickS3TaskHub`, so `/voice`
  no longer fails immediately when the resident whisper-server is not already
  running.
- Codex thread discovery now reads both current Codex state DB locations
  (`~/.codex/state_5.sqlite` and `~/.codex/sqlite/state_5.sqlite`), restoring
  newly opened Codex tasks after Codex's local storage migration.
- OpenClaw heartbeat/default direct sessions are hidden from the StickS3 list,
  and failed OpenClaw rows expire after a short attention window instead of
  staying pinned forever when there is no real task.
- Codex row timestamps preserve the real session activity time, so old tasks no
  longer appear as freshly updated just because an index or file mtime changed.

### Changed

- Refined the StickS3 task UI with a cleaner top bar, status pills, AI source
  icons, stronger selected-task hierarchy, and compact progress rails in both
  landscape and portrait layouts.
- Host regression tests now cover Codex's dual SQLite paths, stale Codex mtime
  handling, and OpenClaw idle/heartbeat filtering.

## v2.0.0 — 2026-06-05

Major update: **voice mode** — hold-to-talk on the StickS3 transcribes Mandarin
or English with local whisper and types it into the AI app you opened — plus the
M5Burner public-firmware provisioning path.

### Added

- M5Burner/public firmware provisioning path: `TASKHUB_PUBLIC_BUILD=1` ignores
  local `secrets.h`, boots into `USB Setup`, accepts a one-line USB serial JSON
  config, stores Wi-Fi/Host/token in NVS, and restarts into normal operation.
- `scripts/provision_sticks3.sh` configures a public-build StickS3 over USB
  using the installed Mac Host token and auto-detected LAN IP.
- `firmware/build_m5burner_public.sh` builds a public artifact set without
  compiling private Wi-Fi credentials, tokens, or local build paths into the
  binary.
- `scripts/setup.sh --provision` wires Host install + USB device provisioning
  into the normal setup helper.
- **Voice mode**: hold **BtnB** on the StickS3 to record (M5.Mic → PSRAM, 16 kHz),
  release to POST the clip to the Host's `POST /voice`, which transcribes it with
  local whisper.cpp (`large-v3-turbo-q5_0`, Mandarin + English, Simplified
  output) and pastes the text into the app the task belongs to. Deterministic
  window targeting (`?task=<id>` resolves the task's app; `?app=<bundle>` /
  `?source=<name>` for Claude, Codex, Manus, Perplexity), optional `?enter=1` to
  auto-send. New `host/taskhub_voice.py`, `host/run_whisper_server.sh`, and
  `host/install_whisper_server.sh` (resident whisper-server LaunchAgent). Audio
  and transcription stay on the LAN / local machine.

### Changed

- CI now compiles both the normal firmware path and the public M5Burner path.

## v1.3.0 — 2026-06-05

### Added

- IMU-based **auto-rotation**: the StickS3 reads its gravity vector and rotates the display to match how it's held (landscape ↔ portrait, all four ways), with a stability window to avoid flicker and a deadzone when lying flat. Tunable via `ENABLE_AUTO_ROTATE` / `ROTATE_*` / `ROT_*` defines; only active while the screen is awake.
- **Portrait multi-task list**: in portrait the screen shows a compact vertical list of several tasks (status bar + source + title per row) instead of stretching the single landscape card. Hint/boot screens now position text proportionally so they read well in both orientations.

## v1.2.1 — 2026-06-05

### Changed

- Swapped the StickS3 button layout: **BtnB** opens the selected task's source app; **BtnA** cycles to the next task, and **hold BtnA** refreshes now.

## v1.2.0 — 2026-06-05

On-device WAIT/DONE alerts, a clearer turn-completion state model, the browser
web bridge + `POST /ingest`, a host config split, and a regression suite with CI.

### Added

- `POST /ingest` endpoint: push external tasks (single or batch) into the Host; each expires after a per-task TTL so closed sources age out on their own. Backed by a thread-safe `ExternalTaskAdapter`.
- `extension/` — Chrome/Edge MV3 "TaskHub Web Bridge" that reads Gemini/Lovable/Perplexity tab titles and pushes them to `POST /ingest`, so browser AI tasks show real titles on the StickS3.
- `host/tests/` — stdlib unittest regression suite (23 cases) covering status derivation, WAIT detection, case-insensitive process matching, token accounting, scan memoisation, and `/ingest` validation/expiry.
- `.github/workflows/ci.yml` — runs the host tests and compiles the firmware on every push/PR.
- `docs/browser-extension-plan.md` — feasibility/workload writeup for the web-source title path.
- `docs/render_screens.py` and pixel-accurate 240x135 screen renders in the README.
- `INSTALL.md` with full first-run, manual, multi-Mac, update, uninstall, and troubleshooting instructions.
- `scripts/setup.sh` for conservative first-run setup, token sync, firmware secret generation, optional Arduino dependency install, compile, and upload.
- `README.zh-CN.md` as a Chinese project landing page.
- Edge-triggered StickS3 alert when a task first enters `WAIT`: wakes the screen and plays a short double beep via the speaker (tunable/silenceable through `ALERT_*` defines). Fires once per empty→WAIT transition, persisted in RTC so a wait first seen on a timer wake still alerts.
- Edge-triggered StickS3 `DONE` chime when a running task finishes, using a softer rising tone.

### Changed

- README now links to the Chinese version and the installation guide, and uses the setup helper in Quick Start.
- Direct manual Host runs now bind to `127.0.0.1` by default; installer-managed LAN mode still passes `--bind 0.0.0.0` explicitly for StickS3 access.
- StickS3 active-state wake cadence tightened from 180s to 60s, capping how long a freshly-appeared `WAIT` can go unnoticed while deep-sleeping (a WAIT almost always follows a running task). Battery-tunable via `ACTIVE_WAKE_SECONDS`.
- Host runtime configuration moved into `host/taskhub_config.py`, reducing the size of the main Host entrypoint without changing its external API.
- Host installer now copies `taskhub_*.py` helper modules alongside `task_hub.py`.
- StickS3 alert defaults retuned for desk use: lower volume, shorter WAIT double beep, and softer DONE chime.
- A completed Claude turn now reports as green `DONE` for a short window (`TASK_HUB_CLAUDE_DONE_WINDOW_MS`, default 5 min) before settling to `REC`, so "the turn just finished" is a distinct state the StickS3 can show and chime on.

### Fixed

- `WAIT` is now driven only by an explicit, unanswered AskUserQuestion / `request_user_input` tool call. A turn that merely ends with question-like prose is treated as a completed turn (`DONE`) — the old text heuristic produced false WAITs on chatty endings. The model is: turn ends → `DONE`; an explicit pending question → `WAIT`; answering it clears the `WAIT`.

- Claude/Codex transcript memoisation now uses a bounded LRU cache instead of growing without eviction, guarded by a lock so concurrent `/tasks` threads can't race its eviction step.
- Claude Code running-session detection was case-sensitive on `/Claude.app/` and missed the lowercase `claude.app` binary; now case-insensitive.
- Claude Code `WAIT` detection now ignores stale human-input tool requests once a later terminal assistant event exists.
- StickS3 clears stale `WAIT` rows after refresh failures instead of keeping an old yellow state indefinitely.
- Extension local Host permissions are scoped to port `5577`, and visible Stop-button detection no longer treats missing layout boxes as visible.
- Extension options now pin the port to `5577` (read-only) and restrict Host to `127.0.0.1`/`localhost`, matching the scoped permissions so a stray value can't be silently blocked at fetch time.
- Adapter exceptions now print tracebacks to stderr before surfacing a failed task row.

### Performance

- Memoised the Claude and Codex transcript scans by `(path, mtime, size)`; unchanged sessions return in O(1) instead of re-parsing on each `/tasks` request.
- Firmware caches the AP BSSID/channel in RTC memory for sub-second Wi-Fi reconnect after deep sleep, and uses `WIFI_PS_MAX_MODEM`.

## v1.1.1

Multi-device diagnostics release.

### Added

- `/peers` HTML page for LAN Host discovery and fetch diagnostics.
- `/peers.json` for machine-readable peer status, task counts, latency, and last errors.
- `/debug/lovable` for Lovable app/browser status inputs, including renderer CPU and running-state basis.

### Changed

- Peer aggregation now records successful empty task lists separately from fetch failures.
- Health and peer diagnostics report TaskHub version `1.1.1`.
- README rebuilt as a public project landing page with setup, source accuracy,
  diagnostics, privacy, troubleshooting, and roadmap sections.

## v1.1.0

Multi-device TaskHub release.

### Added

- LAN peer discovery between authorized TaskHub Hosts using the existing UDP discovery channel.
- Host aggregation of peer `/tasks?scope=local` task lists so one StickS3 can show tasks across Macs.
- Remote task open forwarding: BtnA on a remote task asks the original Mac Host to open the source app.
- Device identity fields (`device_id`, `device_name`, `device_label`) in health, full task, and compact StickS3 payloads.
- StickS3 display support for `Source@Device` labels.
- Lovable desktop app detection, renderer-CPU running heuristic, and orange-red Lovable icon.

### Changed

- `/tasks` now returns aggregated local + peer tasks by default; use `scope=local` for only this Mac.
- The compact StickS3 payload includes `d` for the source device label.

## v1.0.1

Documentation polish release.

### Changed

- Refined the GitHub README into a project landing page.
- Added architecture and status-flow diagrams to explain the TaskHub pipeline.
- Expanded setup, controls, supported sources, privacy, and release notes.

## v1.0.0

Initial public release of TaskHub for StickS3.

### Added

- StickS3 firmware for compact AI task monitoring.
- Local Mac Task Hub HTTP API and UDP discovery.
- LaunchAgent installer/repair script for the Mac hub.
- Codex task, title, folder, turn, token, and running-state detection.
- Claude / Claude Code transcript turn tracking and usage display.
- OpenClaw local task/session tracking.
- Manus local session metadata and usage counters from app storage.
- Perplexity local activity indicator from app preferences/cache signals.
- BtnA open-source-app action.
- BtnB task navigation and hold-to-refresh.
- Display-only hiding of stale tasks on the StickS3.
- Battery-first deep sleep mode with periodic wake refresh.

### Known Limits

- Perplexity does not expose a stable local task transcript, so TaskHub reports
  app-local activity rather than exact task titles.
- Manus status code mapping is best-effort based on observed local metadata.
- Deep sleep is enabled by default; set `ENABLE_DEEP_SLEEP` to `0` in
  `secrets.h` while debugging the screen or network loop.
