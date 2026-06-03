# Changelog

## Unreleased

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

### Changed

- README now links to the Chinese version and the installation guide, and uses the setup helper in Quick Start.

### Fixed

- Claude Code running-session detection was case-sensitive on `/Claude.app/` and missed the lowercase `claude.app` binary; now case-insensitive.

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
