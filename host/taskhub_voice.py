#!/usr/bin/env python3
"""
Voice mode for TaskHub: transcribe a short audio clip (Mandarin / English) with
local whisper.cpp and inject the text into the frontmost macOS app's input
field via the clipboard + Cmd-V.

Flow: StickS3 hold-to-talk -> POST /voice (WAV bytes) -> this module ->
whisper.cpp (resident whisper-server, or whisper-cli fallback) -> cleaned text
-> osascript paste into whatever window is focused (e.g. the chat box you just
opened with BtnB). Everything stays on the LAN / local machine.
"""

from __future__ import annotations

import json
import ctypes
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

# Resident whisper.cpp server (model stays in memory -> fast). Started by
# host/run_whisper_server.sh. We fall back to one-shot whisper-cli if it's down.
WHISPER_SERVER_URL = os.environ.get("TASK_HUB_WHISPER_URL", "http://127.0.0.1:8080/inference")
WHISPER_CLI = os.environ.get("TASK_HUB_WHISPER_CLI", "whisper-cli")

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL_NAMES = (
    "ggml-large-v3-turbo-q5_0.bin",
    "ggml-large-v3-turbo.bin",
    "ggml-medium.bin",
    "ggml-small.bin",
)


def resolve_whisper_model(
    explicit: Optional[str] = None,
    *,
    module_dir: Optional[str] = None,
    cwd: Optional[str] = None,
) -> str:
    """Resolve the model path for the CLI fallback.

    The Host is often copied into ~/Library/Application Support, while the
    downloaded models usually remain in the repo under host/models. Try both
    shapes so voice mode survives a normal Host install.
    """
    if explicit is None:
        explicit = os.environ.get("TASK_HUB_WHISPER_MODEL", "")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))

    module_dir = os.path.abspath(module_dir or MODULE_DIR)
    cwd = os.path.abspath(cwd or os.getcwd())
    search_dirs = [
        os.path.join(module_dir, "models"),
        os.path.join(os.path.dirname(module_dir), "host", "models"),
        os.path.join(cwd, "host", "models"),
    ]
    for directory in search_dirs:
        for name in DEFAULT_MODEL_NAMES:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                return path
    return os.path.join(module_dir, "models", DEFAULT_MODEL_NAMES[0])


WHISPER_MODEL = resolve_whisper_model()
# "auto" lets whisper detect Mandarin vs English per clip (handles code-switching
# reasonably). Override to "zh" or "en" to force a language.
WHISPER_LANGUAGE = os.environ.get("TASK_HUB_WHISPER_LANGUAGE", "auto")

# Canonical source-name -> macOS app bundle id, so a /voice request can target
# the app for a task by its source name. Authoritative targeting still prefers
# the task's own _open action (passed as ?app=<bundle> or resolved from ?task=);
# this map is the convenience/fallback path. Browser-only sources have no
# stable desktop bundle and fall back to the frontmost window.
SOURCE_BUNDLES = {
    "claude": "com.anthropic.claudefordesktop",
    "codex": "com.openai.codex",
    "cursor": "com.todesktop.230313mzl4w4u92",
    "kimi": "com.moonshot.kimichat",
    "manus": "im.manus.desktop",
    "perplexity": "ai.perplexity.macv3",
    "workbuddy": "com.workbuddy.workbuddy",
}


def bundle_for_source(source: str) -> str:
    return SOURCE_BUNDLES.get((source or "").strip().lower(), "")
WHISPER_TIMEOUT = float(os.environ.get("TASK_HUB_WHISPER_TIMEOUT", "30"))
MAX_AUDIO_BYTES = int(os.environ.get("TASK_HUB_VOICE_MAX_BYTES", str(12 * 1024 * 1024)))
VOICE_ACTIVATE_DELAY_SEC = float(os.environ.get("TASK_HUB_VOICE_ACTIVATE_DELAY", "0.65"))
VOICE_PASTE_SETTLE_SEC = float(os.environ.get("TASK_HUB_VOICE_PASTE_SETTLE", "0.45"))
VOICE_RESTORE_DELAY_SEC = float(os.environ.get("TASK_HUB_VOICE_RESTORE_DELAY", "0.45"))
VOICE_RESTORE_CLIPBOARD = os.environ.get("TASK_HUB_VOICE_RESTORE_CLIPBOARD", "0").lower() in {
    "1", "true", "yes", "on"
}
VOICE_FOCUS_CLICK_BUNDLES = {
    item.strip()
    for item in os.environ.get("TASK_HUB_VOICE_FOCUS_CLICK_BUNDLES", "com.anthropic.claudefordesktop").split(",")
    if item.strip()
}
VOICE_FOCUS_CLICK_X_RATIO = float(os.environ.get("TASK_HUB_VOICE_FOCUS_CLICK_X_RATIO", "0.50"))
VOICE_FOCUS_CLICK_Y_RATIO = float(os.environ.get("TASK_HUB_VOICE_FOCUS_CLICK_Y_RATIO", "0.90"))

# whisper emits these for silence/non-speech; never inject them.
_NOISE_TOKENS = {
    "[blank_audio]", "[silence]", "(silence)", "[ silence ]",
    "[music]", "(music)", "[no speech]", "(no speech)",
    "[inaudible]", "(inaudible)", "[noise]", "(noise)",
    "you", "thank you.", "thanks for watching!",
}


def clean_transcript(text: str) -> str:
    """Trim whisper artifacts: bracketed non-speech tags, leading/trailing
    whitespace, and collapse internal whitespace. Returns '' for pure noise."""
    if not text:
        return ""
    # Drop standalone bracketed/parenthesised non-speech tags.
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\((?:silence|music|inaudible|noise|no speech)\)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    if text.lower() in _NOISE_TOKENS:
        return ""
    return text


def _build_multipart(fields: Dict[str, str], file_field: str, filename: str, data: bytes) -> Tuple[bytes, str]:
    boundary = "----taskhubvoiceboundary7MA4YWxkTrZu0gW"
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode()
    out += f"--{boundary}\r\n".encode()
    out += f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    out += b"Content-Type: audio/wav\r\n\r\n"
    out += data
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), boundary


def _transcribe_via_server(data: bytes) -> Optional[str]:
    fields = {"response_format": "json", "temperature": "0"}
    if WHISPER_LANGUAGE:
        fields["language"] = WHISPER_LANGUAGE
    body, boundary = _build_multipart(fields, "file", "clip.wav", data)
    req = urllib.request.Request(
        WHISPER_SERVER_URL,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=WHISPER_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return None
    raw = raw.strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "text" in obj:
            return str(obj["text"])
    except json.JSONDecodeError:
        pass
    return raw  # some builds return plain text


def _transcribe_via_cli(data: bytes) -> Optional[str]:
    if not os.path.isfile(WHISPER_MODEL):
        return None
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as fh:
        fh.write(data)
        fh.flush()
        args = [WHISPER_CLI, "-m", WHISPER_MODEL, "-f", fh.name, "-nt"]
        if WHISPER_LANGUAGE:
            args += ["-l", WHISPER_LANGUAGE]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=WHISPER_TIMEOUT + 30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout


def transcribe(data: bytes) -> Tuple[bool, str, str]:
    """Return (ok, text, error). Tries the resident server first, then CLI."""
    if not data:
        return False, "", "empty audio"
    raw = _transcribe_via_server(data)
    if raw is None:
        raw = _transcribe_via_cli(data)
    if raw is None:
        return False, "", "whisper unavailable (start run_whisper_server.sh or install the model)"
    text = clean_transcript(raw)
    if not text:
        return True, "", "no speech detected"
    return True, text, ""


def _safe_bundle(value: Optional[str]) -> str:
    """Allow only the characters real bundle ids / app names use, so the value
    can't break out of the AppleScript string."""
    if not value:
        return ""
    return value if re.fullmatch(r"[A-Za-z0-9 ._-]{1,80}", value) else ""


def accessibility_trusted() -> bool:
    """Whether this Host process is trusted to drive macOS accessibility events."""
    if os.environ.get("TASK_HUB_VOICE_SKIP_ACCESSIBILITY_CHECK", "").lower() in {"1", "true", "yes", "on"}:
        return True
    try:
        app_services = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        fn = app_services.AXIsProcessTrusted
        fn.restype = ctypes.c_bool
        return bool(fn())
    except Exception:
        return True


def inject_text(
    text: str,
    *,
    press_enter: bool = False,
    restore_clipboard: bool = VOICE_RESTORE_CLIPBOARD,
    activate_bundle: Optional[str] = None,
    activate_name: Optional[str] = None,
) -> Tuple[bool, str]:
    """Paste `text` into a target app via clipboard + Cmd-V (osascript). When
    activate_bundle/activate_name is given, that app is brought to the front
    first so the text lands in *that* window (e.g. the Claude chat you opened)
    rather than whatever happens to be frontmost. Requires the Host's
    controlling app to have macOS Accessibility permission."""
    if not text:
        return False, "empty text"
    if not accessibility_trusted():
        return False, "Accessibility permission required for TaskHub Host (System Settings -> Privacy & Security -> Accessibility)"
    steps = [
        "on run argv",
        "  set theText to item 1 of argv",
        "  set savedClip to \"\"",
        "  try",
        "    set savedClip to (the clipboard as text)",
        "  end try",
    ]
    bundle = _safe_bundle(activate_bundle)
    name = _safe_bundle(activate_name)
    if bundle:
        steps += [
            "  try",
            f"    tell application id \"{bundle}\" to activate",
            "  end try",
            f"  delay {VOICE_ACTIVATE_DELAY_SEC:.2f}",
            "  tell application \"System Events\"",
            f"    set targetProcesses to application processes whose bundle identifier is \"{bundle}\"",
            "    if (count of targetProcesses) > 0 then",
            "      set frontmost of item 1 of targetProcesses to true",
            "    end if",
            "  end tell",
            "  delay 0.20",
        ]
        if bundle in VOICE_FOCUS_CLICK_BUNDLES:
            steps += [
                "  try",
                "    tell application \"System Events\"",
                f"      set targetProcesses to application processes whose bundle identifier is \"{bundle}\"",
                "      if (count of targetProcesses) > 0 then",
                "        tell window 1 of item 1 of targetProcesses",
                "          set winPos to position",
                "          set winSize to size",
                "        end tell",
                f"        set clickX to (item 1 of winPos) + ((item 1 of winSize) * {VOICE_FOCUS_CLICK_X_RATIO:.2f})",
                f"        set clickY to (item 2 of winPos) + ((item 2 of winSize) * {VOICE_FOCUS_CLICK_Y_RATIO:.2f})",
                "        click at {clickX, clickY}",
                "      end if",
                "    end tell",
                "  end try",
                "  delay 0.20",
            ]
    elif name:
        steps += [
            "  try",
            f"    tell application \"{name}\" to activate",
            "  end try",
            f"  delay {VOICE_ACTIVATE_DELAY_SEC:.2f}",
            "  tell application \"System Events\"",
            f"    if exists application process \"{name}\" then",
            f"      set frontmost of application process \"{name}\" to true",
            "    end if",
            "  end tell",
            "  delay 0.20",
        ]
    steps += [
        "  set the clipboard to theText",
        "  delay 0.10",
        "  tell application \"System Events\" to key code 9 using command down",
        f"  delay {VOICE_PASTE_SETTLE_SEC:.2f}",
    ]
    if press_enter:
        steps += ["  tell application \"System Events\" to key code 36", "  delay 0.20"]  # Return
    if restore_clipboard:
        steps += [f"  delay {VOICE_RESTORE_DELAY_SEC:.2f}", "  set the clipboard to savedClip"]
    steps.append("end run")
    script = "\n".join(steps)
    try:
        proc = subprocess.run(["osascript", "-e", script, text], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"osascript failed: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "-1719" in err or "not allowed" in err.lower() or "assistive" in err.lower():
            return False, "Accessibility permission required for the Host app (System Settings → Privacy → Accessibility)"
        return False, err or "osascript error"
    return True, ""


def handle_voice(
    data: bytes,
    *,
    inject: bool = True,
    press_enter: bool = False,
    activate_bundle: Optional[str] = None,
    activate_name: Optional[str] = None,
) -> Dict[str, object]:
    """End-to-end: transcribe `data`, optionally inject the text into a target
    app. Returns a JSON-serialisable dict for the HTTP response."""
    if len(data) > MAX_AUDIO_BYTES:
        return {"ok": False, "error": "audio too large"}
    ok, text, err = transcribe(data)
    if not ok:
        return {"ok": False, "error": err}
    result: Dict[str, object] = {"ok": True, "text": text, "injected": False, "inject_requested": inject}
    if not text:
        result["note"] = err or "no speech detected"
        return result
    if inject:
        injected, ierr = inject_text(
            text, press_enter=press_enter, activate_bundle=activate_bundle, activate_name=activate_name
        )
        result["injected"] = injected
        if activate_bundle or activate_name:
            result["target"] = activate_bundle or activate_name
        if not injected:
            result["inject_error"] = ierr
    return result
