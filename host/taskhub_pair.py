#!/usr/bin/env python3
"""Mac-side half of StickS3 pairing.

Waits for an unpaired StickS3 to show up at the local Host (it finds the Host
over UDP discovery and keeps POSTing the code on its screen to /pair), asks
the person at this Mac to type that code, and approves it over the
loopback-only /pair/approve endpoint. Used at the end of `install.sh` and by
the TaskHub Host app; also runnable on its own:

    python3 taskhub_pair.py            # interactive, in a Terminal
    python3 taskhub_pair.py --gui      # native dialogs (osascript), no Terminal

Exit code 0 once a device is paired, 1 on timeout/cancel.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_BASE = "http://127.0.0.1:5577"

# Talk to the loopback Host directly: a system-wide HTTP proxy (common on
# macOS) would otherwise swallow 127.0.0.1 requests with a 502.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(base: str, path: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    try:
        with _OPENER.open(base + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _post(base: str, path: str, payload: Dict[str, Any], timeout: float = 3.0) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=body, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8", "replace"))
        except ValueError:
            return {"ok": False, "message": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}


def wait_for_host(base: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _get(base, "/health", timeout=1.5):
            return True
        time.sleep(0.5)
    return False


def _osascript(script: str) -> Optional[str]:
    try:
        out = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def gui_notice(title: str, message: str) -> None:
    _osascript(f'display dialog "{_escape(message)}" with title "{_escape(title)}" buttons {{"OK"}} default button "OK"')


def gui_ask_code(title: str, message: str) -> Optional[str]:
    # osascript returns `button returned:OK, text returned:1234`
    out = _osascript(
        f'display dialog "{_escape(message)}" with title "{_escape(title)}" default answer "" '
        'buttons {"Cancel", "Pair"} default button "Pair"'
    )
    if not out or "text returned:" not in out:
        return None
    return out.split("text returned:", 1)[1].strip()


def terminal_input(prompt: str) -> Optional[str]:
    """Read a line from the user even when stdin is a pipe (curl ... | bash)."""
    if sys.stdin.isatty():
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            return None
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(prompt)
            tty.flush()
            line = tty.readline()
            return line.rstrip("\n") if line else None
    except (OSError, KeyboardInterrupt):
        return None


def describe(device: Dict[str, Any]) -> str:
    name = device.get("name") or device.get("device_id") or "StickS3"
    ip = device.get("ip") or ""
    return f"{name} ({ip})" if ip else str(name)


def pick_device(pending: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    fresh = [row for row in pending if not row.get("approved")]
    if not fresh:
        return None
    fresh.sort(key=lambda row: int(row.get("last_seen_ms") or 0))
    return fresh[0]


def run(base: str, timeout_s: float, gui: bool, lang: str) -> int:
    zh = lang.lower().startswith("zh")

    def t(en: str, cn: str) -> str:
        return cn if zh else en

    if not wait_for_host(base, min(timeout_s, 30)):
        msg = t("TaskHub Host is not answering on 127.0.0.1:5577.", "TaskHub Host 没有在 127.0.0.1:5577 响应。")
        if gui:
            gui_notice("TaskHub", msg)
        else:
            print(msg, file=sys.stderr)
        return 1

    waiting_msg = t(
        "Waiting for a StickS3... Power it on; once it is on your Wi-Fi it shows a 4-digit code.",
        "等待 StickS3…… 请开机；连上 Wi-Fi 后屏幕会显示 4 位配对码。",
    )
    if not gui:
        print(waiting_msg)
    deadline = time.monotonic() + timeout_s
    announced = False
    while time.monotonic() < deadline:
        data = _get(base, "/pair/pending") or {}
        device = pick_device(list(data.get("pending") or []))
        if device is None:
            if gui and not announced:
                announced = True
            time.sleep(1.5)
            continue

        prompt = t(
            f"StickS3 \"{describe(device)}\" wants to pair with this Mac.\nEnter the code shown on its screen:",
            f"StickS3「{describe(device)}」请求与这台 Mac 配对。\n请输入它屏幕上显示的配对码：",
        )
        if gui:
            code = gui_ask_code("TaskHub", prompt)
            if code is None:
                return 1
        else:
            answer = terminal_input(prompt.replace("\n", " ") + " ")
            if answer is None:
                print()
                return 1
            code = answer.strip()
        code = "".join(ch for ch in code if ch.isdigit())
        if not code:
            continue
        result = _post(base, "/pair/approve", {"code": code, "device_id": device.get("device_id") or ""})
        if result.get("ok"):
            done = t(
                f"Paired {describe(device)}. The StickS3 saves the token and restarts now.",
                f"已配对 {describe(device)}。StickS3 正在保存并重启。",
            )
            if gui:
                gui_notice("TaskHub", done)
            else:
                print(done)
            return 0
        err = t(
            f"That code did not match ({result.get('message') or 'rejected'}). Try again.",
            f"配对码不匹配（{result.get('message') or 'rejected'}），请重试。",
        )
        if gui:
            gui_notice("TaskHub", err)
        else:
            print(err)

    msg = t(
        "No StickS3 asked to pair. Burn the TaskHub firmware with M5Burner, power the Stick on, and run this again.",
        "没有 StickS3 请求配对。请先用 M5Burner 烧录 TaskHub 固件并开机，然后重新运行。",
    )
    if gui:
        gui_notice("TaskHub", msg)
    else:
        print(msg, file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Approve a StickS3 pairing code on this Mac")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for a device")
    parser.add_argument("--gui", action="store_true", help="use native macOS dialogs instead of the terminal")
    parser.add_argument("--lang", default="en")
    args = parser.parse_args()
    return run(args.base, args.timeout, args.gui, args.lang)


if __name__ == "__main__":
    raise SystemExit(main())
