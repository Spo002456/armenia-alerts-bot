"""English -> Armenian translation of the full text (no length limit).

Tries several translators in order, with retries, until one works:
  1. Claude API          (only if ANTHROPIC_API_KEY is set; best quality)
  2. Google (gtx API)    free, no key
  3. deep-translator     free Google web endpoint
  4. MyMemory            free, small daily quota (last resort)
Long texts are split into paragraph groups and translated piece by piece.
"""
from __future__ import annotations

import os
import sys
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ArmeniaAlertsBot/1.0"}
FAILED = "(Թարգմանությունը հասանելի չէ — տե՛ս անգլերեն բնօրինակը)"

PROMPT = (
    "Translate the following official travel-safety notice from English into "
    "Armenian (Eastern Armenian). Keep the meaning exact and the tone official. "
    "Keep bullet points, line breaks, phone numbers, URLs and abbreviations "
    "(STEP, FCDO, U.S.) as they are. Write Armenian place names in their usual "
    "Armenian form (e.g. Syunik -> Սյունիք, Kapan -> Կապան). "
    "Output only the translation.\n\n"
)


def log(msg: str) -> None:
    print(f"[translate] {msg}", file=sys.stderr, flush=True)


def split_paragraphs(text: str, limit: int) -> list[str]:
    """Group paragraphs into chunks of at most `limit` characters."""
    chunks, cur = [], ""
    for para in text.split("\n"):
        while len(para) > limit:                 # a single huge paragraph
            cut = para.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(para[:cut])
            para = para[cut:].lstrip()
        if len(cur) + len(para) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += para + "\n"
    if cur.strip():
        chunks.append(cur)
    return [c.rstrip("\n") for c in chunks]


# --------------------------------------------------------------------------- engines
def _claude(text: str) -> str:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5"),
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": PROMPT + text}],
        },
        timeout=120,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"]).strip()


def _google_gtx(text: str) -> str:
    r = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "en", "tl": "hy", "dt": "t", "q": text},
        headers=UA,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    out = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
    if not out.strip():
        raise RuntimeError("empty response")
    return out.strip()


def _deep_google(text: str) -> str:
    from deep_translator import GoogleTranslator
    out = GoogleTranslator(source="en", target="hy").translate(text)
    if not out or not out.strip():
        raise RuntimeError("empty response")
    return out.strip()


def _mymemory(text: str) -> str:
    from deep_translator import MyMemoryTranslator
    tr = MyMemoryTranslator(source="en-GB", target="hy-AM")
    return "\n".join((tr.translate(p) or "") for p in split_paragraphs(text, 450)).strip()


ENGINES = [  # (name, function, chunk size)
    ("claude", _claude, 3000),
    ("google-gtx", _google_gtx, 1500),
    ("deep-translator", _deep_google, 4500),
    ("mymemory", _mymemory, 4000),
]


def _with_retries(name: str, fn, text: str, tries: int = 3) -> str:
    last = None
    for attempt in range(1, tries + 1):
        try:
            return fn(text)
        except Exception as ex:
            last = ex
            log(f"{name} attempt {attempt}/{tries} failed: {type(ex).__name__}: {ex}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"{name} failed: {last}")


def to_armenian(text: str) -> str:
    if not text.strip():
        return ""
    for name, fn, size in ENGINES:
        if name == "claude" and not os.getenv("ANTHROPIC_API_KEY"):
            continue
        try:
            parts = [_with_retries(name, fn, chunk) for chunk in split_paragraphs(text, size)]
            log(f"translated with {name}")
            return "\n".join(parts)
        except Exception as ex:
            log(f"switching engine: {ex}")
    log("ALL translation engines failed")
    return FAILED


if __name__ == "__main__":   # quick manual test:  python translate.py
    print(to_armenian("Exercise increased caution in Armenia.\n• Avoid demonstrations."))
