"""Follow MUT YouTubers: newest uploads per channel, one request per channel per hour.

Uses the channel's official RSS feed, and falls back to reading the channel's
Videos tab when YouTube's feed is flaky (it intermittently answers 404/500).
"""
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

import requests

UA = {"User-Agent": "Mozilla/5.0 (MUT-Flip-Agent; follows a few channels hourly)",
      "Accept-Language": "en-US"}
CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
MARKET_WORDS = ("market", "crash", "invest", "sell", "buy", "coin", "flip", "snipe", "price",
                "what to do", "content coming", "spend", "save", "hold", "profit", "cheap")


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


def is_market_video(title):
    t = title.lower()
    return any(w in t for w in MARKET_WORDS)


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
    """Newest-first uploads from a channel's /videos page (YouTube's 2025+ lockup layout)."""
    name = re.search(r'<meta property="og:title" content="([^"]+)"', page)
    channel = channel or (html.unescape(name.group(1)) if name else "")
    out, seen = [], set()
    pat = (r'"lockupMetadataViewModel":\{"title":\{"content":"(.*?)"\}.*?"metadataParts":'
           r'\[\{"text":\{"content":"[^"]*"\}\},\{"text":\{"content":"([^"]*ago)"')
    for m in re.finditer(pat, page):
        ids = re.findall(r'"videoId":"([\w-]{11})"', page[max(0, m.start() - 8000):m.start()])
        if not ids or ids[-1] in seen:
            continue
        seen.add(ids[-1])
        title = json_unescape(m.group(1))
        out.append(Video(ids[-1], title, channel, None, m.group(2)))
    return out


def json_unescape(s):
    try:
        import json
        return json.loads(f'"{s}"')
    except ValueError:
        return s


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
