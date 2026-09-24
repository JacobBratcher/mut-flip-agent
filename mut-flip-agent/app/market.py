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
class ProgramMove:
    program: str
    change: float
    cards: int


@dataclass
class MarketMove:
    change_24h: float | None
    change_7d: float | None
    cards_24h: int
    fallers: list = field(default_factory=list)
    risers: list = field(default_factory=list)
    breadth_down: float | None = None     # share of cards down 10%+ since yesterday
    programs: list = field(default_factory=list)


RECENT = 6 * HOUR       # "now" = a card's latest sales, if it sold in the last 6h
LATEST = 3              # ...using up to its 3 most recent sales


def _latest(sales, now):
    """Median of a card's most recent sales, only if they're fresh."""
    fresh = sorted((t, p) for p, t in sales if now - RECENT <= t <= now + 1)[-LATEST:]
    return median(p for _, p in fresh) if len(fresh) >= MIN_SALES else None


def _baseline(sales, now, back):
    """Median price over the 24h window that ended `back` seconds ago."""
    old = [p for p, t in sales if now - back - DAY <= t < now - back]
    return median(old) if len(old) >= MIN_SALES else None


def program_of(name, url):
    """'T.J. Watt Team of the Week 87 OVR' + '/players/12562-tj-watt/...' -> 'Team of the Week'."""
    m = re.search(r"/players/\d+-([a-z0-9-]+)/", url or "")
    if not name or not m:
        return None
    words = name.split()
    if len(words) > 2 and re.fullmatch(r"\d{2}", words[-2]) and words[-1].upper() == "OVR":
        words = words[:-2]
    player = m.group(1).split("-")
    norm = lambda w: re.sub(r"[^a-z0-9]", "", w.lower())
    i = 0
    for part in player:
        if i < len(words) and norm(words[i]) == part:
            i += 1
    while i < len(words) and norm(words[i]) in {"jr", "sr", "ii", "iii", "iv", "v"}:
        i += 1
    if i == 0 or i >= len(words):
        return None
    return " ".join(words[i:])


def market_move(cards, now, top=5) -> MarketMove:
    """cards: [(uid, name, url, [(price, ts), ...])].

    Each card's latest sales (last 6h) vs. the same card yesterday, so a crash that
    started mid-day shows up in full instead of being averaged with the morning.
    The market figure is the median card, so one card can't move it.
    """
    day, week, movers, by_program = [], [], [], {}
    for uid, name, url, sales in cards:
        cur = _latest(sales, now)
        if cur is None:
            continue
        prev = _baseline(sales, now, DAY)
        if prev:
            ch = cur / prev - 1
            day.append(ch)
            movers.append(Mover(uid, name or uid, url, int(cur), ch))
            prog = program_of(name, url)
            if prog:
                by_program.setdefault(prog, []).append(ch)
        wk = _baseline(sales, now, 7 * DAY)
        if wk:
            week.append(cur / wk - 1)
    movers.sort(key=lambda m: m.change)
    programs = sorted((ProgramMove(p, median(c), len(c)) for p, c in by_program.items()
                       if len(c) >= MIN_PROGRAM_CARDS), key=lambda p: p.change)
    enough = len(day) >= MIN_CARDS
    return MarketMove(
        change_24h=median(day) if enough else None,
        change_7d=median(week) if len(week) >= MIN_CARDS else None,
        cards_24h=len(day),
        fallers=[m for m in movers[:top] if m.change < 0],
        risers=[m for m in reversed(movers[-top:]) if m.change > 0],
        breadth_down=(sum(1 for c in day if c <= -0.10) / len(day)) if enough else None,
        programs=programs,
    )


MIN_PROGRAM_CARDS = 4


def classify(change, threshold):
    """'crash' / 'bump' / None for a market change vs. the configured threshold (e.g. 0.08)."""
    if change is None:
        return None
    if change <= -threshold:
        return "crash"
    if change >= threshold:
        return "bump"
    return None


# ------------------------------------------------------------------ playbook
# Timing rules from GutFoxx (gutfoxx.com/tag/madden-market) and the mut.gg community.
SEASON_CRASHES = [
    # (start month, start day, end month, end day, what)
    (10, 20, 11, 10, "Road to the Playoffs: historically a 50%+ crash on most cards"),
    (1, 1, 2, 15, "Team of the Year + Super Bowl: the biggest crash of the year"),
    (4, 15, 5, 5, "NFL Draft program: most non-top cards sink as players chase coins"),
]


def season_warning(today, lead_days=14):
    """A heads-up when a historically big crash window is near or underway."""
    from datetime import date
    for sm, sd, em, ed, what in SEASON_CRASHES:
        for y in (today.year - 1, today.year, today.year + 1):
            start = date(y, sm, sd)
            end = date(y if (em, ed) >= (sm, sd) else y + 1, em, ed)
            if start <= today <= end:
                return f"Now: {what}. Don't hold cards; flip fast."
            days = (start - today).days
            if 0 < days <= lead_days:
                return f"In {days} days: {what}. Sell anything you're holding before it starts."
    return None


def timing_tips(now_dt, promo_today):
    """Plain-language timing tips for today."""
    tips = []
    if promo_today:
        tips.append("Promo day: older cards dip while packs get ripped. Buy the dip; prices "
                    "usually bounce the next day, so sell into that bounce (GutFoxx).")
    wd = now_dt.weekday()           # Mon=0
    if wd in (1, 2, 3):
        tips.append("Midweek (Tue-Thu) is usually the cheapest time to buy.")
    elif wd in (4, 5):
        tips.append("Fri/Sat prices usually run higher as people upgrade for the weekend: "
                    "good time to sell.")
    warn = season_warning(now_dt.date())
    if warn:
        tips.append(warn)
    return tips


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
