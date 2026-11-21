"""English -> Armenian translation of the full text (no length limit).

Long texts are split into paragraph groups and translated piece by piece.
Uses the Claude API when ANTHROPIC_API_KEY is set (better quality, keeps
place names and phone numbers intact); otherwise falls back to the free
Google Translate endpoint via deep-translator.
"""
from __future__ import annotations

import os
import sys

import requests

PROMPT = (
    "Translate the following official travel-safety notice from English into "
    "Armenian (Eastern Armenian). Keep the meaning exact and the tone official. "
    "Keep bullet points, line breaks, phone numbers, URLs and abbreviations "
    "(STEP, FCDO, U.S.) as they are. Write Armenian place names in their usual "
    "Armenian form (e.g. Syunik -> Սյունիք, Kapan -> Կապան). "
    "Output only the translation.\n\n"
)


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


def _claude(text: str, key: str) -> str:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
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


def _google(text: str) -> str:
    from deep_translator import GoogleTranslator

    tr = GoogleTranslator(source="en", target="hy")
    return (tr.translate(text) or "").strip()


def to_armenian(text: str) -> str:
    if not text.strip():
        return ""
    key = os.getenv("ANTHROPIC_API_KEY")
    use_claude = bool(key)
    out = []
    for chunk in split_paragraphs(text, 3000 if use_claude else 4500):
        translated = None
        if use_claude:
            try:
                translated = _claude(chunk, key)
            except Exception as e:
                print(f"[translate] Claude API failed, using Google: {e}", file=sys.stderr)
        if translated is None:
            try:
                translated = _google(chunk)
            except Exception as e:
                print(f"[translate] Google failed: {e}", file=sys.stderr)
                return "(Թարգմանությունը հասանելի չէ — տե՛ս անգլերեն բնօրինակը)"
        out.append(translated)
    return "\n".join(out)
