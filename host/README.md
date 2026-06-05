# StickS3 AI Task Hub

Run this on the Mac. The StickS3 reads the compact endpoint over Wi-Fi and asks
this service to open task details.

Install or repair the background Hub:

```bash
./host/install_task_hub.sh
```

The installer copies the Hub into `~/Library/Application Support/StickS3TaskHub`,
keeps the existing device token when present, installs the optional Manus LevelDB
reader dependency, writes the LaunchAgent, and restarts the service.

```bash
python3 host/task_hub.py --bind 0.0.0.0 --port 5577 --token dev-token
```

Or run it with the local firmware token from `secrets.h`:

```bash
./host/run_task_hub.sh
```

For a detached local test session:

```bash
screen -dmS sticks3_taskhub /bin/sh -lc 'cd "$PWD" && ./host/run_task_hub.sh >> .run/task_hub.log 2>&1'
```

Useful checks:

```bash
curl http://127.0.0.1:5577/health
curl -H 'X-Device-Token: dev-token' 'http://127.0.0.1:5577/tasks?format=stick&limit=8'
curl -H 'X-Device-Token: dev-token' 'http://127.0.0.1:5577/tasks?scope=local&limit=8'
curl -H 'X-Device-Token: dev-token' 'http://127.0.0.1:5577/peers.json'
curl -H 'X-Device-Token: dev-token' 'http://127.0.0.1:5577/debug/lovable'
```

Push an external task (e.g. from the browser extension or any script):

```bash
curl -X POST -H 'X-Device-Token: dev-token' 'http://127.0.0.1:5577/ingest' \
  -d '{"source":"Gemini","title":"Refactor pricing page","status":"running","url":"https://gemini.google.com/app/abc"}'
```

`POST /ingest` accepts a single task object or `{"tasks":[...]}`. Only `source`
and `title` are required; optional `id`, `status`, `subtitle`, `url`,
`updated_ms`, `ttl_sec` (default 90s), `needs_attention`, `detail`, `usage`.
Each pushed task expires after its TTL unless re-pushed, so it disappears on its
own when the pusher stops. See `extension/` for the Chrome/Edge web bridge.

Discovery check:

```bash
python3 - <<'PY'
import json, socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.settimeout(2)
sock.sendto(json.dumps({"type":"sticks3.discover","device":"test","token":"dev-token"}).encode(), ("255.255.255.255", 5578))
print(sock.recvfrom(2048)[0].decode())
PY
```

Configure the firmware:

```bash
cp firmware/task_monitor/secrets.h.example firmware/task_monitor/secrets.h
```

Set `WIFI_SSID`, `WIFI_PASSWORD`, and `DEVICE_TOKEN`.
`TASK_HUB_HOST` is now only a fallback; the firmware discovers the current Mac IP over UDP port `5578`.

Build and flash:

```bash
./firmware/flash_task_monitor.sh compile
./firmware/flash_task_monitor.sh upload
```

Button behavior:

- BtnA opens the selected task's original app/source on the Mac.
- BtnB selects the next task.
- BtnB hold refreshes immediately.

The local detail page remains available in the browser at `/tasks/:id`.

Multi-device mode:

- Install this Host on every Mac you want to include.
- Use the same `TASK_HUB_TOKEN` / firmware `DEVICE_TOKEN` on each Mac.
- Each Host announces `TASK_HUB_DEVICE_NAME` and `TASK_HUB_DEVICE_ID` in UDP discovery.
- Any Host can aggregate peers: it fetches `/tasks?scope=local` from discovered
  peers and rewrites remote IDs so BtnA can forward open requests back to the
  original Mac.
- Disable peer aggregation with `TASK_HUB_ENABLE_PEERS=0`.
- Tune LAN polling with `TASK_HUB_PEER_DISCOVERY_MS`, `TASK_HUB_PEER_CACHE_MS`,
  `TASK_HUB_PEER_DISCOVERY_TIMEOUT_MS`, and `TASK_HUB_PEER_HTTP_TIMEOUT_MS`.
- Open `/peers` locally to inspect discovered Hosts, fetch latency, task count,
  last success, and the latest error for each peer.
- Use `/debug/lovable` to inspect Lovable renderer CPU, browser-tab signals,
  and the specific basis used for `RUN` versus `REC`.

Power behavior:

- The device wakes, fetches tasks, shows them briefly, then enters deep sleep.
- It wakes every `AUTO_WAKE_SECONDS=600` by default, or every
  `ACTIVE_WAKE_SECONDS=60` while active/attention tasks are visible, so a freshly
  appeared `WAIT` is noticed within ~1 minute.
- Below `LOW_BATTERY_THRESHOLD_PCT=30`, it drops brightness and wakes every
  `LOW_BATTERY_WAKE_SECONDS=900`.
- Timer wakes stay interactive for `QUIET_TIMER_TIMEOUT_MS=3000`; button wakes
  stay interactive for `INTERACTIVE_TIMEOUT_MS=10000`.
- The firmware uses `POWER_SAVE_CPU_MHZ=80`, `DISPLAY_BRIGHTNESS=32`, and
  `CHARGE_CURRENT_MA=200` by default.
- No WebSocket is kept open by default; this is intentional for the small battery.

Current stable build:

- `ENABLE_DEEP_SLEEP` defaults to `1`. The device wakes, fetches the list, stays briefly interactive, then sleeps again. Override `ENABLE_DEEP_SLEEP` to `0` in `secrets.h` for UI/network debugging.
- Codex, Claude Code, and OpenClaw tasks include local token/turn or session usage when their logs expose it. OpenClaw uses its local task registry and session status instead of process-open detection.
- Codex and Claude Code can mark recent assistant questions or explicit confirmation prompts as `WAIT`; the StickS3 keeps the display on and refreshes while a visible `WAIT` task exists.
- Claude Code status is based on transcript turn state plus the matching `claude --resume` process. Active tool-use turns stay `RUN` even when a long tool call does not append transcript lines for several minutes. The compact subtitle uses `folder · tN · state` to fit the StickS3 screen.
- Manus is read from local app storage (`Local Storage/leveldb`) when the `classic-level` Node dependency is available to the hub. It uses local `sessions_detail`, `task_finished`, timestamps, and session usage counters; no message body or auth token is returned by the API. To keep the StickS3 list usable, Manus history is capped by `TASK_HUB_MANUS_MAX_SESSIONS` (default `3`).
- Perplexity uses local preferences for query counters when macOS allows the background hub to read them, and WebKit cache/WAL mtimes for recent activity. It marks `RUN` only when the hub observes those local signals change during polling; otherwise it stays `REC` or `IDLE` because no stable local task transcript or task database has been found yet.
- Gemini uses the local Gemini app process plus settings/log/cache mtimes, and also scans browser tabs for `gemini.google.com` in Safari, Chrome, Arc, Edge, Brave, and Chromium. When a Gemini tab is visible in Safari, accessibility headings can provide the current page/task title; background tabs and Chromium browsers fall back to the tab title, often just `Gemini`. It only marks `RUN` when a visible browser signal such as a stop-generation control is exposed.
- Lovable detects `Lovable.app` (`dev.lovable.build`) through its app process, renderer CPU, and local `lovable-desktop` storage/cache mtimes, and also scans browser tabs for `lovable.dev` in Safari, Chrome, Arc, Edge, Brave, and Chromium. It shows up to `TASK_HUB_LOVABLE_MAX_TABS=3` open Lovable project tabs, opens the original app or tab URL from BtnA, and marks `RUN` when a visible page exposes generation/building controls or when the Lovable renderer exceeds `TASK_HUB_LOVABLE_RENDERER_RUN_CPU=8.0`; app-only local cache activity stays `REC`.
- StickS3 hides old display-only tasks without deleting them on the Mac. Defaults: `DONE`/`IDLE` after 10 minutes, `REC` after 1 hour, `RUN`/`WAIT`/`FAIL` never. Override with `STICK_HIDE_DONE_AFTER_SEC`, `STICK_HIDE_IDLE_AFTER_SEC`, `STICK_HIDE_RECENT_AFTER_SEC`, and `STICK_HIDE_UNKNOWN_AFTER_SEC`.
