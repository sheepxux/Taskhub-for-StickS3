# Installation

**Just want to use it?** Burn the firmware with M5Burner, put the Stick on Wi-Fi
from your phone, and install the Mac Host with one command; see
[End-User Setup](#end-user-setup-m5burner--one-command-host). The rest of this
guide is for developers who build from source or need the USB/manual paths.

TaskHub has two parts:

- **Mac Host**: a local macOS service that reads AI task state and exposes a LAN API.
- **StickS3 firmware**: the M5StickS3 app that discovers the Host, shows task state, and sleeps between refreshes.

Security note: Host-to-device traffic uses plain HTTP with a shared token. Use
TaskHub only on a trusted local network, and do not expose ports `5577` or
`5578` to the public internet.

## Requirements

- macOS
- M5StickS3
- USB-C cable for the first flash
- Python 3
- `arduino-cli`
- ESP32 Arduino core
- Arduino libraries: `M5Unified`, `ArduinoJson`
- Optional: Node.js, used by some adapters to read local LevelDB app stores

## Recommended Setup

Clone the repository:

```bash
git clone https://github.com/sheepxux/Taskhub-for-StickS3.git
cd Taskhub-for-StickS3
```

Run the setup helper:

```bash
./scripts/setup.sh
```

By default this will:

- install or repair the macOS Host LaunchAgent
- create `firmware/task_monitor/secrets.h` if it does not exist
- create or reuse the shared device token
- sync the token into the firmware config
- prompt for Wi-Fi SSID/password when the firmware config still has placeholders

It will **not** install Arduino dependencies or flash the device unless you ask
for that explicitly.

## macOS Host Package (preview)

On macOS, build a secret-free Host installer with:

```bash
./packaging/macos/build_host_pkg.sh
```

The resulting `dist/macos/TaskHub-Host-<version>.pkg` contains only an explicit
Host-code allowlist. It does not include firmware `secrets.h`, Wi-Fi values,
tokens, models, logs, caches, or repository metadata. Installation creates a
random per-user token, starts the Host LaunchAgent, installs the TaskHub Host
app (pairing dialog + diagnostics) and adds the `taskhub-pair` and
`taskhub-provision` commands. The installer ends by opening the pairing dialog.

The development package is unsigned. Public distribution requires a Developer
ID Installer certificate and Apple notarization; see
[`packaging/macos/README.md`](packaging/macos/README.md).

## End-User Setup (M5Burner + one-command Host)

No `arduino-cli`, no `secrets.h`, no USB provisioning.

1. **Firmware**: open [M5Burner](https://docs.m5stack.com/en/download), search
   `TaskHub for StickS3`, connect the Stick over USB-C, click **Burn**.
2. **Wi-Fi**: power the Stick on. It starts an open hotspot `TaskHub-XXXX` and
   shows a QR code. Join it from your phone; the captive page lists nearby
   networks, pick yours and enter the password. The Stick joins, then shows a
   4-digit **pairing code** (the phone page shows it too).
3. **Host + pairing**: on the Mac, in Terminal:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/sheepxux/Taskhub-for-StickS3/main/scripts/install.sh | bash
   ```

   The script installs the Host LaunchAgent, waits for the Stick to ask for
   pairing over the LAN, and prompts for the code. The token is only sent to
   the device after the exact code is entered. The Stick saves it and restarts
   into the task list.

Pair again later (another Stick, or after a factory reset):

```bash
python3 ~/Library/Application\ Support/StickS3TaskHub/taskhub_pair.py
```

Device-side resets: hold **both buttons while powering on** to clear Wi-Fi and
token (back to the hotspot screen); hold **A for 3s on the pairing screen** to
redo only the Wi-Fi.

If the first Terminal command opens an Apple "Command Line Tools" installer,
finish that install and run the command again; macOS ships `python3` that way.

### How pairing works

- The Stick broadcasts `sticks3.discover` with `pair: true` on UDP `5578`;
  the Host answers with its LAN address (the interface facing the Stick, so
  a VPN/proxy tunnel IP is never advertised).
- The Stick POSTs `{device_id, name, code}` to `/pair` every 2.5s and shows
  the code. `/pair` is unauthenticated but only ever answers `pending`.
- `taskhub_pair.py` (or TaskHub Host.app) reads `/pair/pending` and calls
  `/pair/approve` with the code you type; both are loopback-only.
- The next `/pair` poll from that device gets the token and Host address.
  Pending requests expire after 5 minutes; wrong codes are simply rejected.

## Developer / USB Provisioning Of The Public Firmware

Build the public artifacts yourself:

```bash
./firmware/build_m5burner_public.sh
```

This compiles with `TASKHUB_PUBLIC_BUILD=1`, which ignores
`firmware/task_monitor/secrets.h` even if it exists, writes
`dist/m5burner/TaskHub-StickS3-v<version>.bin` and aborts if any local secret
string is found in the binary. Publishing steps are in
[`packaging/m5burner/README.md`](packaging/m5burner/README.md).

USB serial provisioning still works on the hotspot and pairing screens and is
handy for scripted setups. Plug the StickS3 into the Mac and run:

```bash
./scripts/setup.sh --skip-firmware --provision
```

The helper will:

- install or repair the macOS Host
- read the Host token from `~/Library/Application Support/StickS3TaskHub/token`
- auto-detect the StickS3 USB serial port
- auto-detect the Mac LAN IP as the fallback Host address
- prompt for Wi-Fi values when needed
- send one JSON config line over USB serial
- store the config in StickS3 NVS and restart the device

The on-device UI defaults to English (the captive portal also offers 中文). Add
`--lang zh` to the provisioning command for Chinese fixed UI text:

```bash
./scripts/setup.sh --skip-firmware --provision --lang zh
```

Non-interactive provisioning:

```bash
TASKHUB_WIFI_SSID="My WiFi" \
TASKHUB_WIFI_PASSWORD="wifi-password" \
TASKHUB_LANG="en" \
./scripts/setup.sh --skip-firmware --provision --non-interactive
```

Reset the runtime config on a plugged-in StickS3:

```bash
./scripts/provision_sticks3.sh --reset
```

Rotate an old/default shared token only while the StickS3 is connected:

```bash
./scripts/setup.sh --skip-firmware --rotate-token --provision
```

The helper updates the physical device first and replaces the Host token only
after USB provisioning succeeds.

## One-Pass Setup With Firmware Compile

If `arduino-cli` is already installed:

```bash
./scripts/setup.sh --compile
```

If you want the helper to install the ESP32 core and required Arduino libraries:

```bash
./scripts/setup.sh --deps --compile
```

If the StickS3 is plugged in and you want to compile and upload:

```bash
./scripts/setup.sh --deps --upload
```

## Non-Interactive Setup

For scripted setup, pass values through environment variables:

```bash
TASKHUB_WIFI_SSID="My WiFi" \
TASKHUB_WIFI_PASSWORD="wifi-password" \
TASKHUB_DEVICE_ID="sticks3-task-01" \
TASKHUB_LANG="en" \
./scripts/setup.sh --non-interactive
```

You can also pass command-line flags:

```bash
./scripts/setup.sh \
  --wifi-ssid "My WiFi" \
  --wifi-password "wifi-password" \
  --device-id "sticks3-task-01" \
  --lang en
```

Avoid putting real Wi-Fi passwords in shell history on shared machines.

## Manual Setup

### 1. Install The Mac Host

```bash
./host/install_task_hub.sh
```

The Host is installed to:

```text
~/Library/Application Support/StickS3TaskHub
```

The shared token is stored at:

```text
~/Library/Application Support/StickS3TaskHub/token
```

Check that the Host is running:

```bash
curl http://127.0.0.1:5577/health
```

Expected result: JSON with `"ok": true` and the current TaskHub version.

### 2. Configure Firmware Secrets

```bash
cp firmware/task_monitor/secrets.h.example firmware/task_monitor/secrets.h
```

Edit `firmware/task_monitor/secrets.h`:

```cpp
#define WIFI_SSID       "your-wifi-ssid"
#define WIFI_PASSWORD   "your-wifi-password"
#define DEVICE_TOKEN    "same-token-as-the-mac-host"
```

`DEVICE_TOKEN` must match:

```text
~/Library/Application Support/StickS3TaskHub/token
```

`TASK_HUB_HOST` is only a fallback. The firmware first tries UDP discovery on
port `5578`, so the Mac's LAN IP can change.

### 3. Install Arduino Dependencies

Install `arduino-cli` first. On macOS with Homebrew:

```bash
brew install arduino-cli
```

Install the ESP32 core and libraries:

```bash
arduino-cli config init || true
arduino-cli core update-index
arduino-cli core install esp32:esp32
arduino-cli lib install M5Unified ArduinoJson
```

### 4. Compile

```bash
./firmware/flash_task_monitor.sh compile
```

### 5. Upload

Plug in the StickS3, then run:

```bash
./firmware/flash_task_monitor.sh upload
```

Or compile and upload in one command:

```bash
./firmware/flash_task_monitor.sh all
```

## Multi-Mac Setup

Install the Host on every Mac you want to include.

Use the same token on every Mac and in the StickS3 firmware. Any Host can act
as the aggregator:

- Hosts discover peers over UDP port `5578`.
- The aggregator fetches each peer's `/tasks?scope=local` task list.
- The StickS3 displays rows such as `Codex@MBP` or `Lovable@Studio`.
- BtnA on a remote task forwards the open action to the Mac that owns it.

Useful checks:

```bash
open http://127.0.0.1:5577/diagnostics
curl http://127.0.0.1:5577/diagnostics.json
open http://127.0.0.1:5577/peers
curl http://127.0.0.1:5577/peers.json?refresh=1
```

## macOS Permissions

Some browser or app signals depend on macOS permissions.

If browser titles or visible running states are missing, grant the terminal or
Host runner accessibility permission:

1. Open **System Settings**.
2. Go to **Privacy & Security**.
3. Open **Accessibility**.
4. Allow the terminal app or the app that launches TaskHub.

TaskHub still works without this permission, but browser-based sources may only
show app activity rather than detailed titles or visible `RUN`/`WAIT` signals.

### Full Disk Access (macOS 15+)

Newer macOS versions protect other apps' data folders (TCC app-data
protection). The launchd-run Host is denied reads of, for example, Cursor's
`~/Library/Application Support/Cursor` and `~/.cursor/projects`, so the Cursor
adapter silently reports zero tasks even though the adapter itself is healthy.

Grant **Full Disk Access** to the Python interpreter that runs the Host:

1. Open **System Settings** → **Privacy & Security** → **Full Disk Access**.
2. Click **+** and add the interpreter binary. With the default LaunchAgent
   this is the Python that `/usr/bin/python3` resolves to — usually the
   Command Line Tools (`/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/Current/Resources/Python.app`)
   or, if only Xcode is installed,
   `/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/Current/Resources/Python.app`.
   Press Cmd+Shift+G in the file picker to paste the path.
3. Restart the Host: `launchctl kickstart -k gui/$(id -u)/com.sticks3.taskhub`.

`/diagnostics` flags affected adapters with a "Full Disk Access" error instead
of pretending the source is simply idle.

## Troubleshooting

### Start With Host Diagnostics

Open the local diagnostics page first when task status, voice mode, or
multi-device discovery looks wrong:

```bash
open http://127.0.0.1:5577/diagnostics
curl http://127.0.0.1:5577/diagnostics.json
```

The page summarizes adapter health, local task counts, voice-mode readiness,
Accessibility permission, caches, and peer discovery. It intentionally omits
token values and task titles.

### StickS3 Cannot Find The Host

Check:

```bash
curl http://127.0.0.1:5577/health
```

Then confirm:

- Mac and StickS3 are on the same Wi-Fi.
- macOS firewall allows local network connections.
- UDP port `5578` is not blocked.
- `TASK_HUB_HOST` in `secrets.h` is a valid fallback IP.

### `401` Or Unauthorized

The firmware token does not match the Host token.

Check the Host token:

```bash
cat "$HOME/Library/Application Support/StickS3TaskHub/token"
```

Then make sure `DEVICE_TOKEN` in `firmware/task_monitor/secrets.h` matches.

### No Peer Macs

Open:

```bash
open http://127.0.0.1:5577/peers
```

Check that all Hosts:

- are running TaskHub `v1.1` or newer
- use the same token
- are on the same LAN
- can use UDP port `5578`

### Firmware Does Not Compile

Run:

```bash
arduino-cli core list
arduino-cli lib list | grep -E 'M5Unified|ArduinoJson'
```

Then reinstall dependencies:

```bash
./scripts/setup.sh --deps --compile
```

### Upload Cannot Find A Port

Plug in the StickS3 and check:

```bash
ls /dev/cu.usbmodem*
```

If no port appears, try another cable or reconnect the device.

### Battery Drains Too Quickly

Keep deep sleep enabled for normal use. Current firmware defaults to Eco Mode.
For more battery life, lower brightness further or increase the active wake
cadence in `firmware/task_monitor/secrets.h`:

```cpp
#define ECO_MODE 1
#define DISPLAY_BRIGHTNESS 18
#define LOW_BATTERY_BRIGHTNESS 6
#define ACTIVE_WAKE_SECONDS 180
#define WAIT_ATTENTION_TIMEOUT_MS 120000
```

## Voice Mode (optional)

Hold **BtnB** on the StickS3 to dictate (Mandarin/English) into the app of the
selected task. Transcription is local (whisper.cpp); audio never leaves the LAN.

1. Install whisper.cpp and a model, then start the resident server:

   ```bash
   brew install whisper-cpp
   mkdir -p host/models && curl -L -o host/models/ggml-large-v3-turbo-q5_0.bin \
     https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
   ./host/install_whisper_server.sh
   ```

2. Grant the Host **Accessibility** permission: System Settings → Privacy &
   Security → Accessibility → enable the app that runs the Host (so it can paste
   into other apps).

3. Test from the Mac without the device:

   ```bash
   say -v Tingting "你好，这是语音测试" -o /tmp/v.wav --data-format=LEI16@16000
   TOKEN=$(cat "$HOME/Library/Application Support/StickS3TaskHub/token")
   curl -s -X POST -H "X-Device-Token: $TOKEN" --data-binary @/tmp/v.wav \
     'http://127.0.0.1:5577/voice?inject=0'
   ```

Then on the device: short-press BtnB to open a task's app, hold BtnB to talk,
release to transcribe and send. Tunables: `TASK_HUB_WHISPER_MODEL`,
`TASK_HUB_WHISPER_LANGUAGE`, and device-side `VOICE_AUTO_SEND` /
`TASKHUB_VOICE_SEND` / `--voice-send off` for paste-only review before sending.

## Updating

Pull the latest code:

```bash
git pull
```

Repair/reinstall the Host:

```bash
./scripts/setup.sh --skip-firmware
```

Recompile or reflash firmware when `firmware/task_monitor/task_monitor.ino`
changes:

```bash
./firmware/flash_task_monitor.sh all
```

## Uninstalling

Unload the LaunchAgent:

```bash
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.sticks3.taskhub.plist"
```

Remove installed Host files:

```bash
rm -rf "$HOME/Library/Application Support/StickS3TaskHub"
rm -f "$HOME/Library/LaunchAgents/com.sticks3.taskhub.plist"
```

This does not erase the firmware from the StickS3.
