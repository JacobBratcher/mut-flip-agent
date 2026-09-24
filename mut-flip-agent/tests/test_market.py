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
             (cur_price, NOW - 2 * H), (cur_price, NOW - 10 * H)]
    if week_price:
        sales += [(week_price, NOW - 7 * D - 2 * H), (week_price, NOW - 7 * D - 10 * H)]
    return (uid, f"Card {uid}", f"https://www.mut.gg/players/x/{uid}/", sales)


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

    def market_report(self, move, news, drops, snipes, platform):
        self.reports.append((move, news, drops))


def agent(tmp_path, monkeypatch, **market_cfg):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = DEFAULTS | {"discord_webhook_url": "x", "discover_all_players": False,
                      "market": DEFAULTS["market"] | market_cfg}
    a = main.Agent(cfg)
    a.discord = Disc()
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
