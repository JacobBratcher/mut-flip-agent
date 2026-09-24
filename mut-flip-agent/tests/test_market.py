import time
from datetime import datetime, timezone

from app import main, market
from app.config import DEFAULTS
from tests.test_agent import FakeDiscord

NOW = time.time()
H, D = 3600, 86400


def card(uid, prev_price, cur_price, week_price=None):
    """Two sales in each 24h window: yesterday, today (and optionally a week ago)."""
    sales = [(prev_price, NOW - 30 * H), (prev_price, NOW - 40 * H),
             (cur_price, NOW - 1 * H), (cur_price, NOW - 4 * H)]
    if week_price:
        sales += [(week_price, NOW - 7 * D - 2 * H), (week_price, NOW - 7 * D - 10 * H)]
    return (uid, "Player Legends 86 OVR", f"https://www.mut.gg/players/1-player/{uid}/", sales)


def test_market_crash_detected():
    cards = [card(f"27-{i}", 100_000, 88_000, week_price=120_000) for i in range(12)]
    m = market.market_move(cards, NOW)
    assert round(m.change_24h, 2) == -0.12 and m.cards_24h == 12
    assert round(m.change_7d, 3) == round(88_000 / 120_000 - 1, 3)
    assert market.classify(m.change_24h, 0.08) == "crash"


def test_one_card_cannot_move_the_market():
    cards = [card(f"27-{i}", 100_000, 100_000) for i in range(11)]
    cards.append(card("27-99", 100_000, 20_000))          # one card collapses
    m = market.market_move(cards, NOW)
    assert m.change_24h == 0 and market.classify(m.change_24h, 0.08) is None
    assert m.fallers[0].uid == "27-99" and round(m.fallers[0].change, 2) == -0.8
    assert round(m.breadth_down, 2) == round(1 / 12, 2)


def test_not_enough_cards_is_not_a_signal():
    m = market.market_move([card(f"27-{i}", 100_000, 50_000) for i in range(5)], NOW)
    assert m.change_24h is None and market.classify(m.change_24h, 0.08) is None


def test_market_bump():
    cards = [card(f"27-{i}", 100_000, 110_000) for i in range(12)]
    assert market.classify(market.market_move(cards, NOW).change_24h, 0.08) == "bump"


NEWS = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="https://www.sitemaps.org/schemas/sitemap/0.9"
  xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://www.mut.gg/news/team-of-the-week-2/</loc><news:news>
<news:publication_date>2026-09-23T10:52:04.324340-04:00</news:publication_date>
<news:title><![CDATA[ Team of the Week 2: Travis Kelce and More ]]></news:title></news:news></url>
<url><loc>https://www.mut.gg/news/how-to-get-kendal-daniels/</loc><news:news>
<news:publication_date>2026-09-22T09:00:00-04:00</news:publication_date>
<news:title><![CDATA[ How to Get 86 OVR MCS Kendal Daniels in MUT 27 ]]></news:title></news:news></url>
<url><loc>https://www.mut.gg/news/team-builders-part-2/</loc><news:news>
<news:publication_date>2026-09-23T10:46:21-04:00</news:publication_date>
<news:title><![CDATA[ Team Builders Part 2: Ja&#x27;Marr Chase ]]></news:title></news:news></url>
</urlset>"""


def test_parse_news_newest_first_and_promo_tagging():
    arts = market.parse_news_sitemap(NEWS)
    assert [a.url.rsplit("/", 2)[1] for a in arts] == [
        "team-of-the-week-2", "team-builders-part-2", "how-to-get-kendal-daniels"]
    assert arts[1].title == "Team Builders Part 2: Ja'Marr Chase"
    assert market.is_promo(arts[0].title) and market.is_promo(arts[1].title)
    assert not market.is_promo(arts[2].title)


def test_parse_drops():
    live = ("Drops for Madden NFL 27: 1 reward — Madden Twitch Pack (15m). Ends Sep 25. "
            "Claim: twitch.tv/drops/inventory | Details: twitchdrops.app/game/madden-nfl-27")
    assert market.parse_drops(live) == live
    assert market.parse_drops("Drops for Madden NFL 27: 0 rewards.") is None
    assert market.parse_drops("No active drops for Madden NFL 27.") is None
    assert market.parse_drops("") is None


class Disc(FakeDiscord):
    def __init__(self):
        super().__init__()
        self.news_posts, self.drop_posts, self.alerts, self.reports = [], [], [], []

    def news(self, a, promo):
        self.news_posts.append((a.title, promo))

    def drops(self, text):
        self.drop_posts.append(text)

    def market_alert(self, kind, move, platform):
        self.alerts.append(kind)

    def market_report(self, move, news, drops, snipes, platform, **kw):
        self.reports.append((move, news, drops))


def agent(tmp_path, monkeypatch, **market_cfg):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = DEFAULTS | {"discord_webhook_url": "x", "discover_all_players": False,
                      "market": DEFAULTS["market"] | market_cfg, "youtube_channels": []}
    a = main.Agent(cfg)
    a.discord = Disc()
    a.db.put("promo_backfilled", "1")
    return a


def test_news_first_run_is_silent_then_posts_only_new(tmp_path, monkeypatch):
    a = agent(tmp_path, monkeypatch)
    arts = market.parse_news_sitemap(NEWS)
    monkeypatch.setattr(market, "fetch_news", lambda s: arts[1:])
    a._check_news()
    assert a.discord.news_posts == []                      # existing articles aren't spammed
    monkeypatch.setattr(market, "fetch_news", lambda s: arts)
    a._check_news()
    assert a.discord.news_posts == [("Team of the Week 2: Travis Kelce and More", True)]
    a._check_news()
    assert len(a.discord.news_posts) == 1                  # not reposted


def test_drop_reminder_once_per_campaign(tmp_path, monkeypatch):
    a = agent(tmp_path, monkeypatch)
    live = "Drops for Madden NFL 27: 1 reward — Madden Twitch Pack (15m). Ends Sep 25."
    monkeypatch.setattr(market, "fetch_drops", lambda s: live)
    a._check_drops(); a._check_drops()
    assert a.discord.drop_posts == [live]
    monkeypatch.setattr(market, "fetch_drops", lambda s: None)
    a._check_drops()
    monkeypatch.setattr(market, "fetch_drops", lambda s: live)
    a._check_drops()                                        # a new campaign later alerts again
    assert len(a.discord.drop_posts) == 2


def _seed_crash(a):
    for i in range(12):
        uid = f"27-{i}"
        a.db.upsert_item(uid, "")
        for p, t in card(uid, 100_000, 85_000)[3]:
            iso = datetime.fromtimestamp(t, timezone.utc).isoformat()
            a.db.add_sales(uid, [(p, iso)])


def test_crash_alert_once_and_daily_report(tmp_path, monkeypatch):
    a = agent(tmp_path, monkeypatch, report_hour=0, news=False, twitch_drops=False)
    _seed_crash(a)
    a.maybe_market()
    assert a.discord.alerts == ["crash"] and len(a.discord.reports) == 1
    assert round(a.move.change_24h, 2) == -0.15
    a.next["market"] = 0
    a.maybe_market()                                        # same day: no repeat alert or report
    assert a.discord.alerts == ["crash"] and len(a.discord.reports) == 1


def test_midday_crash_shows_in_full():
    """Crash at 10:50 AM, measured at midnight: the old whole-day average showed about half of it."""
    cards = []
    for i in range(12):
        sales = [(100_000, NOW - h * H) for h in range(26, 48, 2)]          # yesterday: 100k
        sales += [(100_000, NOW - h * H) for h in range(14, 24, 2)]         # this morning: 100k
        sales += [(75_000, NOW - h * H) for h in range(1, 13, 2)]           # after the drop: 75k
        cards.append((f"27-{i}", f"P{i} Team of the Week 86 OVR", f"https://www.mut.gg/players/{i}-p{i}/27-{i}/", sales))
    m = market.market_move(cards, NOW)
    assert round(m.change_24h, 2) == -0.25 and m.breadth_down == 1.0
    assert m.programs[0].program == "Team of the Week" and round(m.programs[0].change, 2) == -0.25


def test_program_split_shows_opposite_moves():
    legends = [card(f"27-L{i}", 100_000, 70_000) for i in range(6)]
    builders = [(f"27-B{i}", f"B{i} Team Builders 85 OVR", f"https://www.mut.gg/players/{i}-b{i}/27-B{i}/",
                 card("x", 100_000, 160_000)[3]) for i in range(6)]
    m = market.market_move(legends + builders, NOW)
    progs = {p.program: round(p.change, 2) for p in m.programs}
    assert progs == {"Legends": -0.3, "Team Builders": 0.6}


def _promo_cards(pre, dip, nxt, n=12, ts=None):
    """Cards that sold at `pre` before a promo at ts, `dip` right after, `nxt` the next day."""
    ts = ts or NOW - 3 * D
    cards = []
    for i in range(n):
        sales = [(pre, ts - h * H) for h in (1, 3, 5)]
        sales += [(dip, ts + h * H) for h in (4, 8, 12)]
        sales += [(nxt, ts + h * H) for h in (26, 30, 36)]
        cards.append((f"27-{i}", f"P{i} Core 84 OVR", f"https://www.mut.gg/players/{i}-p{i}/27-{i}/", sales))
    return cards, ts


def test_promo_reaction_dip_then_bounce():
    cards, ts = _promo_cards(100_000, 88_000, 96_000)
    r = market.promo_reactions(cards, [(ts, "TOTW 2"), (ts + 600, "Team Builders 2")], NOW)
    assert len(r) == 1 and r[0].title == "TOTW 2 + Team Builders 2"      # same-time drops count once
    assert round(r[0].dip, 2) == -0.12 and round(r[0].next_day, 2) == -0.04 and r[0].cards == 12


def test_promo_too_recent_is_not_scored():
    cards, ts = _promo_cards(100_000, 88_000, 96_000, ts=NOW - 20 * H)
    assert market.promo_reactions(cards, [(ts, "TOTW 3")], NOW) == []


def test_timing_tips_use_measured_reactions():
    from datetime import datetime
    r = [market.PromoReaction(NOW - 9 * D, "TOTW 1", -0.10, -0.03, 40),
         market.PromoReaction(NOW - 2 * D, "TOTW 2", -0.14, -0.05, 40)]
    tips = market.timing_tips(datetime(2026, 9, 30, 12), True, r)
    assert "last 2 promos" in tips[0] and "-12%" in tips[0] and "bounced 2 of 2" in tips[0]
    assert "sell into tomorrow's bounce" in tips[0]
    falling = [market.PromoReaction(NOW - 9 * D, "a", -0.10, -0.15, 40),
               market.PromoReaction(NOW - 2 * D, "b", -0.08, -0.12, 40)]
    assert "don't rush" in market.timing_tips(datetime(2026, 9, 30, 12), True, falling)[0]
    # nothing measured yet: generic, sourced line on promo days only, no weekday claims
    assert "Still measuring" in market.timing_tips(datetime(2026, 9, 30, 12), True)[0]
    assert market.timing_tips(datetime(2026, 10, 2, 12), False) == []


def test_weekday_pattern_needs_two_weeks_and_finds_cheap_day():
    from datetime import datetime
    cards = []
    for i in range(25):
        sales = []
        for d in range(1, 22):
            t = NOW - d * D
            p = 90_000 if datetime.fromtimestamp((t // D) * D).weekday() == 1 else 100_000   # Tuesdays cheap
            sales += [(p, t), (p, t + 60), (p, t + 120)]
        cards.append((f"27-{i}", "x", "", sales))
    w = market.weekday_pattern(cards, NOW)
    assert min(w, key=w.get) == 1 and round(w[1], 2) == -0.10
    short = [(u, n, url, [s for s in sales if s[1] > NOW - 6 * D]) for u, n, url, sales in cards]
    assert market.weekday_pattern(short, NOW) == {}


def test_parse_article_date():
    page = ('<meta property="og:title" content="Team of the Week 2: Travis Kelce and More - MUT.GG">'
            '<script>{"datePublished": "2026-09-23T10:52:04.324340-04:00"}</script>')
    title, when = market.parse_article(page)
    assert title == "Team of the Week 2: Travis Kelce and More" and when.hour == 10


def test_program_alert_fires_once(tmp_path, monkeypatch):
    a = agent(tmp_path, monkeypatch, news=False, twitch_drops=False, report=False)
    a.discord.program_alerts = []
    a.discord.program_alert = lambda kind, p, move, platform: a.discord.program_alerts.append((kind, p.program))
    for uid, name, url, sales in [card(f"27-{i}", 100_000, 70_000) for i in range(12)]:
        a.db.upsert_item(uid, url); a.db.set_name(uid, name)
        a.db.add_sales(uid, [(p, datetime.fromtimestamp(t, timezone.utc).isoformat()) for p, t in sales])
    a._check_market(); a._check_market()
    assert a.discord.program_alerts == [("crash", "Legends")] and a.discord.alerts == ["crash"]


def test_youtube_first_look_silent_then_new_uploads(tmp_path, monkeypatch):
    from app import youtube
    a = agent(tmp_path, monkeypatch)
    a.cfg["youtube_channels"] = ["UCd0jBryyetpHJ5DhPlKQ_sA"]
    posted = []
    a.discord.video = lambda v, kind: posted.append((v.title, kind))
    old = [youtube.Video("aaaaaaaaaaa", "Ranking The BEST QBs in MUT 27!", "GutFoxx")]
    monkeypatch.setattr(youtube, "latest", lambda s, cid: old)
    a._check_youtube()
    assert posted == []
    new = [youtube.Video("ccccccccccc", "Top 5 Plays of the Week", "GutFoxx"),
           youtube.Video("bbbbbbbbbbb", "MARKET CRASH! Sell these now", "GutFoxx"),
           youtube.Video("ddddddddddd", "UNSTOPPABLE PROMO LEAKED! Full Content Schedule", "GutFoxx")] + old
    monkeypatch.setattr(youtube, "latest", lambda s, cid: new)
    a._check_youtube(); a._check_youtube()
    # gameplay video waits for the report; market + leak videos post right away, once
    assert sorted(posted) == [("MARKET CRASH! Sell these now", "market"),
                              ("UNSTOPPABLE PROMO LEAKED! Full Content Schedule", "leak")]
    assert a.videos[-1].id in ("ccccccccccc", "aaaaaaaaaaa")      # unflagged sorted last


def test_videos_tab_parser_on_real_page():
    import os
    from app import youtube
    if not os.path.exists("/tmp/gfv.html"):
        return
    vids = youtube.parse_videos_tab(open("/tmp/gfv.html", encoding="utf-8", errors="ignore").read())
    assert len(vids) >= 10 and vids[0].channel == "GutFoxx" and len(vids[0].id) == 11


def test_video_classification_on_real_titles():
    from app.youtube import classify
    assert classify("REDUX AND WHAT TO DO THIS WEEK IN MUT 27!") == "market"
    assert classify("UNSTOPPABLE PROMO LEAKED! New Collector Series + FULL Content Schedule") == "leak"
    assert classify("DO THIS NOW BEFORE THE UNSTOPPABLE PROMO IN MADDEN 27!") == "leak"
    assert classify("I Did THE MOST REWARDING MISSION in MUT 27! (2M Coins)") == "market"
    assert classify("Ranking The BEST QBs in MUT 27!") is None
    assert classify("The Wheel of MUT! Madden 27 Season Opener") is None


def test_chatty_channel_leak_videos_are_rate_limited(tmp_path, monkeypatch):
    from app import youtube
    a = agent(tmp_path, monkeypatch)
    a.cfg["youtube_channels"] = ["UCvD5D-RRf0bXWX-gV5FD3xA"]
    posted = []
    a.discord.video = lambda v, kind: posted.append(kind)
    feed = [youtube.Video("a" * 11, "old video", "Moshi")]
    monkeypatch.setattr(youtube, "latest", lambda s, cid: feed)
    a._check_youtube()
    feed[:0] = [youtube.Video(c * 11, f"UPDATES ON EVERYTHING! LEAKS #{c}", "Moshi") for c in "bcd"]
    feed.insert(0, youtube.Video("e" * 11, "Best way to make coins today", "Moshi"))
    a._check_youtube()
    assert posted.count("leak") == 1 and posted.count("market") == 1


def test_promo_filter_and_schedule_from_real_releases():
    from datetime import datetime
    assert not market.is_promo("MCS Pro League Game Time Prediction Contest")
    assert market.program_name("Unreal Moments Part 2.5: Jahmyr Gibbs") == "Unreal Moments"
    assert market.program_name("Team of the Week 2: Travis Kelce") == "Team of the Week"
    ts = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()
    promos = [(ts("2026-09-02 10:44"), "Preseason Team of the Week: A"),
              (ts("2026-09-16 15:00"), "Team of the Week 1: B"),
              (ts("2026-09-23 10:52"), "Team of the Week 2: C"),
              (ts("2026-09-30 10:47"), "Team of the Week 3: D"),
              (ts("2026-09-12 11:03"), "Legends: E"), (ts("2026-09-19 10:48"), "Legends: F"),
              (ts("2026-09-17 10:55"), "Game Time Part 2: G")]
    sched = market.promo_schedule(promos)
    assert ("Team of the Week", 2, "10:47 AM") in sched            # Preseason merged, 3:00 PM outlier ignored
    assert ("Legends", 5, "10:55 AM") in sched
    assert all(p != "Game Time" for p, _, _ in sched)              # seen once: not a pattern
    tips = market.timing_tips(datetime(2026, 10, 6, 20), False, schedule=sched)   # a Tuesday
    assert tips[0].startswith("Tomorrow (Wed): Team of the Week ~10:47 AM")
