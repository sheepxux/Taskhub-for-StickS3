# Publishing the StickS3 firmware on M5Burner

M5Burner is the official M5Stack burning tool and the way end users get the
firmware: they search "TaskHub", click **Burn**, and never touch a terminal.
Third-party firmware is published from inside M5Burner itself; there is no
web upload.

## 1. Build the public binary

```bash
./firmware/build_m5burner_public.sh
```

This compiles with `-DTASKHUB_PUBLIC_BUILD=1` (ignores `secrets.h`), writes
`dist/m5burner/TaskHub-StickS3-v<version>.bin` (merged bootloader + partitions +
app, burned at `0x0`) plus a `.sha256`, and refuses to continue if any string
from your local `secrets.h` is present in the binary.

## 2. Publish from M5Burner

1. Open M5Burner, sign in with your M5Stack community account (top right).
2. Bottom left: **USER CUSTOM** → **Publish**.
3. Fill in the form with the values below and upload the `.bin`.
4. Click **Upload**, then **Publish** to make it public (or **Share** for a
   share code while testing).

To ship a new version, open the firmware in USER CUSTOM → **Detail** and add a
version instead of creating a second listing.

### Listing values

| Field | Value |
|---|---|
| Name | `TaskHub for StickS3` |
| Version | same as `TASKHUB_FW_VERSION` in `task_monitor.ino` |
| Device Type | `StickS3` (ESP32-S3) |
| Github | `https://github.com/sheepxux/Taskhub-for-StickS3` |
| Firmware | `dist/m5burner/TaskHub-StickS3-v<version>.bin` |
| Cover | `packaging/m5burner/cover.png` (generate with `python3 packaging/m5burner/make_cover.py`) |

### Description (paste as-is)

```
TaskHub turns the M5StickS3 into a pocket status light for your AI coding agents.
It shows which of Codex, Claude Code, Cursor, Cline, Gemini CLI, Copilot CLI,
Kimi, Manus and more are RUNNING, WAITING for your approval, DONE or FAILED,
buzzes when one needs you, and opens the right window on your Mac with one
button. Everything stays on your LAN.

Setup (no terminal needed):
1. Burn this firmware and power the Stick on.
2. On your phone, join the Wi-Fi "TaskHub-XXXX" shown on the screen and pick
   your home network in the page that pops up.
3. On your Mac, install TaskHub Host from GitHub (one command or the .pkg) and
   type the 4-digit code shown on the Stick when asked.

Mac Host + source: https://github.com/sheepxux/Taskhub-for-StickS3
Buttons: A = next task, hold A = refresh, B = open task on the Mac, hold B = voice note.
Both buttons at power-on = factory reset (redo Wi-Fi + pairing).

把 M5StickS3 变成 AI 编程助手的口袋状态灯：显示 Codex / Claude Code / Cursor /
Cline / Gemini CLI / Copilot CLI / Kimi / Manus 等任务的运行、等待批准、完成、
失败状态，需要你时震动提醒，按一下就在 Mac 上切到对应窗口。全程局域网，不上传。
首次使用：烧录后开机 → 手机连接屏幕上的 "TaskHub-XXXX" 热点选择家里 Wi-Fi →
Mac 上安装 TaskHub Host 并输入屏幕上的 4 位配对码。
```

## 3. Sanity check before publishing

- Burn the `.bin` onto a StickS3 with M5Burner (or `esptool.py write_flash 0x0 <bin>`).
- It must boot straight into the **Wi-Fi Setup** screen (QR + `TaskHub-XXXX`),
  not into a task list: that would mean Wi-Fi credentials leaked into the build.
- Join the AP from a phone, pick a network, confirm the **Pair with Mac** screen
  shows a 4-digit code, then run `python3 host/taskhub_pair.py` on a Mac running
  the Host and enter the code. The Stick restarts into the task list.
