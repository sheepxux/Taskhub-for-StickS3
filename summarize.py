"""
StickS3 voice-recorder daily summarizer (Step 3).

Reads memory/voice/YYYY-MM-DD.md, classifies the day's entries into
产品点子 / 阅读笔记 / 待办 / 其他, and writes a concise digest to
memory/voice/YYYY-MM-DD-summary.md.

Uses the Claude API when ANTHROPIC_API_KEY is set; otherwise falls back to a
keyword heuristic so a summary is always produced. Meant to run from a 22:00
cron, but runnable by hand:

  python3 summarize.py                 # today (CST)
  python3 summarize.py --date 2026-05-28
"""
from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import llm

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
# Same VOICE_DIR as transcribe.py — override to OpenClaw's indexed memory dir.
VOICE_DIR = Path(os.environ.get("VOICE_DIR", ROOT / "memory" / "voice"))

CATEGORIES = ["产品点子", "阅读笔记", "待办", "其他"]

# Heading like "## 14:32 · 标题 #待办"; the trailing #tag is optional and stripped.
_ENTRY_RE = re.compile(r"^##\s+(\d{1,2}:\d{2})\s+·\s+(.*?)(?:\s+#\S+)?$")


@dataclass
class Entry:
    time: str
    title: str
    body: str


def _today_str() -> str:
    return f"{datetime.now(CST):%Y-%m-%d}"


def parse_day(md: str) -> list[Entry]:
    entries: list[Entry] = []
    cur: Entry | None = None
    buf: list[str] = []
    for line in md.splitlines():
        m = _ENTRY_RE.match(line)
        if m:
            if cur is not None:
                cur.body = "\n".join(buf).strip()
                entries.append(cur)
            cur = Entry(time=m.group(1), title=m.group(2).strip(), body="")
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        cur.body = "\n".join(buf).strip()
        entries.append(cur)
    return entries


# ---------------------------------------------------------------- heuristic

_KEYWORDS = {
    "待办": ["待办", "记得", "要做", "明天", "下周", "deadline", "买", "打电话", "todo", "提醒"],
    "阅读笔记": ["读", "书", "文章", "paper", "blog", "作者", "看了", "论文", "读到"],
    "产品点子": ["点子", "想法", "idea", "产品", "功能", "做一个", "可以做", "灵感"],
}


def classify_text(text: str) -> str:
    """Keyword-heuristic tag for a single note. Shared with transcribe.py so the
    per-note "sticky" tag and the 22:00 summary use the same category logic."""
    for cat in ("待办", "产品点子", "阅读笔记"):
        if any(kw in text for kw in _KEYWORDS[cat]):
            return cat
    return "其他"


def _classify_heuristic(e: Entry) -> str:
    return classify_text(f"{e.title} {e.body}")


def summarize_heuristic(entries: list[Entry], date: str) -> str:
    buckets: dict[str, list[Entry]] = {c: [] for c in CATEGORIES}
    for e in entries:
        buckets[_classify_heuristic(e)].append(e)
    counts = " · ".join(f"{c} {len(buckets[c])}" for c in CATEGORIES if buckets[c])
    lines = [f"# {date} 日记摘要", "", f"共 {len(entries)} 条 · {counts}", ""]
    for cat in CATEGORIES:
        if not buckets[cat]:
            continue
        lines.append(f"## {cat}")
        for e in buckets[cat]:
            bullet = "- [ ] " if cat == "待办" else "- "
            teaser = e.body.replace("\n", " ").strip()
            teaser = (teaser[:50] + "…") if len(teaser) > 50 else teaser
            lines.append(f"{bullet}{e.time} {e.title}" + (f" — {teaser}" if teaser else ""))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------- LLM

_SYSTEM = (
    "你是一个语音备忘录的每日总结助手。把当天的零散语音转录条目，"
    "分类聚合成简洁中文摘要。只输出 Markdown，不要寒暄。"
)


def _build_prompt(entries: list[Entry], date: str) -> str:
    lines = [f"日期：{date}，共 {len(entries)} 条记录。请按以下分类聚合：产品点子 / 阅读笔记 / 待办 / 其他。", ""]
    for e in entries:
        lines.append(f"[{e.time}] {e.title}：{e.body}")
    lines += [
        "",
        "输出格式（严格遵守）：",
        f"# {date} 日记摘要",
        "",
        f"共 {len(entries)} 条 · <各非空分类及计数，用 · 分隔>",
        "",
        "## <分类名>",
        "- <要点，待办用 - [ ] >",
        "",
        "省略没有内容的分类。每条要点尽量精炼。",
    ]
    return "\n".join(lines)


def summarize_llm(entries: list[Entry], date: str) -> str:
    prompt = _build_prompt(entries, date)
    text = llm.call_claude(prompt, system=_SYSTEM, max_tokens=1500)
    if not text.lstrip().startswith("#"):
        text = f"# {date} 日记摘要\n\n" + text
    return text.strip() + "\n"


# ---------------------------------------------------------------- driver


def run(date: str) -> Path | None:
    day_file = VOICE_DIR / f"{date}.md"
    if not day_file.exists():
        print(f"summarize: no entries file {day_file}")
        return None
    entries = parse_day(day_file.read_text(encoding="utf-8"))
    if not entries:
        print(f"summarize: {day_file} has no entries")
        return None

    if llm.available():
        try:
            summary = summarize_llm(entries, date)
            engine = "llm"
        except llm.LLMUnavailable as e:
            print(f"summarize: LLM unavailable ({e}); using heuristic")
            summary = summarize_heuristic(entries, date)
            engine = "heuristic"
    else:
        summary = summarize_heuristic(entries, date)
        engine = "heuristic"

    out = VOICE_DIR / f"{date}-summary.md"
    out.write_text(summary, encoding="utf-8")
    print(f"summarize: wrote {out} ({len(entries)} entries, engine={engine})")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=_today_str(), help="YYYY-MM-DD (CST), default today")
    args = ap.parse_args()
    run(args.date)


if __name__ == "__main__":
    main()
