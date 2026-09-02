# TaskHub for StickS3

A pocket hardware dashboard for AI agent work across your Macs.

[简体中文](README.zh-CN.md) | [Installation](INSTALL.md)

[![CI](https://github.com/sheepxux/Taskhub-for-StickS3/actions/workflows/ci.yml/badge.svg)](https://github.com/sheepxux/Taskhub-for-StickS3/actions/workflows/ci.yml)
[![Release](https://img.shields.io/badge/release-v2.2.0-111827)](CHANGELOG.md)
[![Hardware](https://img.shields.io/badge/hardware-M5StickS3-2563eb)](firmware/task_monitor)
[![Host](https://img.shields.io/badge/host-macOS-0f766e)](host)
[![Local First](https://img.shields.io/badge/local--first-yes-16a34a)](#privacy-and-security)
[![License](https://img.shields.io/badge/license-MIT-374151)](LICENSE)

TaskHub for StickS3 turns an M5StickS3 into a tiny always-nearby status screen
for AI coding agents, desktop AI apps, and browser-based agent tools. A local
Mac Host reads task metadata from sources such as Codex, Claude Code, Cursor,
Cline / Roo Code / Kilo Code, Gemini CLI, Qwen Code, GitHub Copilot CLI,
OpenClaw, Manus, Perplexity, Gemini, Lovable, Kimi, WorkBuddy, and Grok, discovers other authorized TaskHub Hosts
on the same LAN, then sends a compact task list to the StickS3 over Wi-Fi.

The device shows which task is running, which one needs your input, what
recently finished, and token or turn usage when the source exposes it. BtnB can
open the source app on the Mac, BtnA switches tasks, and the firmware sleeps
between refreshes so the small StickS3 battery remains usable.

## Why It Exists

AI agents are easy to start and easy to forget. TaskHub gives them a physical
status surface:

- See active AI work without switching windows.
- Catch Codex or Claude Code prompts when they need a reply.
- Track tasks across more than one Mac on the same Wi-Fi.
- Keep task state local instead of sending agent metadata to a cloud service.
- Use a small, inexpensive device instead of dedicating another monitor.

## Screens

Pixel-accurate renders of what the StickS3 draws at its native 240x135
resolution. Regenerate them with `python3 docs/render_screens.py`.

<table>
  <tr>
    <td align="center"><img src="docs/screen-boot.png" width="320" alt="Boot logo screen"/><br/><sub><b>BOOT</b> - Wi-Fi sync splash</sub></td>
    <td align="center"><img src="docs/screen-wake.png" width="320" alt="Wake reconnect screen"/><br/><sub><b>WAKE</b> - reconnect after sleep</sub></td>
  </tr>
  <tr>
    <td align="center"><img src="docs/screen-run.png" width="320" alt="RUN screen"/><br/><sub><b>RUN</b> - active agent turn</sub></td>
    <td align="center"><img src="docs/screen-wait.png" width="320" alt="WAIT screen"/><br/><sub><b>WAIT</b> - needs your input</sub></td>
  </tr>
  <tr>
    <td align="center"><img src="docs/screen-fail.png" width="320" alt="FAIL screen"/><br/><sub><b>FAIL</b> - error or attention</sub></td>
    <td align="center"><img src="docs/screen-done.png" width="320" alt="DONE screen"/><br/><sub><b>DONE</b> - finished task</sub></td>
  </tr>
  <tr>
    <td align="center"><img src="docs/screen-empty.png" width="320" alt="No tasks screen"/><br/><sub>No tasks - idle hub</sub></td>
    <td align="center"><img src="docs/screen-error.png" width="320" alt="Hub unreachable screen"/><br/><sub>Hub unreachable - Wi-Fi lost</sub></td>
  </tr>
</table>

## Current Release

`v2.2.0` is the current public build for developers and hardware makers. The
core pipeline is working: Mac Host, StickS3 firmware, Wi-Fi discovery, compact
task display, button actions, deep sleep, voice input, and LAN multi-device
aggregation.

Some AI apps expose rich local task logs; others expose only app activity or
visible browser UI. TaskHub is explicit about that distinction so users know
when a row is exact task tracking versus best-effort local signal detection.

## Feature Matrix

| Area | Status | Notes |
| --- | --- | --- |
| StickS3 firmware | Ready | Native 240x135 UI, buttons, Wi-Fi discovery, deep sleep |
| M5Burner public firmware | Ready | No secrets in the binary; first boot opens a Wi-Fi captive portal, then pairs with the Host by a 4-digit code |
| macOS Host | Ready | LaunchAgent installer, local HTTP API, UDP discovery |
| macOS Host package | Preview | Builds a secret-free `.pkg`; public distribution still requires Developer ID signing/notarization |
| Host diagnostics | Ready | `/diagnostics` checks adapters, voice mode, local permissions, caches, and peers without exposing tokens or task titles |
| Multi-Mac aggregation | Ready | Authorized Hosts discover peers and merge task lists |
| BtnB open source | Ready | Opens local source app; remote tasks forward to the origin Mac |
| WAIT attention mode | Ready | Wakes and holds the display briefly when input is needed, then sleeps and keeps checking |
| WAIT alert | Ready | Per-task edge: screen wake + short double beep each time a task enters WAIT, even while another is already waiting (`ALERT_*` tunable) |
| DONE alert | Ready | Per-task edge: softer rising chime when a running task finishes |
| FAIL alert | Ready | Per-task edge: falling two-note buzz when a task errors out |
| Battery-aware operation | Ready | Sleeps by default, short timer-wake screen time, low brightness, charging bolt on the battery icon |
| Auto-rotation | Ready | IMU gravity rotates the screen to match how it's held; portrait shows a multi-task list (`ROTATE_*` tunable) |
| Voice input | Ready | Hold BtnB to dictate (Mandarin/English) → local whisper.cpp → text pasted and sent in the selected task's app (`POST /voice?enter=1`) |
| Codex adapter | Detailed | Tracks title, folder, turns, token usage, running/wait state |
| Claude Code adapter | Detailed | Tracks transcript turn state, prompts, plan approvals, usage, resume process |
| Cursor adapter | Detailed | Composer title/folder, RUN via live counters, pending-approval WAIT, context token usage |
| OpenClaw adapter | Detailed | Reads local task/session stores |
| Manus adapter | Best effort | Reads local app storage and usage counters when available |
| Perplexity adapter | Activity | Local app/browser activity; exact Perplexity Computer tasks are not guaranteed |
| Gemini adapter | Activity | App/web activity and visible browser title when exposed |
| Lovable adapter | Activity | App/browser activity, renderer CPU, visible generation controls |
| Kimi adapter | Activity | Local conversation status plus daemon active-turn state and web title; open app alone is not RUN |
| WorkBuddy adapter | Detailed | Local session title, tool calls, RUN/WAIT/DONE, folder, token data when present |
| Cline / Roo Code / Kilo Code adapter | Detailed | Extension task transcripts inside VS Code, Cursor, Windsurf, VSCodium, Antigravity, Trae; unanswered approval/question is an exact WAIT, request in flight is RUN, completion is DONE, token/cost usage |
| Gemini CLI / Qwen Code adapter | Detailed | Chat recordings under `~/.gemini` / `~/.qwen`: RUN while the model answers or tools execute, WAIT on a tool awaiting approval, DONE on a finished reply, folder from `projects.json` (fixture-verified; real-world reports welcome) |
| GitHub Copilot CLI adapter | Best effort | `~/.copilot/session-state/*/events.jsonl` turn events: RUN/DONE/FAIL, WAIT on a pending permission event, workspace summary as title (fixture-verified; real-world reports welcome) |
| Grok adapter | Activity | Web/Safari app title and visible Stop control; open app alone is not RUN |
| Browser web bridge | Optional | `extension/` (Chrome/Edge) reads Gemini/Grok/Kimi/Lovable/Perplexity tab titles and pushes them via `POST /ingest` |
| External push API | Ready | `POST /ingest` accepts tasks from any local script; entries expire on a TTL |

## Browser Web Bridge (optional)

Browser-based AI tools keep task data on their servers, so the Host can only see
them as "active". The optional Chrome/Edge extension in [`extension/`](extension)
reads the open Gemini / Grok / Kimi / Lovable / Perplexity tab's title and pushes it to the
Host via `POST /ingest`, so those tasks show real titles on the StickS3. It is
local-only (talks to `127.0.0.1`) and best-effort (page selectors fall back to
the tab title). See [extension/README.md](extension/README.md) to load it.

Any script can push too:

```bash
curl -X POST -H 'X-Device-Token: <token>' http://127.0.0.1:5577/ingest \
  -d '{"source":"Manus","title":"Draft weekly report","status":"running","ttl_sec":120}'
```

## Voice Mode

Hold **BtnB** on the StickS3 to dictate into the AI app you're working with —
Mandarin or English, transcribed locally, pasted into the chat box, and sent by
default.

1. Short-press BtnB to open a task's app (brings e.g. Claude to the front).
2. **Hold BtnB** and speak; release to transcribe and send.
3. The clip is POSTed to `POST /voice`, transcribed by a resident whisper.cpp
   server (`large-v3-turbo-q5_0`, Simplified Chinese + English), and pasted into
   the task's app. `?task=<id>` targets that app deterministically, and
   `?enter=1` submits the text with Return.

Everything stays local — the audio never leaves your machine/LAN.

One-time setup:

```bash
brew install whisper-cpp
mkdir -p host/models && curl -L -o host/models/ggml-large-v3-turbo-q5_0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
./host/install_whisper_server.sh   # resident whisper-server LaunchAgent on :8080
```

Grant the Host **Accessibility** permission (System Settings → Privacy & Security →
Accessibility) so it can paste into other apps. Tunables: `TASK_HUB_WHISPER_MODEL`,
`TASK_HUB_WHISPER_LANGUAGE` (`auto`/`zh`/`en`). Device-side auto-send is on by
default; set `VOICE_AUTO_SEND 0`, `TASKHUB_VOICE_SEND=0`, or provision with
`--voice-send off` if you want paste-only review before sending. Targeting works
for Claude, Codex, Cursor, Manus, and Perplexity desktop apps.

## Status Model

TaskHub keeps the full task list on the Mac. The StickS3 applies a
display-only filter so old rows disappear from the tiny screen without deleting
anything from the computer.

| Label | Color intent | Meaning | StickS3 visibility |
| --- | --- | --- | --- |
| `RUN` | Blue | Active task or active agent turn | Always visible |
| `WAIT` | Yellow | Waiting for user input or queued attention | Always visible while awake; fires a one-shot wake + double-beep alert, then sleeps again after the attention hold |
| `FAIL` | Red | Failed or needs attention | Always visible |
| `DONE` | Green | Completed | Hidden after 10 minutes by default; a running-to-finished edge plays a softer chime |
| `REC` | White/gray | Recently active | Hidden after 1 hour by default |
| `IDLE` | Dark gray | Source is idle | Hidden after 10 minutes by default |
| `HIDDEN` | Gray/black | Display-only stale row | Not shown on device |

```mermaid
stateDiagram-v2
  [*] --> RUN: active turn
  [*] --> WAIT: prompt needs input
  RUN --> REC: turn finished recently
  WAIT --> RUN: work resumes
  RUN --> FAIL: error or aborted
  REC --> DONE: terminal state reported
  DONE --> Hidden: older than 10 minutes
  IDLE --> Hidden: older than 10 minutes
  REC --> Hidden: older than 1 hour
  FAIL --> [*]: stays visible
  WAIT --> [*]: sleeps after attention hold
  RUN --> [*]: stays visible
```

## Architecture

```mermaid
flowchart LR
  subgraph "LAN Macs"
    Host["TaskHub Host\nAggregator + HTTP API"]
    Peer["Peer TaskHub Host\nscope=local"]
    Codex["Codex\nsessions + usage"]
    Claude["Claude Code\ntranscripts + resume process"]
    Cursor["Cursor\ncomposer index + transcripts"]
    OpenClaw["OpenClaw\nlocal tasks + sessions"]
    Manus["Manus\nlocal app storage"]
    Perplexity["Perplexity\nlocal app/browser activity"]
    Gemini["Gemini\nlocal app/browser activity"]
    Lovable["Lovable\napp + browser tabs"]
    Kimi["Kimi\ndesktop + browser UI"]
    WorkBuddy["WorkBuddy\nlocal session events"]
    Grok["Grok\nSafari app + browser UI"]
  end

  subgraph "M5StickS3"
    Firmware["Task monitor firmware"]
    Screen["Compact status screen"]
    Buttons["BtnA / BtnB"]
    Sleep["Deep sleep timer"]
  end

  Peer <-->|"UDP discovery + token"| Host
  Codex --> Host
  Claude --> Host
  Cursor --> Host
  OpenClaw --> Host
  Manus --> Host
  Perplexity --> Host
  Gemini --> Host
  Lovable --> Host
  Kimi --> Host
  WorkBuddy --> Host
  Grok --> Host
  Host -- "UDP discovery response" --> Firmware
  Firmware -- "GET /tasks?format=stick" --> Host
  Firmware -- "POST /tasks/:id/open" --> Host
  Firmware --> Screen
  Buttons --> Firmware
  Firmware --> Sleep
```

## Quick Start

Two things, no terminal required for the Stick: **firmware from M5Burner**
(M5Stack's official burning tool) and the **Mac Host from GitHub**.

### 1. Burn the firmware

Open [M5Burner](https://docs.m5stack.com/en/download), search **TaskHub for
StickS3**, plug the StickS3 in over USB-C and click **Burn**.

### 2. Put the Stick on your Wi-Fi

Power it on. The screen shows a QR code and a Wi-Fi name like `TaskHub-3F2A`.
Join that network from your phone (scan the QR or pick it in Wi-Fi settings),
choose your home Wi-Fi in the page that pops up, and enter its password. The
Stick joins and shows a **4-digit pairing code**.

### 3. Install the Mac Host and pair

Paste this in Terminal (Spotlight → "Terminal"):

```bash
curl -fsSL https://raw.githubusercontent.com/sheepxux/Taskhub-for-StickS3/main/scripts/install.sh | bash
```

It installs the Host as a login item, then asks for the code on the Stick.
Type it, and the Stick restarts into your task list. From now on it finds the
Host on its own (UDP discovery on port `5578`), so your Mac's IP can change.

Pair another Stick later with `python3 ~/Library/Application\ Support/StickS3TaskHub/taskhub_pair.py`,
or open **TaskHub Host.app** if you installed the `.pkg`. Holding both buttons
while powering on wipes the Stick's Wi-Fi and token; holding **A** on the
pairing screen redoes only the Wi-Fi.

### Other install paths

- **macOS `.pkg`**: a double-click installer that ends in the pairing dialog.
  Until the package is signed with a Developer ID, macOS blocks it on first
  open (System Settings → Privacy & Security → *Open Anyway*).
- **Developers**: build and flash from source with `arduino-cli`, bake Wi-Fi
  and token into `secrets.h`, or provision over USB serial. See
  [INSTALL.md](INSTALL.md).

## Controls

| Control | Action |
| --- | --- |
| BtnB | Open the selected task's source app on the Mac |
| BtnB hold | Voice input — hold to talk, release to dictate and send in the task's app |
| BtnA | Select the next task |
| BtnA hold | Refresh immediately |

## Multi-Device Mode

Install the Mac Host on every Mac you want to include and use the same token on
each Host. Any TaskHub Host can act as the aggregator:

- Hosts announce themselves over UDP port `5578`.
- The aggregator fetches each peer's `/tasks?scope=local` list.
- Compact StickS3 rows include a short device label, such as `Codex@MBP`.
- BtnB on a remote task forwards the open request back to the Mac that owns it.

Useful environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TASK_HUB_DEVICE_NAME` | macOS hostname | Human-readable device name |
| `TASK_HUB_DEVICE_ID` | stable host hash | Stable LAN peer identity |
| `TASK_HUB_ENABLE_PEERS` | `1` | Set to `0` to disable peer aggregation |
| `TASK_HUB_PEER_DISCOVERY_MS` | `15000` | UDP peer discovery interval |
| `TASK_HUB_PEER_CACHE_MS` | `5000` | Remote task cache duration |
| `TASK_HUB_ADAPTER_MAX_WORKERS` | `8` | Concurrent local adapter scans |

Diagnostics:

```bash
open http://127.0.0.1:5577/diagnostics
curl http://127.0.0.1:5577/diagnostics.json
open http://127.0.0.1:5577/peers
curl http://127.0.0.1:5577/peers.json?refresh=1
```

## Local API

| Endpoint | Purpose |
| --- | --- |
| `/health` | Host status, version, LAN identity |
| `/tasks` | Full task list for the web/debug page |
| `/tasks?format=stick` | Compact payload used by the StickS3 |
| `/tasks?scope=local` | Local Mac tasks only, used by peer aggregation |
| `/tasks/:id` | Local detail/debug page |
| `/tasks/:id/open` | Open selected source from the StickS3 |
| `/tasks/:id/open-native` | Host-to-host remote open forwarding |
| `/voice` | Transcribe a posted audio clip and paste it into a target app (voice mode) |
| `POST /pair` | Unauthenticated pairing poll from an unprovisioned StickS3; answers `pending` until the code is approved |
| `/pair/pending`, `POST /pair/approve` | Loopback-only: list devices asking to pair, approve a code (used by `taskhub_pair.py`) |
| `/diagnostics` | Human-readable Host health, adapter, voice, cache, and peer diagnostics |
| `/diagnostics.json` | Machine-readable diagnostics without token values or task titles |
| `/peers` | Human-readable multi-device diagnostics |
| `/peers.json` | Machine-readable peer status |
| `/debug/lovable` | Lovable app/browser signal diagnostics |

## Power Profile

The firmware is battery-first by default. It wakes, fetches once, shows the last
task snapshot immediately when possible, stays visible briefly, then sleeps
again. Active and WAIT tasks still refresh more often, but Eco Mode avoids
keeping the tiny display lit forever.

| Setting | Default |
| --- | --- |
| Eco mode | `ECO_MODE=1` |
| Normal timer wake | `AUTO_WAKE_SECONDS=600` |
| Active/attention timer wake | `ACTIVE_WAKE_SECONDS=120` |
| Low-battery timer wake | `LOW_BATTERY_WAKE_SECONDS=1200` |
| Timer-wake screen time | `QUIET_TIMER_TIMEOUT_MS=1500` |
| Button-wake screen time | `INTERACTIVE_TIMEOUT_MS=6000` |
| WAIT attention hold | `WAIT_ATTENTION_TIMEOUT_MS=180000` |
| Low-battery WAIT hold | `LOW_BATTERY_WAIT_ATTENTION_TIMEOUT_MS=45000` |
| Normal brightness | `DISPLAY_BRIGHTNESS=22` |
| Low-battery brightness | `LOW_BATTERY_BRIGHTNESS=8` |
| Awake active refresh | `AWAKE_REFRESH_ACTIVE_MS=15000` |
| Awake WAIT refresh | `AWAKE_REFRESH_WAIT_MS=15000` |
| CPU clock | `POWER_SAVE_CPU_MHZ=80` |
| Charge current | `CHARGE_CURRENT_MA=200` |

A WAIT almost always follows a running task, so the device is usually deep-sleeping
with an active task when one appears. `ACTIVE_WAKE_SECONDS=120` caps how long a
new WAIT can go unnoticed to about two minutes while cutting radio wakeups in
half versus the old 60-second default. Set `WAIT_ATTENTION_TIMEOUT_MS=0` if you
want the old always-on WAIT behavior.

The on-device WAIT/DONE/FAIL alerts are tunable in `firmware/task_monitor/secrets.h`.
Transitions are detected per task (by id) against the previous refresh, so a
second session entering WAIT still rings; when several edges land in one
refresh only the most urgent plays (WAIT > FAIL > DONE) and the screen jumps to
that task.

| Setting | Default | Purpose |
| --- | --- | --- |
| `ALERT_ON_WAIT` | `1` | Master switch for the WAIT alert |
| `ALERT_ON_DONE` | `1` | Master switch for the DONE chime |
| `ALERT_ON_FAIL` | `1` | Master switch for the FAIL buzz |
| `ALERT_BEEP` | `1` | Speaker beep/chime; set `0` for silent screen-only alerts |
| `ALERT_WAIT_HZ` / `ALERT_DONE_HZ` / `ALERT_FAIL_HZ` | `2400` / `1500` / `1800` | WAIT double-beep pitch, DONE base pitch, FAIL starting pitch |
| `ALERT_BEEP_VOLUME` | `150` | Shared speaker loudness |

> Vibration: the M5StickS3 is not driven as a motor by the pinned M5Unified, so
> `ALERT_VIBRATION` is a no-op on this board and stays off — the alert uses the
> screen and speaker instead.

For UI or network debugging, set `ENABLE_DEEP_SLEEP` to `0` in
`firmware/task_monitor/secrets.h`. Re-enable it before normal use.

## Source Accuracy

TaskHub uses local data only. Accuracy depends on what each source exposes on
disk, through local process state, or through visible browser UI.

| Source | What is usually available |
| --- | --- |
| Codex | Task title, folder, turn state, token usage, running/wait status |
| Claude Code | Transcript state, prompt/wait detection, plan approvals, usage, resume process |
| Cursor | Composer title, workspace folder, turn state, pending approvals, context token usage |
| OpenClaw | Local task registry, session title, task state |
| Manus | Local session metadata, timestamps, status codes, usage counters |
| Perplexity | App/browser activity; exact Perplexity Computer task names may be unavailable |
| Gemini | App/browser activity; visible tab title when exposed by the browser |
| Lovable | App/browser activity, project tabs, renderer CPU, visible generation controls |
| Kimi | Local `running/completed` state cross-checked with daemon active turns, plus browser title |
| WorkBuddy | Local session title, folder, tool calls, completion state, token data when exposed |
| Cline / Roo Code / Kilo Code | Task prompt, host editor, workspace folder, pending approval/question (WAIT), request in flight (RUN), completion, tokens and cost |
| Gemini CLI / Qwen Code | First prompt, project folder, model/tool turn state, tool awaiting approval (WAIT), token usage |
| GitHub Copilot CLI | Workspace summary, folder, turn start/end and tool events, pending permission (WAIT) |
| Grok | Safari Web App/browser title and visible Stop control; exact title is best with the web bridge |

If an app is open but not actively generating or executing, TaskHub should show
`REC` or `IDLE`, not `RUN`.

## Privacy And Security

TaskHub is local-first.

- The StickS3 talks to your Mac Host on your LAN.
- The Host does not upload task data to a cloud service.
- Firmware Wi-Fi secrets live in `secrets.h`, which is gitignored.
- The StickS3 API does not return auth tokens or message bodies.
- LAN peers must use the same token to participate.
- Host-to-device traffic uses plain HTTP on your LAN; run TaskHub only on a
  trusted local network.
- Do not expose port `5577` or `5578` directly to the public internet.

## Troubleshooting

| Problem | Check |
| --- | --- |
| StickS3 cannot find the Host | Confirm Mac and StickS3 are on the same Wi-Fi, then check `/health`. A VPN/proxy (Clash, Surge, Tailscale) used to make the Host advertise its tunnel IP; the Host now picks the LAN interface facing the Stick |
| Pairing screen says "looking for Mac Host" forever | Same Wi-Fi as the Mac? Guest networks and "AP isolation" block the UDP discovery on port `5578`. Is the Host running (`/health`)? |
| `401` from the Host | Confirm `DEVICE_TOKEN` matches the Host token file, or re-pair: hold both buttons while powering on, then run `taskhub_pair.py` |
| Task status looks stale or wrong | Open `/diagnostics` and check the source adapter row |
| Cursor (or another source) shows 0 tasks with a "Full Disk Access" adapter error | macOS TCC blocks the launchd Host from other apps' data; grant Full Disk Access to the Python that runs the Host (see [INSTALL.md](INSTALL.md#full-disk-access-macos-15)) |
| No peer Macs show up | Open `/peers.json?refresh=1`, check token match and UDP port `5578` |
| An app only shows `REC` | The app may expose activity but no active task signal |
| Lovable running state looks wrong | Open `/debug/lovable` and inspect renderer CPU/browser basis |
| Battery drains too quickly | Lower brightness, shorten timeouts, keep deep sleep enabled |
| Browser task title is missing | Give the browser accessibility permission or keep the tab visible |

## Development

Useful checks before publishing a build:

```bash
python3 -m unittest discover -s host/tests   # host adapter regression suite
python3 -m py_compile host/task_hub.py docs/render_screens.py
python3 docs/render_screens.py
./firmware/flash_task_monitor.sh compile
```

### Testing & CI

The host logic ships with a dependency-free `unittest` suite in
[`host/tests/`](host/tests) covering status derivation, WAIT detection,
case-insensitive process matching, token accounting, LRU scan memoisation, and
`/ingest` validation/expiry. [GitHub Actions](.github/workflows/ci.yml) runs the
suite, byte-compiles the Host, and compiles the firmware against the ESP32 core
on every push and pull request.

Repository layout:

```text
firmware/task_monitor/   StickS3 firmware
firmware/flash_task_monitor.sh
host/task_hub.py         Local macOS Host
host/taskhub_config.py   Host runtime configuration
host/install_task_hub.sh LaunchAgent installer/repair script
host/README.md           Host diagnostics and adapter notes
scripts/setup.sh         First-run setup helper
INSTALL.md               Full installation and troubleshooting guide
docs/                    Device screen renders
CHANGELOG.md             Release notes
```

## Roadmap

- Signed and notarized distribution of the current Mac Host package.
- Recorded local-metadata fixtures to broaden the adapter regression suite.
- More detailed browser-task extraction and recorded fixtures as supported sites evolve.
- Optional external buzzer / vibration motor for alerts.
- Better first-run setup flow for non-developer users.

## Release

Current release: `v2.2.0`.

See [CHANGELOG.md](CHANGELOG.md) for release notes.
