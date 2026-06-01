# Changelog

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
