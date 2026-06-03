# Changelog

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
