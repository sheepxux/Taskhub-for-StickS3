"""
Render representative StickS3 screens at the device's native 240×135 to PNGs.
Output goes to docs/screen-*.png. Colors and layout mirror task_monitor.ino
(top bar at y0..20, card at x6/y23 w228/h92, status accent strip 8px,
text positions and fonts as drawn by the firmware).

The CJK font falls back through PingFang SC → Heiti SC → Helvetica so this
script runs on any macOS without extra installs. ASCII labels use Menlo to
approximate M5GFX Font0's compact pixel look.
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

W, H = 240, 135

# Convert RGB565 → 8-bit RGB the way the panel actually shows it.
def rgb565(v: int):
    r = ((v >> 11) & 0x1F) << 3
    g = ((v >> 5) & 0x3F) << 2
    b = (v & 0x1F) << 3
    return (r, g, b)

C = {
    "BG": (0, 0, 0),
    "WHITE": (255, 255, 255),
    "GRAY": rgb565(0x7BEF),          # TFT_DARKGREY
    "GREEN": rgb565(0x07E0),
    "AMBER": rgb565(0xFDA0),
    "RED": rgb565(0xF800),
    "BLUE": rgb565(0x5BDF),          # the firmware's accent blue
    "CARD": rgb565(0x1082),          # card background
}

STATUS = {
    "run":  ("RUN",  C["BLUE"]),
    "wait": ("WAIT", C["AMBER"]),
    "fail": ("FAIL", C["RED"]),
    "done": ("DONE", C["GREEN"]),
    "idle": ("IDLE", C["GRAY"]),
}


def font(size: int, cjk: bool = False):
    """Pick a font that resembles the M5 glyphs at the requested size."""
    candidates_ascii = [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Monaco.ttf",
        "/System/Library/Fonts/Supplemental/Andale Mono.ttf",
    ]
    candidates_cjk = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ]
    for p in (candidates_cjk if cjk else candidates_ascii):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def text_w(draw, s, f):
    bbox = draw.textbbox((0, 0), s, font=f)
    return bbox[2] - bbox[0]


def truncate(draw, s, f, max_w):
    if text_w(draw, s, f) <= max_w:
        return s
    suffix = "…"
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if text_w(draw, s[:mid] + suffix, f) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (s[:lo] + suffix) if lo > 0 else suffix


def wrap_lines(draw, s, f, max_w, max_lines):
    """Greedy CJK-aware wrap by character, mirroring drawWrappedText()."""
    lines, cur = [], ""
    for ch in s:
        trial = cur + ch
        if text_w(draw, trial, f) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = ch
            if len(lines) == max_lines:
                # truncate the just-flushed line with the rest collapsed
                rest = (lines.pop() + s[len(''.join(lines)) + len(cur):]).strip()
                lines.append(truncate(draw, rest, f, max_w))
                return lines
    if cur:
        lines.append(cur)
    return lines[:max_lines]


def draw_top_bar(draw, wifi_ok=True, batt=87):
    f = font(9)
    draw.text((6, 5), "AI TASKS", fill=C["GRAY"], font=f)
    label = "wifi" if wifi_ok else "net"
    col = C["GREEN"] if wifi_ok else C["AMBER"]
    lw = text_w(draw, label, f)
    draw.text((W - 38 - lw, 5), label, fill=col, font=f)
    batt_s = f"{batt}%"
    bw = text_w(draw, batt_s, f)
    draw.text((W - 6 - bw, 5), batt_s, fill=C["WHITE"], font=f)


def draw_card(draw, status, source, title, subtitle, age, footer_left, footer_right):
    label, col = STATUS[status]
    card_x, card_y, card_w, card_h = 6, 23, 228, 92
    radius = 7

    # card body + status accent strip
    draw.rounded_rectangle((card_x, card_y, card_x + card_w, card_y + card_h),
                           radius=radius, fill=C["CARD"])
    draw.rounded_rectangle((card_x, card_y, card_x + 8, card_y + card_h),
                           radius=radius, fill=col)

    content_x = card_x + 14
    content_w = card_w - 24

    # header row inside card
    f_small = font(9)
    draw.text((content_x, card_y + 8), label, fill=col, font=f_small)
    src_x = content_x + 38
    src_max = W - content_x - 94
    draw.text((src_x, card_y + 8), truncate(draw, source, f_small, src_max),
              fill=C["GRAY"], font=f_small)
    age_w = text_w(draw, age, f_small)
    draw.text((W - 16 - age_w, card_y + 8), age, fill=C["GRAY"], font=f_small)

    # title (wrapped 2 lines, CJK 16px)
    f_title = font(15, cjk=True)
    lines = wrap_lines(draw, title, f_title, content_w, 2)
    for i, ln in enumerate(lines):
        draw.text((content_x, card_y + 28 + i * 18), ln, fill=C["WHITE"], font=f_title)

    # subtitle / usage line
    f_sub = font(12, cjk=True)
    draw.text((content_x, card_y + card_h - 22),
              truncate(draw, subtitle, f_sub, content_w),
              fill=C["GRAY"], font=f_sub)

    # footer
    draw.text((6, H - 12), truncate(draw, footer_left, f_small, W - 78),
              fill=C["AMBER"] if "alert" in footer_left or "tokens" in footer_left else C["GRAY"],
              font=f_small)
    fr_w = text_w(draw, footer_right, f_small)
    draw.text((W - 6 - fr_w, H - 12), footer_right, fill=C["GRAY"], font=f_small)


def draw_message(draw, title, sub, color):
    f_title = font(15, cjk=True)
    f_sub = font(12, cjk=True)
    tw = text_w(draw, title, f_title)
    draw.text(((W - tw) // 2, 50), title, fill=color, font=f_title)
    sw = text_w(draw, sub, f_sub)
    draw.text(((W - sw) // 2, 80), sub, fill=C["GRAY"], font=f_sub)
    hint = "BtnB 刷新"
    hw = text_w(draw, hint, f_sub)
    draw.text(((W - hw) // 2, 112), hint, fill=C["GRAY"], font=f_sub)


def new_canvas():
    img = Image.new("RGB", (W, H), C["BG"])
    return img, ImageDraw.Draw(img)


def render_all(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. run — Codex actively working
    img, d = new_canvas()
    draw_top_bar(d, wifi_ok=True, batt=87)
    draw_card(d, status="run", source="Codex", title="Refactor auth flow into JWT middleware",
              subtitle="Running tests · 3/8 files", age="12m",
              footer_left="12.4k tokens · 3 turns", footer_right="1/5 A")
    img.save(out_dir / "screen-run.png")

    # 2. wait — Claude Code waiting on user input
    img, d = new_canvas()
    draw_top_bar(d, wifi_ok=True, batt=86)
    draw_card(d, status="wait", source="Claude Code",
              title="是否允许写入 settings.json?",
              subtitle="等待你确认 · 2 个 alert", age="2m",
              footer_left="3 active · 1 alert", footer_right="2/5 A")
    img.save(out_dir / "screen-wait.png")

    # 3. fail — build error
    img, d = new_canvas()
    draw_top_bar(d, wifi_ok=True, batt=85)
    draw_card(d, status="fail", source="Codex",
              title="Build failed: type error in auth.ts:42",
              subtitle="Last turn errored out · click to open",
              age="8m",
              footer_left="3 active · 1 alert", footer_right="3/5 A")
    img.save(out_dir / "screen-fail.png")

    # 4. done — finished OpenClaw task
    img, d = new_canvas()
    draw_top_bar(d, wifi_ok=True, batt=84)
    draw_card(d, status="done", source="OpenClaw",
              title="迁移完成 · memory index rebuilt",
              subtitle="终态:已交付 · 1h 前完成", age="1h",
              footer_left="3 active · 1 alert", footer_right="4/5 A")
    img.save(out_dir / "screen-done.png")

    # 5. empty — no tasks
    img, d = new_canvas()
    draw_top_bar(d, wifi_ok=True, batt=87)
    draw_message(d, "暂无任务", "会定时自动刷新", C["GRAY"])
    img.save(out_dir / "screen-empty.png")

    # 6. error — hub unreachable
    img, d = new_canvas()
    draw_top_bar(d, wifi_ok=False, batt=82)
    draw_message(d, "无法读取任务", "Hub unreachable", C["RED"])
    img.save(out_dir / "screen-error.png")

    print(f"wrote 6 PNGs to {out_dir}")


if __name__ == "__main__":
    render_all(Path(__file__).resolve().parent)
