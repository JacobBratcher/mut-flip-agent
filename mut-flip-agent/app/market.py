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
# Learned from this season's own sales, not from old guides. The only outside
# rule kept is that prices dip when a promo drops and recover within a day or two,
# which a current MUT 27 guide confirms (timesaver.gg auction house guide, 2026).
PROMO_SOURCE = "https://timesaver.gg/blog/madden-nfl-27-auction-house-guide-make-coins-flipping"
CLUSTER = 3 * HOUR          # promos released together (TOTW + Team Builders) count once


@dataclass
class PromoReaction:
    ts: float
    title: str
    dip: float          # median change 2-14h after the drop vs. the 6h before
    next_day: float     # median change 24-40h after vs. before
    cards: int


def _median_in(sales, start, end):
    ps = [p for p, t in sales if start <= t < end]
    return median(ps) if len(ps) >= MIN_SALES else None


def promo_reactions(cards, promos, now):
    """promos: [(ts, title)]. For each promo with a full next day behind it, how the cards
    that already existed moved: the dip right after, and where they were the next day."""
    events = []
    for ts, title in sorted(promos):
        if events and ts - events[-1][0] < CLUSTER:
            events[-1][1].append(title)
        else:
            events.append((ts, [title]))
    out = []
    for ts, titles in events:
        if now < ts + 40 * HOUR:
            continue
        dips, nexts = [], []
        for _uid, _name, _url, sales in cards:
            pre = _median_in(sales, ts - 6 * HOUR, ts)
            dip = _median_in(sales, ts + 2 * HOUR, ts + 14 * HOUR)
            nxt = _median_in(sales, ts + 24 * HOUR, ts + 40 * HOUR)
            if pre and dip and nxt:
                dips.append(dip / pre - 1)
                nexts.append(nxt / pre - 1)
        if len(dips) >= MIN_CARDS:
            out.append(PromoReaction(ts, " + ".join(titles), median(dips), median(nexts), len(dips)))
    return out


def weekday_pattern(cards, now, days=28):
    """Median price by weekday relative to each card's own weekly level, across cards.
    Returns {0..6: deviation} only with 2+ weeks of data; Mon=0."""
    by_day, dates = {}, {}
    for _uid, _name, _url, sales in cards:
        daily = {}
        for p, t in sales:
            if t >= now - days * DAY:
                daily.setdefault(int(t // DAY), []).append(p)
        med = {d: median(v) for d, v in daily.items() if len(v) >= MIN_SALES}
        for d, m in med.items():
            around = [med[x] for x in range(d - 3, d + 4) if x in med]
            if len(around) >= 5:
                wd = datetime.fromtimestamp(d * DAY).weekday()
                by_day.setdefault(wd, []).append(m / median(around) - 1)
                dates.setdefault(wd, set()).add(d)
    # every weekday must have been seen on 2+ different dates (i.e. 2+ weeks), by 20+ cards
    if (len(by_day) < 7 or min(len(v) for v in by_day.values()) < 20
            or min(len(v) for v in dates.values()) < 2):
        return {}
    return {wd: median(v) for wd, v in by_day.items()}


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def timing_tips(now_dt, promo_today, reactions=(), weekdays=None, schedule=()):
    """Plain-language tips for today, from what this season's market actually did."""
    tips = []
    tomorrow = (now_dt.weekday() + 1) % 7
    nxt = [f"{p} ~{t}" for p, wd, t in schedule if wd == tomorrow]
    if nxt:
        tips.append(f"Tomorrow ({DAYS[tomorrow]}): {', '.join(nxt)} usually drops. "
                    "Keep coins free for the dip.")
    recent = list(reactions)[-5:]
    if len(recent) >= 2:
        dip = median(r.dip for r in recent)
        nxt = median(r.next_day for r in recent)
        bounced = sum(1 for r in recent if r.next_day > r.dip)
        summary = (f"After the last {len(recent)} promos on PC, existing cards moved "
                   f"{dip * 100:+.0f}% in the first 12h and sat at {nxt * 100:+.0f}% the next day "
                   f"(bounced {bounced} of {len(recent)} times).")
        if promo_today:
            advice = (" Buy the dip today and sell into tomorrow's bounce." if nxt > dip
                      else " The dip has kept going the next day, so don't rush to buy.")
            tips.append("Promo day. " + summary + advice)
        else:
            tips.append(summary)
    elif promo_today:
        tips.append("Promo day: existing cards usually dip while packs get opened and recover "
                    "within a day or two. Buy the dip, sell into the recovery. "
                    "(Still measuring how PC reacts this season.)")
    if weekdays:
        lo = min(weekdays, key=weekdays.get)
        hi = max(weekdays, key=weekdays.get)
        if weekdays[hi] - weekdays[lo] >= 0.03:
            today = now_dt.weekday()
            line = (f"This season, prices run lowest on {DAYS[lo]} ({weekdays[lo] * 100:+.0f}%) "
                    f"and highest on {DAYS[hi]} ({weekdays[hi] * 100:+.0f}%).")
            if today == lo:
                line += " Today's a good day to buy."
            elif today == hi:
                line += " Today's a good day to sell."
            tips.append(line)
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


NOT_PROMO = ("contest", "prediction", "how to", "guide", "tier list", "ranking", "best ",
             "what are", "explained", "review")


def is_promo(title):
    t = title.lower()
    return any(w in t for w in PROMO_WORDS) and not any(w in t for w in NOT_PROMO)


def program_name(title):
    """'Team of the Week 2: Travis Kelce…' -> 'Team of the Week'; 'Unreal Moments Part 2.5: …' -> 'Unreal Moments'."""
    head = title.split(":")[0]
    head = re.sub(r"\s+(Part\s+[\d.]+|\d+(\.\d+)?)$", "", head.strip(), flags=re.I)
    return re.sub(r"^Preseason\s+", "", head.strip(), flags=re.I)


def promo_schedule(promos, min_repeats=2, window_min=90):
    """Programs that keep dropping on the same weekday around the same time:
    [(program, weekday, 'H:MM AM')], learned from observed release times."""
    by = {}
    for ts, title in promos:
        dt = datetime.fromtimestamp(ts)
        by.setdefault((program_name(title), dt.weekday()), []).append(dt.hour * 60 + dt.minute)
    out = []
    for (prog, wd), mins in by.items():
        # anchor on the release time with the most others near it (ignores one-off odd times)
        mid = max(mins, key=lambda m: sum(abs(x - m) <= window_min for x in mins))
        close = [m for m in mins if abs(m - mid) <= window_min]
        if len(close) >= min_repeats:
            h, m = divmod(int(median(close)), 60)
            out.append((prog, wd, datetime(2000, 1, 1, h, m).strftime("%-I:%M %p")))
    return sorted(out, key=lambda x: (x[1], x[2]))


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


def parse_article(page):
    """(title, published) from a mut.gg article page's JSON-LD and og:title."""
    date = re.search(r'"datePublished":\s*"([^"]+)"', page)
    title = re.search(r'<meta property="og:title" content="([^"]+)"', page)
    if not date or not title:
        return None
    t = re.sub(r"\s*-\s*MUT\.GG\s*$", "", html.unescape(title.group(1)))
    try:
        return t, datetime.fromisoformat(date.group(1))
    except ValueError:
        return None


def backfill_promos(session: requests.Session, since_ts, limit=25, pause=2.0):
    """Promo release times from recent articles (the archive sitemap has no dates, so read
    each article page, newest first, politely). Stops at the first article older than since."""
    import time as _time
    r = session.get("https://www.mut.gg/sitemap-all-news.xml", timeout=30)
    r.raise_for_status()
    locs = re.findall(r"<loc>([^<]+)</loc>", r.text)[:limit]
    out = []
    for url in locs:
        _time.sleep(pause)
        page = session.get(url, timeout=30)
        if page.status_code != 200:
            continue
        got = parse_article(page.text)
        if not got:
            continue
        title, when = got
        if when.timestamp() < since_ts:
            break
        if is_promo(title):
            out.append((when.timestamp(), title))
    return out


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
