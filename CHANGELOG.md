# Changelog

## Unreleased

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
- Swapped the StickS3 button layout: **BtnB** opens the selected task's source app; **BtnA** cycles to the next task, and **hold BtnA** refreshes now.
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
