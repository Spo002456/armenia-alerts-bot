"""Fetchers for the three Armenia travel-advice sources.

Each fetcher returns a list of Item objects (newest first). The bot decides
what is "new" by comparing Item.uid / Item.fingerprint with the saved state.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "ArmeniaAlertsBot/1.0 (personal Telegram notifier)"}
TIMEOUT = 30
MAX_IMAGES = 5

STATE_DEPT_FEED = "https://travel.state.gov/_res/rss/TAsTWs.xml"
STATE_DEPT_PAGE = "https://travel.state.gov/en/international-travel/travel-advisories/armenia.html"
EMBASSY_FEED = "https://am.usembassy.gov/category/alert/feed/"
EMBASSY_PAGE = "https://am.usembassy.gov/category/alert/"
GOVUK_API = "https://www.gov.uk/api/content/foreign-travel-advice/armenia"
GOVUK_PAGE = "https://www.gov.uk/foreign-travel-advice/armenia"


@dataclass
class Item:
    source: str            # state key: "state_dept" | "embassy" | "govuk"
    source_name: str       # shown in the message header
    source_icon: str
    uid: str               # stable id of the item (link / guid)
    title: str
    date: str              # raw date string from the source
    date_kind: str         # "issued" | "published" | "updated"
    url: str
    text: str              # plain-text English body (full, never truncated)
    level: str = ""        # State Dept threat level, if any
    images: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        raw = "|".join([self.uid, self.title, self.date, self.level, self.text])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- helpers
def html_to_text(html: str) -> str:
    """Turn an HTML fragment into readable plain text with bullet points."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "img", "figure", "picture"]):
        tag.decompose()
    for li in soup.find_all("li"):
        li.insert_before("\n• ")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "ul", "ol"]):
        block.insert_after("\n")
    text = soup.get_text()
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\n• \n+", "\n• ", text)
    text = re.sub(r"\n\n+•", "\n•", text)
    return text.strip()


_SKIP_IMG = re.compile(r"logo|icon|avatar|emoji|pixel|spacer|seal|flag|badge|sprite",
                       re.I)
_IMG_EXT = re.compile(r"\.(jpe?g|png|webp)(\?|$)", re.I)


def extract_images(html: str, base_url: str) -> list[str]:
    """Content images inside an HTML fragment (skips logos, icons, tiny images)."""
    soup = BeautifulSoup(html or "", "html.parser")
    found: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        if not src and img.get("srcset"):
            src = img["srcset"].split(",")[-1].strip().split(" ")[0]
        if not src or src.startswith("data:"):
            continue
        src = urljoin(base_url, src)
        if _SKIP_IMG.search(src) or not _IMG_EXT.search(src):
            continue
        try:
            if int(img.get("width", 999)) < 150 or int(img.get("height", 999)) < 100:
                continue
        except ValueError:
            pass
        if src not in found:
            found.append(src)
    return found[:MAX_IMAGES]


def feed_images(entry, base_url: str, body_html: str) -> list[str]:
    """Images attached to an RSS entry (media:content, enclosures) + inside body."""
    urls: list[str] = []
    for m in entry.get("media_content", []) + entry.get("media_thumbnail", []):
        if m.get("url") and (m.get("medium") in (None, "image")):
            urls.append(m["url"])
    for enc in entry.get("enclosures", []):
        if (enc.get("type") or "").startswith("image/") and enc.get("href"):
            urls.append(enc["href"])
    urls += extract_images(body_html, base_url)
    clean: list[str] = []
    for u in urls:
        if u not in clean and not _SKIP_IMG.search(u):
            clean.append(u)
    return clean[:MAX_IMAGES]


def _get(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------
# 1. U.S. Department of State — Travel Advisory (RSS, all countries)
# --------------------------------------------------------------------------
def fetch_state_dept(country_tag: str = "AM") -> list[Item]:
    feed = feedparser.parse(_get(STATE_DEPT_FEED).content)
    for e in feed.entries:
        # <category domain="Country-Tag">AM</category> -> tags[].scheme / term
        tags = {(t.get("scheme") or "").strip(): (t.get("term") or "").strip()
                for t in e.get("tags", [])}
        ident = (e.get("dc_identifier") or "").split(",")[0].strip()
        if tags.get("Country-Tag") == country_tag or ident == country_tag:
            level = tags.get("Threat-Level", "")
            if not level:
                m = re.search(r"Level \d:[^<\n]*", e.get("title", ""))
                level = m.group(0).strip() if m else ""
            body = e.get("summary", "")
            return [Item(
                source="state_dept",
                source_name="U.S. Department of State — Travel Advisory",
                source_icon="🇺🇸",
                uid=e.get("id") or e.get("link") or STATE_DEPT_PAGE,
                title=e.get("title", "Armenia Travel Advisory").strip(),
                date=e.get("published", ""),
                date_kind="issued",
                url=STATE_DEPT_PAGE,
                text=html_to_text(body),
                level=level,
                images=feed_images(e, STATE_DEPT_PAGE, body),
            )]
    raise RuntimeError(f"Country-Tag {country_tag} not found in State Dept feed")


# --------------------------------------------------------------------------
# 2. U.S. Embassy Yerevan — Alerts
#    The site blocks unknown bots, so we look like a normal browser and try
#    three entry points: RSS feed -> WordPress JSON API -> HTML page.
# --------------------------------------------------------------------------
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "application/rss+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,hy;q=0.8",
    "Referer": "https://am.usembassy.gov/",
}
EMBASSY_API = "https://am.usembassy.gov/wp-json/wp/v2"


def _embassy_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s


def _embassy_item(uid, title, date, link, body_html) -> Item:
    return Item(
        source="embassy",
        source_name="U.S. Embassy Yerevan — Alert",
        source_icon="🏛",
        uid=uid or link,
        title=html_to_text(title).strip(),
        date=date,
        date_kind="published",
        url=link,
        text=html_to_text(body_html),
        images=extract_images(body_html, link),
    )


def _embassy_from_feed(s: requests.Session, limit: int) -> list[Item]:
    r = s.get(EMBASSY_FEED, timeout=TIMEOUT)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    if not feed.entries:
        raise RuntimeError("feed has no entries")
    items = []
    for e in feed.entries[:limit]:
        body = e.content[0].get("value", "") if e.get("content") else ""
        body = body or e.get("summary", "")
        link = e.get("link", EMBASSY_PAGE)
        it = _embassy_item(e.get("id"), e.get("title", ""), e.get("published", ""),
                           link, body)
        it.images = feed_images(e, link, body)
        items.append(it)
    return items


def _embassy_from_api(s: requests.Session, limit: int) -> list[Item]:
    r = s.get(f"{EMBASSY_API}/categories", params={"slug": "alert"}, timeout=TIMEOUT)
    r.raise_for_status()
    cats = r.json()
    if not cats:
        raise RuntimeError("category 'alert' not found")
    r = s.get(f"{EMBASSY_API}/posts", timeout=TIMEOUT,
              params={"categories": cats[0]["id"], "per_page": limit, "_embed": 1})
    r.raise_for_status()
    items = []
    for p in r.json():
        body = p.get("content", {}).get("rendered", "")
        it = _embassy_item(p.get("guid", {}).get("rendered") or p.get("link"),
                           p.get("title", {}).get("rendered", ""),
                           p.get("date_gmt", "") + ("Z" if p.get("date_gmt") else ""),
                           p.get("link", EMBASSY_PAGE), body)
        media = (p.get("_embedded", {}).get("wp:featuredmedia") or [{}])[0]
        if media.get("source_url") and not _SKIP_IMG.search(media["source_url"]):
            it.images = [media["source_url"]] + [i for i in it.images
                                                 if i != media["source_url"]]
        items.append(it)
    if not items:
        raise RuntimeError("API returned no posts")
    return items


def _embassy_from_html(s: requests.Session, limit: int) -> list[Item]:
    r = s.get(EMBASSY_PAGE, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.select("h2 a, h3 a, .entry-title a"):
        href = a.get("href", "")
        if href.startswith("https://am.usembassy.gov/") and href not in links:
            links.append(href)
    if not links:
        raise RuntimeError("no alert links on page")
    items = []
    for href in links[:limit]:
        pr = s.get(href, timeout=TIMEOUT)
        pr.raise_for_status()
        page = BeautifulSoup(pr.text, "html.parser")
        title = page.find("h1")
        body = page.select_one(".entry-content, article")
        meta = page.find("meta", attrs={"property": "article:published_time"})
        tm = page.find("time")
        date = (meta.get("content") if meta else "") or \
               (tm.get("datetime") if tm and tm.get("datetime") else "")
        items.append(_embassy_item(href, title.get_text(strip=True) if title else href,
                                   date, href, str(body) if body else ""))
    return items


def fetch_embassy(limit: int = 5) -> list[Item]:
    s = _embassy_session()
    errors = []
    for name, fn in (("rss", _embassy_from_feed), ("wp-json", _embassy_from_api),
                     ("html", _embassy_from_html)):
        try:
            items = fn(s, limit)
            print(f"[embassy] {name}: OK ({len(items)} alerts)")
            return items
        except Exception as ex:
            print(f"[embassy] {name}: FAILED - {type(ex).__name__}: {ex}")
            errors.append(f"{name}: {ex}")
    raise RuntimeError("all embassy methods failed -> " + " | ".join(errors))


# --------------------------------------------------------------------------
# 3. GOV.UK — FCDO Foreign travel advice (Content API, JSON)
# --------------------------------------------------------------------------
def _part_body(part: dict) -> str:
    body = part.get("body", "")
    if isinstance(body, list):  # some formats return [{content_type, content}]
        html = next((b.get("content", "") for b in body
                     if b.get("content_type") == "text/html"), "")
        return html or (body[0].get("content", "") if body else "")
    return body


def fetch_govuk() -> list[Item]:
    data = _get(GOVUK_API).json()
    details = data.get("details", {})
    change = (details.get("change_description") or "").strip()
    updated = data.get("public_updated_at", "")
    parts = details.get("parts", [])
    preferred = ("warnings-and-insurance", "summary")
    part = next((p for p in parts if p.get("slug") in preferred),
                parts[0] if parts else None)
    body_html = _part_body(part) if part else ""
    body = html_to_text(body_html)
    text = f"Latest update: {change}\n\n{body}".strip() if change else body
    images = extract_images(body_html, GOVUK_PAGE)
    for img in (details.get("image") or {}, ):   # rarely present
        if img.get("url"):
            images.insert(0, img["url"])
    return [Item(
        source="govuk",
        source_name="GOV.UK — FCDO Foreign Travel Advice",
        source_icon="🇬🇧",
        uid=GOVUK_PAGE,
        title="Armenia travel advice",
        date=updated,
        date_kind="updated",
        url=GOVUK_PAGE,
        text=text,
        images=images[:MAX_IMAGES],
        extra={"change": change},
    )]


FETCHERS = {
    "state_dept": fetch_state_dept,
    "embassy": fetch_embassy,
    "govuk": fetch_govuk,
}
