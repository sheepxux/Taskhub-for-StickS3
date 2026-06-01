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
screen -dmS sticks3_taskhub /bin/sh -lc 'cd /path/to/Taskhub-for-StickS3 && ./host/run_task_hub.sh >> .run/task_hub.log 2>&1'
```

Useful checks:

```bash
curl http://127.0.0.1:5577/health
curl -H 'X-Device-Token: dev-token' 'http://127.0.0.1:5577/tasks?format=stick&limit=8'
```

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

Power behavior:

- The device wakes, fetches tasks, shows them briefly, then enters deep sleep.
- It also wakes every `AUTO_WAKE_SECONDS` to refresh without USB.
- No WebSocket is kept open by default; this is intentional for the 250mAh battery.

Current stable build:

- `ENABLE_DEEP_SLEEP` defaults to `1`. The device wakes, fetches the list, stays briefly interactive, then sleeps again. Override `ENABLE_DEEP_SLEEP` to `0` in `secrets.h` for UI/network debugging.
- Codex, Claude Code, and OpenClaw tasks include local token/turn or session usage when their logs expose it. OpenClaw uses its local task registry and session status instead of process-open detection.
- Claude Code status is based on transcript turn state plus the matching `claude --resume` process. Active tool-use turns stay `RUN` even when a long tool call does not append transcript lines for several minutes. The compact subtitle uses `folder · tN · state` to fit the StickS3 screen.
- Manus is read from local app storage (`Local Storage/leveldb`) when the `classic-level` Node dependency is available to the hub. It uses local `sessions_detail`, `task_finished`, timestamps, and session usage counters; no message body or auth token is returned by the API. To keep the StickS3 list usable, Manus history is capped by `TASK_HUB_MANUS_MAX_SESSIONS` (default `3`).
- Perplexity uses local preferences for query counters when macOS allows the background hub to read them, and WebKit cache/WAL mtimes for recent activity. It marks `RUN` only when the hub observes those local signals change during polling; otherwise it stays `REC` or `IDLE` because no stable local task transcript or task database has been found yet.
- StickS3 hides old display-only tasks without deleting them on the Mac. Defaults: `DONE`/`IDLE` after 10 minutes, `REC` after 1 hour, `RUN`/`WAIT`/`FAIL` never. Override with `STICK_HIDE_DONE_AFTER_SEC`, `STICK_HIDE_IDLE_AFTER_SEC`, `STICK_HIDE_RECENT_AFTER_SEC`, and `STICK_HIDE_UNKNOWN_AFTER_SEC`.
