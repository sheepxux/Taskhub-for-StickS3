# Installation

This guide is for setting up TaskHub for StickS3 from a fresh clone.

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

## M5Burner / Public Firmware Setup

M5Burner should use the public firmware build, not a binary compiled from your
local `secrets.h`.

Build public artifacts:

```bash
./firmware/build_m5burner_public.sh
```

This compiles with `TASKHUB_PUBLIC_BUILD=1`, which ignores
`firmware/task_monitor/secrets.h` even if it exists. The resulting firmware has
no Wi-Fi password or Host token baked in. On first boot it shows `USB Setup`.

After burning the public firmware, plug the StickS3 into the Mac and run:

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

The on-device UI defaults to English. Add `--lang zh` to the provisioning
command if you want Chinese fixed UI text:

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

You can also clear runtime config by holding both StickS3 buttons during boot.
On public builds that returns the device to the `USB Setup` screen.

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

## Troubleshooting

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

Keep deep sleep enabled for normal use. Lower these values in
`firmware/task_monitor/secrets.h` if needed:

```cpp
#define DISPLAY_BRIGHTNESS 32
#define QUIET_TIMER_TIMEOUT_MS 3000
#define INTERACTIVE_TIMEOUT_MS 10000
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
