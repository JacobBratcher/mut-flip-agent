"""Follow MUT YouTubers: newest uploads per channel, one request per channel per hour.

Uses the channel's official RSS feed, and falls back to reading the channel's
Videos tab when YouTube's feed is flaky (it intermittently answers 404/500).
"""
import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

import requests

UA = {"User-Agent": "Mozilla/5.0 (MUT-Flip-Agent; follows a few channels hourly)",
      "Accept-Language": "en-US"}
CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
# 💰 market/coins advice, and 🔮 leaks / upcoming content (what moves prices next).
MARKET_WORDS = ("market", "crash", "invest", "sell", "coin", "flip", "snipe", "price", "profit",
                "cheap", "spend", "save", "hold", "value", "what to do", "buy")
LEAK_WORDS = ("leak", "schedule", "coming", "before", "increase", "reveal", "next",
              "do this now", "do this first")


@dataclass
class Video:
    id: str
    title: str
    channel: str
    published: datetime | None = None
    age: str | None = None           # "1 day ago" when only the Videos tab was readable

    @property
    def url(self):
        return f"https://www.youtube.com/watch?v={self.id}"


def classify(title):
    """'market', 'leak' or None. Only classified videos are posted instantly."""
    t = title.lower()
    if any(w in t for w in MARKET_WORDS):
        return "market"
    if any(w in t for w in LEAK_WORDS):
        return "leak"
    return None


ICON = {"market": "💰", "leak": "🔮", None: "📺"}


def is_market_video(title):
    return classify(title) == "market"


def resolve(session: requests.Session, ident: str) -> str | None:
    """'@GutFoxx' or a channel id -> channel id."""
    ident = ident.strip()
    if CHANNEL_ID.match(ident):
        return ident
    handle = ident if ident.startswith("@") else "@" + ident
    r = session.get(f"https://www.youtube.com/{handle}", headers=UA, timeout=30)
    if r.status_code != 200:
        return None
    m = re.search(r'"externalId":"(UC[\w-]{22})"', r.text)
    return m.group(1) if m else None


def parse_feed(xml_text) -> list[Video]:
    root = ET.fromstring(xml_text)
    author = root.find("a:title", ATOM)
    out = []
    for e in root.findall("a:entry", ATOM):
        vid, title, pub = e.find("yt:videoId", ATOM), e.find("a:title", ATOM), e.find("a:published", ATOM)
        if vid is None or title is None:
            continue
        when = None
        if pub is not None and pub.text:
            try:
                when = datetime.fromisoformat(pub.text)
            except ValueError:
                pass
        out.append(Video(vid.text, title.text or "", author.text if author is not None else "", when))
    return out


def parse_videos_tab(page, channel="") -> list[Video]:
    """Decode the embedded upload data without regex backtracking over HTML.

    YouTube adds fields between metadata entries. The former cross-page regex
    could spend minutes retrying its wildcards and hold the GIL, stalling the
    feeder HTTP threads too. JSON decoding and an iterative walk are bounded by
    the page size and keep each title attached to its own video ID.
    """
    name = re.search(r'<meta property="og:title" content="([^"]+)"', page)
    channel = channel or (html.unescape(name.group(1)) if name else "")
    marker = re.search(r'(?:\bytInitialData|window\["ytInitialData"\])\s*=\s*', page)
    if not marker:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(page, marker.end())
    except (ValueError, RecursionError):
        return []
    out, seen, pending = [], set(), [data]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(reversed(node))
            continue
        if not isinstance(node, dict):
            continue
        video = node.get("lockupViewModel")
        if isinstance(video, dict) and video.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
            uid = video.get("contentId", "")
            meta = video.get("metadata", {}).get("lockupMetadataViewModel", {})
            title = meta.get("title", {}).get("content", "")
            rows = meta.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
            age = next((part.get("text", {}).get("content", "")
                        for row in rows for part in row.get("metadataParts", [])
                        if part.get("text", {}).get("content", "").endswith("ago")), "")
            if re.fullmatch(r"[\w-]{11}", uid) and title and uid not in seen:
                seen.add(uid)
                out.append(Video(uid, title, channel, None, age))
            continue
        pending.extend(reversed(list(node.values())))
    return out


def latest(session: requests.Session, channel_id: str) -> list[Video]:
    r = session.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                    headers=UA, timeout=30)
    if r.status_code == 200:
        try:
            vids = parse_feed(r.content)
            if vids:
                return vids
        except ET.ParseError:
            pass
    r = session.get(f"https://www.youtube.com/channel/{channel_id}/videos", headers=UA, timeout=30)
    r.raise_for_status()
    return parse_videos_tab(r.text)
