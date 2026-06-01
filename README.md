# TaskHub for StickS3

A tiny M5StickS3 dashboard for monitoring local AI agent tasks from your Mac.

TaskHub for StickS3 runs a local Mac hub that reads task metadata from AI tools
you already use, then sends a compact task list to an M5StickS3 over Wi-Fi. The
device can show running/recent/done states, token or turn usage when available,
and open the source app on the Mac with BtnA.

## Supported Sources

- Codex
- Claude / Claude Code
- OpenClaw
- Manus
- Perplexity

Support is intentionally local-first. The Mac hub reads local metadata, logs, or
app storage where available. It does not send your task data to a cloud service.

## Hardware

- M5StickS3
- macOS machine on the same Wi-Fi network
- USB cable for the first firmware flash

## Quick Start

Install or repair the Mac hub:

```bash
./host/install_task_hub.sh
```

Configure the firmware:

```bash
cp firmware/task_monitor/secrets.h.example firmware/task_monitor/secrets.h
```

Edit `firmware/task_monitor/secrets.h` and set:

- `WIFI_SSID`
- `WIFI_PASSWORD`
- `DEVICE_TOKEN`

The `DEVICE_TOKEN` must match the token in:

```text
~/Library/Application Support/StickS3TaskHub/token
```

Build and flash the StickS3:

```bash
./firmware/flash_task_monitor.sh upload
```

After flashing, the StickS3 discovers the Mac hub over UDP, fetches the compact
task list, shows it briefly, then enters deep sleep. It wakes on button press or
every `AUTO_WAKE_SECONDS`.

## Controls

- BtnA: open the selected task's source app on the Mac
- BtnB: select the next task
- BtnB hold: refresh now

## Display Behavior

- `RUN`, `WAIT`, and `FAIL` tasks stay visible.
- Old `DONE` and `IDLE` tasks are hidden on the StickS3 after 10 minutes.
- Old `REC` tasks are hidden after 1 hour.
- This only affects the StickS3 display. The Mac hub and source apps are not
  modified.

## Privacy

TaskHub is designed to run on your LAN.

- The StickS3 talks only to your Mac hub.
- The hub keeps task collection local.
- Firmware secrets are stored in `secrets.h`, which is gitignored.
- Auth tokens and message bodies are not returned by the StickS3 API.
- Some adapters inspect local app metadata or storage, depending on what each
  AI app exposes locally.

## Repository Layout

```text
firmware/task_monitor/   StickS3 firmware
host/task_hub.py         Local Mac hub
host/install_task_hub.sh LaunchAgent installer/repair script
host/README.md           Hub development and diagnostic notes
```

## Requirements

- macOS
- Python 3
- `arduino-cli`
- ESP32 Arduino core
- M5Unified and ArduinoJson libraries
- Node.js is optional but recommended for Manus local LevelDB parsing

## Release

Current release target: `v1.0.0`.

See [CHANGELOG.md](CHANGELOG.md) for release notes.
