"""Market-wide view: price index, crash/bump detection, promo news, Twitch drops.

The analysis functions are pure (no I/O) so they can be unit tested; the two
fetchers at the bottom are small and polite (a few requests per day).
"""
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median

import requests

HOUR = 3600
DAY = 86400
MIN_CARDS = 10          # need at least this many cards with data before calling a market move
MIN_SALES = 2           # sales needed in each window for a card to count


@dataclass
class Mover:
    uid: str
    name: str
    url: str
    now_price: int
    change: float        # e.g. -0.18 for an 18% drop


@dataclass
class MarketMove:
    change_24h: float | None
    change_7d: float | None
    cards_24h: int
    fallers: list = field(default_factory=list)
    risers: list = field(default_factory=list)


def _window(sales, start, end):
    return [p for p, t in sales if start <= t < end]


def _change(sales, now, back):
    """Card price now vs. `back` seconds ago, from sales in two matching 24h windows."""
    cur = _window(sales, now - DAY, now + 1)
    prev = _window(sales, now - back - DAY, now - back)
    if len(cur) < MIN_SALES or len(prev) < MIN_SALES:
        return None, None
    c, p = median(cur), median(prev)
    return (c / p - 1 if p else None), c


def market_move(cards, now, top=5) -> MarketMove:
    """cards: [(uid, name, url, [(price, ts), ...])]. Index = median change across cards,
    so one card's spike or crash can't move it."""
    day, week, movers = [], [], []
    for uid, name, url, sales in cards:
        ch, cur = _change(sales, now, DAY)
        if ch is not None:
            day.append(ch)
            movers.append(Mover(uid, name or uid, url, int(cur), ch))
        ch7, _ = _change(sales, now, 7 * DAY)
        if ch7 is not None:
            week.append(ch7)
    movers.sort(key=lambda m: m.change)
    return MarketMove(
        change_24h=median(day) if len(day) >= MIN_CARDS else None,
        change_7d=median(week) if len(week) >= MIN_CARDS else None,
        cards_24h=len(day),
        fallers=[m for m in movers[:top] if m.change < 0],
        risers=[m for m in reversed(movers[-top:]) if m.change > 0],
    )


def classify(change, threshold):
    """'crash' / 'bump' / None for a market change vs. the configured threshold (e.g. 0.08)."""
    if change is None:
        return None
    if change <= -threshold:
        return "crash"
    if change >= threshold:
        return "bump"
    return None


# ------------------------------------------------------------------ news
@dataclass
class Article:
    url: str
    title: str
    published: datetime


PROMO_WORDS = ("team of the week", "totw", "ltd", "legends", "part ", "program", "promo",
               "team builders", "1on1", "redux", "game time", "unreal", "crystal",
               "pregame", "heroes", "collectors", "golden ticket", "zero chill", "most feared")


def is_promo(title):
    t = title.lower()
    return any(w in t for w in PROMO_WORDS)


def parse_news_sitemap(xml_text) -> list[Article]:
    """mut.gg's Google-News sitemap: loc + title + publication_date per article."""
    root = ET.fromstring(xml_text)
    out = []
    for url in root:
        loc = title = pub = None
        for el in url.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "loc":
                loc = (el.text or "").strip()
            elif tag == "title":
                title = html.unescape((el.text or "").strip())
            elif tag == "publication_date":
                try:
                    pub = datetime.fromisoformat((el.text or "").strip())
                except ValueError:
                    pub = None
        if loc and title and pub:
            out.append(Article(loc, title, pub))
    return sorted(out, key=lambda a: a.published, reverse=True)


def fetch_news(session: requests.Session) -> list[Article]:
    r = session.get("https://www.mut.gg/sitemap-news.xml", timeout=30,
                    headers={"Accept": "application/xml"})
    r.raise_for_status()
    return parse_news_sitemap(r.content)


# ----------------------------------------------------------------- drops
DROPS_URL = "https://twitchdrops.app/api/chatbot/madden-nfl-27"
DROPS_PAGE = "https://twitchdrops.app/game/madden-nfl-27"


def parse_drops(text) -> str | None:
    """The chatbot endpoint answers in one line; None when no drop is live."""
    text = " ".join((text or "").split())
    if not text or not re.search(r"\b\d+\s+rewards?\b", text, re.I):
        return None
    if re.search(r"\b0\s+rewards?\b", text, re.I):
        return None
    return text


def fetch_drops(session: requests.Session) -> str | None:
    r = session.get(DROPS_URL, timeout=20, headers={"Accept": "text/plain"})
    r.raise_for_status()
    return parse_drops(r.text)
