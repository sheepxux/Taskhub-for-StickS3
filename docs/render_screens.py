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
    "LOVABLE_RED": rgb565(0xFA20),
    "LOVABLE_ORANGE": rgb565(0xFD20),
    "LOVABLE_SHADOW": rgb565(0x5BFF),
    "BLUE": rgb565(0x5BDF),          # the firmware's accent blue
    "TEAL": rgb565(0x867F),
    "MAGENTA": rgb565(0xF81F),
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


def draw_taskhub_mark(draw, x, y, scale, color):
    def px(px_x, px_y, ww, hh):
        x0 = x + px_x * scale
        y0 = y + px_y * scale
        draw.rectangle((x0, y0, x0 + ww * scale - 1, y0 + hh * scale - 1), fill=color)

    px(6, 1, 15, 1)
    px(5, 2, 1, 11)
    px(20, 2, 1, 12)
    px(6, 13, 15, 1)
    px(21, 3, 2, 1)
    px(22, 4, 1, 10)
    px(21, 14, 2, 1)

    px(8, 4, 12, 1)
    px(8, 5, 1, 8)
    px(19, 5, 1, 8)
    px(9, 12, 10, 1)
    px(12, 7, 4, 1)
    px(10, 9, 8, 1)
    px(11, 11, 6, 1)

    px(4, 14, 17, 1)
    px(3, 15, 1, 4)
    px(21, 15, 1, 4)
    px(4, 18, 17, 1)
    px(6, 16, 2, 1)
    px(17, 16, 5, 1)
    px(22, 15, 1, 3)
    px(20, 19, 3, 1)

    px(3, 18, 1, 1)
    px(2, 19, 1, 1)
    px(1, 20, 1, 1)
    px(0, 21, 20, 1)
    px(20, 19, 1, 1)
    px(19, 20, 1, 1)


def draw_taskhub_mini_mark(draw, x, y, color):
    draw.rectangle((x + 4, y, x + 12, y + 6), outline=color)
    draw.rectangle((x + 5, y + 2, x + 11, y + 5), outline=color)
    draw.line((x + 13, y + 1, x + 15, y + 3, x + 15, y + 9), fill=color)
    draw.line((x + 4, y + 8, x + 14, y + 8), fill=color)
    draw.rectangle((x + 3, y + 9, x + 15, y + 11), outline=color)
    draw.line((x + 3, y + 12, x + 1, y + 15, x + 13, y + 15, x + 16, y + 12), fill=color)
    draw.line((x + 7, y + 3, x + 9, y + 3), fill=color)
    draw.line((x + 7, y + 5, x + 11, y + 5), fill=color)


def source_logo_color(source):
    s = source.lower()
    if "codex" in s:
        return C["BLUE"]
    if "claude" in s:
        return C["AMBER"]
    if "perplexity" in s:
        return C["WHITE"]
    if "gemini" in s:
        return C["BLUE"]
    if "lovable" in s:
        return C["LOVABLE_RED"]
    if "manus" in s:
        return C["GREEN"]
    if "openclaw" in s or "claw" in s:
        return C["RED"]
    return C["GRAY"]


def draw_ai_source_icon(draw, source, x, y, bg):
    s = source.lower()
    c = source_logo_color(source)
    draw.rectangle((x, y, x + 11, y + 11), fill=bg)
    if "codex" in s:
        draw.ellipse((x + 1, y + 2, x + 7, y + 8), fill=c)
        draw.ellipse((x + 3, y + 1, x + 11, y + 9), fill=c)
        draw.ellipse((x + 5, y + 5, x + 11, y + 11), fill=c)
        draw.rectangle((x + 2, y + 5, x + 9, y + 10), fill=c)
        draw.line((x + 3, y + 4, x + 5, y + 6, x + 3, y + 8), fill=C["WHITE"])
        draw.line((x + 7, y + 8, x + 9, y + 8), fill=C["WHITE"])
    elif "claude" in s:
        draw.line((x + 6, y + 1, x + 6, y + 10), fill=c)
        draw.line((x + 1, y + 6, x + 10, y + 6), fill=c)
        draw.line((x + 3, y + 3, x + 9, y + 9), fill=c)
        draw.line((x + 9, y + 3, x + 3, y + 9), fill=c)
    elif "perplexity" in s:
        draw.line((x + 6, y, x + 6, y + 11), fill=c)
        draw.line((x + 1, y + 6, x + 11, y + 6), fill=c)
        draw.line((x + 2, y + 1, x + 6, y + 5, x + 10, y + 1), fill=c)
        draw.line((x + 2, y + 11, x + 6, y + 7, x + 10, y + 11), fill=c)
        draw.line((x + 2, y + 1, x + 2, y + 11), fill=c)
        draw.line((x + 10, y + 1, x + 10, y + 11), fill=c)
    elif "gemini" in s:
        draw.polygon((x + 6, y, x + 8, y + 5, x + 11, y + 6, x + 8, y + 7,
                      x + 6, y + 11, x + 4, y + 7, x, y + 6, x + 4, y + 5), fill=c)
    elif "lovable" in s:
        draw.rectangle((x + 2, y + 8, x + 8, y + 10), fill=C["LOVABLE_SHADOW"])
        draw.ellipse((x + 1, y + 1, x + 7, y + 7), fill=C["LOVABLE_ORANGE"])
        draw.ellipse((x + 5, y + 1, x + 11, y + 7), fill=C["LOVABLE_RED"])
        draw.polygon((x + 1, y + 5, x + 11, y + 5, x + 6, y + 11), fill=C["LOVABLE_RED"])
        draw.polygon((x + 2, y + 5, x + 7, y + 5, x + 6, y + 10), fill=C["LOVABLE_ORANGE"])
        draw.point((x + 6, y + 2), fill=C["LOVABLE_RED"])
        draw.point((x, y + 2), fill=bg)
        draw.point((x + 11, y + 2), fill=bg)
    elif "manus" in s:
        draw.line((x + 2, y + 2, x + 2, y + 10), fill=c)
        draw.line((x + 10, y + 2, x + 10, y + 10), fill=c)
        draw.line((x + 3, y + 3, x + 6, y + 7, x + 9, y + 3), fill=c)
    elif "openclaw" in s or "claw" in s:
        draw.line((x + 3, y + 1, x + 1, y), fill=c)
        draw.line((x + 9, y + 1, x + 11, y), fill=c)
        draw.ellipse((x + 1, y + 1, x + 11, y + 11), fill=c)
        draw.ellipse((x - 1, y + 4, x + 3, y + 8), fill=c)
        draw.ellipse((x + 9, y + 4, x + 13, y + 8), fill=c)
        draw.rectangle((x + 4, y + 10, x + 5, y + 11), fill=c)
        draw.rectangle((x + 7, y + 10, x + 8, y + 11), fill=c)
        draw.ellipse((x + 3, y + 4, x + 5, y + 6), fill=C["BG"])
        draw.ellipse((x + 7, y + 4, x + 9, y + 6), fill=C["BG"])
    else:
        draw.rectangle((x + 1, y + 1, x + 10, y + 10), outline=c)
        draw.line((x + 3, y + 9, x + 6, y + 2, x + 9, y + 9), fill=c)
        draw.line((x + 4, y + 6, x + 8, y + 6), fill=c)


def draw_top_bar(draw, wifi_ok=True, batt=87):
    f = font(9)
    draw_taskhub_mini_mark(draw, 5, 2, C["BLUE"])
    draw.text((26, 5), "TaskHub", fill=C["GRAY"], font=f)
    label = "wifi" if wifi_ok else "net"
    col = C["GREEN"] if wifi_ok else C["AMBER"]
    lw = text_w(draw, label, f)
    draw.text((W - 38 - lw, 5), label, fill=col, font=f)
    batt_s = f"{batt}%"
    bw = text_w(draw, batt_s, f)
    draw.text((W - 6 - bw, 5), batt_s, fill=C["WHITE"], font=f)


def draw_card(draw, status, source, title, subtitle, age, footer_left, footer_right, device="Mini"):
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
    src_icon_x = content_x + 38
    src_x = src_icon_x + 15
    src_max = W - src_x - 58
    draw_ai_source_icon(draw, source, src_icon_x, card_y + 7, C["CARD"])
    source_label = f"{source}@{device}" if device else source
    draw.text((src_x, card_y + 8), truncate(draw, source_label, f_small, src_max),
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


def draw_boot(draw, status="sync..."):
    scale = 3
    icon_w, icon_h = 24 * scale, 22 * scale
    icon_x = (W - icon_w) // 2
    icon_y = 8
    draw_taskhub_mark(draw, icon_x, icon_y, scale, C["BLUE"])

    f_title = font(16)
    title = "TaskHub"
    tw = text_w(draw, title, f_title)
    draw.text(((W - tw) // 2, icon_y + icon_h + 7), title, fill=C["BLUE"], font=f_title)
    f_byline = font(9)
    byline = "Developed by Axu"
    bw = text_w(draw, byline, f_byline)
    draw.text(((W - bw) // 2, icon_y + icon_h + 28), byline, fill=C["GRAY"], font=f_byline)
    f_status = font(9)
    sw = text_w(draw, status, f_status)
    draw.text(((W - sw) // 2, H - 22), status, fill=C["GRAY"], font=f_status)


def draw_wake_sync(draw, status="wifi..."):
    draw_top_bar(draw, wifi_ok=False, batt=87)
    f_title = font(15, cjk=True)
    title = "连接 Wi-Fi"
    tw = text_w(draw, title, f_title)
    draw.text(((W - tw) // 2, 48), title, fill=C["BLUE"], font=f_title)
    f_sub = font(12, cjk=True)
    sub = "同步任务状态"
    sw = text_w(draw, sub, f_sub)
    draw.text(((W - sw) // 2, 78), sub, fill=C["GRAY"], font=f_sub)
    f_status = font(9)
    stw = text_w(draw, status, f_status)
    draw.text(((W - stw) // 2, H - 22), status, fill=C["GRAY"], font=f_status)


def new_canvas():
    img = Image.new("RGB", (W, H), C["BG"])
    return img, ImageDraw.Draw(img)


def render_all(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. boot — logo shown while Wi-Fi/hub sync runs
    img, d = new_canvas()
    draw_boot(d, status="sync...")
    img.save(out_dir / "screen-boot.png")

    # 2. wake — deep-sleep wake reconnects without the brand splash
    img, d = new_canvas()
    draw_wake_sync(d, status="wifi...")
    img.save(out_dir / "screen-wake.png")

    # 3. run — Codex actively working
    img, d = new_canvas()
    draw_top_bar(d, wifi_ok=True, batt=87)
    draw_card(d, status="run", source="Codex", title="Refactor auth flow into JWT middleware",
              subtitle="Running tests · 3/8 files", age="12m",
              footer_left="12.4k tokens · 3 turns", footer_right="1/5 A")
    img.save(out_dir / "screen-run.png")

    # 4. wait — Claude Code waiting on user input
    img, d = new_canvas()
    draw_top_bar(d, wifi_ok=True, batt=86)
    draw_card(d, status="wait", source="Claude Code",
              title="是否允许写入 settings.json?",
              subtitle="等待你确认 · 2 个 alert", age="2m",
              footer_left="3 active · 1 alert", footer_right="2/5 A")
    img.save(out_dir / "screen-wait.png")

    # 5. fail — build error
    img, d = new_canvas()
    draw_top_bar(d, wifi_ok=True, batt=85)
    draw_card(d, status="fail", source="Codex",
              title="Build failed: type error in auth.ts:42",
              subtitle="Last turn errored out · click to open",
              age="8m",
              footer_left="3 active · 1 alert", footer_right="3/5 A")
    img.save(out_dir / "screen-fail.png")

    # 6. done — finished OpenClaw task
    img, d = new_canvas()
    draw_top_bar(d, wifi_ok=True, batt=84)
    draw_card(d, status="done", source="OpenClaw",
              title="迁移完成 · memory index rebuilt",
              subtitle="终态:已交付 · 1h 前完成", age="1h",
              footer_left="3 active · 1 alert", footer_right="4/5 A")
    img.save(out_dir / "screen-done.png")

    # 7. empty — no tasks
    img, d = new_canvas()
    draw_top_bar(d, wifi_ok=True, batt=87)
    draw_message(d, "暂无任务", "会定时自动刷新", C["GRAY"])
    img.save(out_dir / "screen-empty.png")

    # 8. error — hub unreachable
    img, d = new_canvas()
    draw_top_bar(d, wifi_ok=False, batt=82)
    draw_message(d, "无法读取任务", "Hub unreachable", C["RED"])
    img.save(out_dir / "screen-error.png")

    print(f"wrote 8 PNGs to {out_dir}")


if __name__ == "__main__":
    render_all(Path(__file__).resolve().parent)
