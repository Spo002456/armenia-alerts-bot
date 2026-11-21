#!/usr/bin/env python3
"""Armenia travel-advice → Telegram notifier.

Checks three official sources. For every new/changed item it sends, as
separate Telegram messages, without truncating anything:
  1. photo(s) from the item, if it has any (header as caption)
  2. header + full original English text
  3. full Armenian translation + link to the source
A text longer than Telegram's 4096-character limit continues in the next
message ("2/3", "3/3" ...).

Usage:
  python bot.py              # normal run (cron / GitHub Actions)
  python bot.py --dry-run    # print messages, send nothing, don't save state
  python bot.py --force      # send the current item of every source now
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from sources import FETCHERS, HEADERS, Item
from translate import to_armenian

STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).with_name("state.json")))
TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024
SEP = "━━━━━━━━━━━━━━"
YEREVAN = ZoneInfo("Asia/Yerevan")

MONTHS_HY = ["հունվարի", "փետրվարի", "մարտի", "ապրիլի", "մայիսի", "հունիսի",
             "հուլիսի", "օգոստոսի", "սեպտեմբերի", "հոկտեմբերի", "նոյեմբերի",
             "դեկտեմբերի"]
DATE_LABELS = {  # (Armenian, English)
    "issued": ("Թողարկվել է", "Issued"),
    "published": ("Հրապարակվել է", "Published"),
    "updated": ("Վերջին անգամ թարմացվել է", "Last updated"),
}


# --------------------------------------------------------------------------- state
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text("utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


# --------------------------------------------------------------------------- dates
def parse_date(raw: str) -> tuple[datetime | None, bool]:
    """Return (datetime, has_time)."""
    raw = (raw or "").strip()
    if not raw:
        return None, False
    try:
        return datetime.strptime(raw, "%a, %d %b %Y"), False      # date only
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(raw), True                    # RSS dates
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))    # ISO 8601
        return dt, len(raw) > 10
    except ValueError:
        return None, False


def format_date(raw: str) -> tuple[str, str]:
    """('2026 թ. նոյեմբերի 18, 16:03 (Երևանի ժամով)', '18 November 2026, 16:03 Yerevan time')"""
    dt, has_time = parse_date(raw)
    if dt is None:
        return (raw or "անհայտ"), (raw or "unknown")
    if has_time:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(YEREVAN)
    hy = f"{dt.year} թ. {MONTHS_HY[dt.month - 1]} {dt.day}"
    en = f"{dt.day} {dt.strftime('%B')} {dt.year}"
    if has_time:
        hy += f", {dt:%H:%M} (Երևանի ժամով)"
        en += f", {dt:%H:%M} Yerevan time"
    return hy, en


def checked_now() -> str:
    now = datetime.now(YEREVAN)
    return f"{now.year} թ. {MONTHS_HY[now.month - 1]} {now.day}, {now:%H:%M}"


# --------------------------------------------------------------------------- messages
def build_header(item: Item, prev_level: str = "") -> str:
    e = html.escape
    label_hy, label_en = DATE_LABELS.get(item.date_kind, DATE_LABELS["published"])
    date_hy, date_en = format_date(item.date)
    lines = [
        f"{item.source_icon} <b>ԱՂԲՅՈՒՐ՝ {e(item.source_name)}</b>",
        SEP,
        f"<b>{e(item.title)}</b>",
        "",
        f"📅 <b>{label_hy}՝ {e(date_hy)}</b>",
        f"<i>{label_en}: {e(date_en)}</i>",
    ]
    if item.level:
        lines.append(f"⚠️ {e(item.level)}")
    if prev_level and prev_level != item.level:
        lines.append(f"🚨 <b>Մակարդակը փոխվել է՝</b> {e(prev_level)} → {e(item.level)}")
    lines.append(f"🔎 <i>Ստուգվել է՝ {checked_now()}</i>")
    return "\n".join(lines)


def split_into_messages(prefix: str, body: str, suffix: str = "",
                        limit: int = TG_TEXT_LIMIT) -> list[str]:
    """Split one logical message into as many Telegram messages as needed.

    `prefix`/`suffix` are ready HTML; `body` is plain text (escaped here).
    Splits only between lines, so HTML tags are never cut in half.
    """
    lines = [html.escape(l) for l in body.split("\n")]
    # break a single over-long line into pieces at spaces
    room = limit - 200
    fixed: list[str] = []
    for l in lines:
        while len(l) > room:
            cut = l.rfind(" ", 0, room)
            cut = cut if cut > 0 else room
            fixed.append(l[:cut])
            l = l[cut:].lstrip()
        fixed.append(l)

    parts: list[list[str]] = [[]]
    size = len(prefix) + 2
    for l in fixed:
        if size + len(l) + 1 > limit - len(suffix) - 40 and parts[-1]:
            parts.append([])
            size = 40
        parts[-1].append(l)
        size += len(l) + 1

    total = len(parts)
    out = []
    for i, chunk in enumerate(parts, 1):
        text = "\n".join(chunk).strip("\n")
        head = prefix + "\n\n" if i == 1 else f"<i>…շարունակություն ({i}/{total})</i>\n\n"
        tail = ""
        if i < total:
            tail = f"\n\n<i>({i}/{total}) շարունակությունը՝ հաջորդ հաղորդագրությունում ↓</i>"
        elif suffix:
            tail = "\n\n" + suffix
        out.append(head + text + tail)
    return out


def build_messages(item: Item, hy: str, prev_level: str, with_photo: bool) -> tuple[str, list[str]]:
    """Return (photo caption, [text messages])."""
    e = html.escape
    header = build_header(item, prev_level)
    caption = header if len(header) <= TG_CAPTION_LIMIT else header[:TG_CAPTION_LIMIT]
    link = f'🔗 <a href="{e(item.url, quote=True)}">Բացել աղբյուրը / Open source</a>'

    en_prefix = "🇬🇧 <b>English (original)</b>"
    if not with_photo:                  # header goes into the first text message
        en_prefix = header + "\n\n" + en_prefix
    messages = split_into_messages(en_prefix, item.text or "(no text)")

    hy_prefix = (f"🇦🇲 <b>Հայերեն թարգմանություն</b>\n"
                 f"<i>{item.source_icon} {e(item.source_name)} — {e(item.title)}</i>")
    messages += split_into_messages(hy_prefix, hy or "(թարգմանություն չկա)", link)
    return caption, messages


# --------------------------------------------------------------------------- telegram
def _tg(method: str, data: dict | None = None, files: dict | None = None) -> dict:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    for _ in range(4):
        if files:
            r = requests.post(url, data=data, files=files, timeout=60)
        else:
            r = requests.post(url, json=data, timeout=30)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            raise RuntimeError(f"Telegram {method} failed: {r.status_code} {r.text[:300]}")
        time.sleep(1)                   # stay well under Telegram's flood limits
        return r.json()
    raise RuntimeError("Telegram rate limit: giving up")


def send_text(text: str) -> None:
    _tg("sendMessage", {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
                        "parse_mode": "HTML", "disable_web_page_preview": True})


def _download(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        if not r.headers.get("content-type", "image/").startswith("image/"):
            return None
        if len(r.content) > 10_000_000:     # Telegram photo limit
            return None
        return r.content
    except Exception as ex:
        print(f"[image] download failed {url}: {ex}", file=sys.stderr)
        return None


def send_photos(urls: list[str], caption: str) -> bool:
    """Send the item's images (album if several). Returns False if none sent."""
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    files = {}
    for i, u in enumerate(urls):
        data = _download(u)
        if data:
            files[f"p{i}"] = (f"p{i}.jpg", data)
    if not files:
        return False
    try:
        if len(files) == 1:
            name, payload = next(iter(files.items()))
            _tg("sendPhoto", {"chat_id": chat_id, "caption": caption,
                              "parse_mode": "HTML"}, {"photo": payload})
        else:
            media = []
            for n, name in enumerate(files):
                m = {"type": "photo", "media": f"attach://{name}"}
                if n == 0:
                    m.update(caption=caption, parse_mode="HTML")
                media.append(m)
            _tg("sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media)},
                files)
        return True
    except Exception as ex:
        print(f"[image] telegram rejected photos: {ex}", file=sys.stderr)
        return False


# --------------------------------------------------------------------------- main
def pick_new(source: str, items: list[Item], state: dict, force: bool) -> list[Item]:
    """Return the items that must be announced, oldest first."""
    if source == "embassy":
        seen = set(state.get("embassy", {}).get("seen", []))
        if force or not seen:          # first run: announce only the latest alert
            return items[:1]
        return [i for i in reversed(items) if i.uid not in seen]
    prev = state.get(source, {})
    if force or items[0].fingerprint != prev.get("fp"):
        return items[:1]
    return []


def remember(source: str, item: Item, state: dict) -> None:
    if source == "embassy":
        seen = state.setdefault("embassy", {}).setdefault("seen", [])
        if item.uid not in seen:
            seen.insert(0, item.uid)
        del seen[50:]
    else:
        state[source] = {"fp": item.fingerprint, "level": item.level,
                         "date": item.date}


def announce(item: Item, prev_level: str, dry_run: bool) -> None:
    hy = to_armenian(item.text)
    if dry_run:
        caption, msgs = build_messages(item, hy, prev_level, bool(item.images))
        if item.images:
            print(f"\n[PHOTO x{len(item.images)}] {item.images}\n[CAPTION]\n{caption}")
        for m in msgs:
            print("\n" + "=" * 60 + f"  ({len(m)} chars)\n" + m)
        return
    with_photo = False
    if item.images:
        caption, _ = build_messages(item, hy, prev_level, True)
        with_photo = send_photos(item.images, caption)
    _, msgs = build_messages(item, hy, prev_level, with_photo)
    for m in msgs:
        send_text(m)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    state = load_state()
    failures = 0
    for source, fetch in FETCHERS.items():
        try:
            items = fetch()
        except Exception as ex:
            failures += 1
            print(f"[{source}] fetch failed: {ex}", file=sys.stderr)
            continue
        new = pick_new(source, items, state, args.force)
        if source == "embassy" and not state.get("embassy", {}).get("seen"):
            for it in items[1:]:     # don't replay old alerts on the first run
                remember(source, it, state)
        print(f"[{source}] {len(items)} item(s), {len(new)} to send")
        for item in new:
            prev_level = state.get(source, {}).get("level", "") if source == "state_dept" else ""
            try:
                announce(item, prev_level, args.dry_run)
                if not args.dry_run:
                    remember(source, item, state)
                    save_state(state)   # save after each item: no duplicates on crash
            except Exception as ex:
                failures += 1
                print(f"[{source}] telegram send failed: {ex}", file=sys.stderr)

    if not args.dry_run:
        save_state(state)
    return 1 if failures == len(FETCHERS) else 0


if __name__ == "__main__":
    sys.exit(main())
