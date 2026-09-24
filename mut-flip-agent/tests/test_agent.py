"""End-to-end: fake mut.gg + fake Discord, verify a flip alert fires once."""
from datetime import datetime, timedelta, timezone

from app import main
from app.config import DEFAULTS


def iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


class FakeAPI:
    def __init__(self):
        self.auctions = [{"soldPrice": 500_000, "soldDate": iso(h)} for h in range(2, 40, 3)]

    def prices(self, uid, url=""):
        return {"pricesData": {"completedAuctions": list(self.auctions)}}

    def item_name(self, url):
        return "T.J. Watt Team of the Week 87 OVR"

    def discover(self):
        return []


class FakeDiscord:
    def __init__(self):
        self.flips = []

    def flip(self, name, url, f, platform):
        self.flips.append((name, f))

    def status(self, *a, **k):
        pass

    def digest(self, *a, **k):
        pass


def test_flip_alert_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = DEFAULTS | {"discord_webhook_url": "x",
                      "watchlist": ["https://www.mut.gg/players/12562-tj-watt/27-162004004/"],
                      "discover_all_players": False}
    agent = main.Agent(cfg)
    agent.api, agent.discord = FakeAPI(), FakeDiscord()
    agent.sync_items()

    agent.check(agent.db.next_due())              # first pass: builds history, no alert
    assert agent.discord.flips == []

    agent.api.auctions.insert(0, {"soldPrice": 360_000, "soldDate": iso(0.01)})
    agent.check(agent.db.item("27-162004004"))    # cheap sale appears
    assert len(agent.discord.flips) == 1
    name, f = agent.discord.flips[0]
    assert name.startswith("T.J. Watt") and f.buy_seen == 360_000

    agent.api.auctions.insert(0, {"soldPrice": 355_000, "soldDate": iso(0.005)})
    agent.check(agent.db.item("27-162004004"))    # cooldown blocks a repeat
    assert len(agent.discord.flips) == 1


def test_hot_tier_is_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = DEFAULTS | {"discord_webhook_url": "x", "discover_all_players": False}
    agent = main.Agent(cfg)
    agent.api, agent.discord = FakeAPI(), FakeDiscord()
    agent.api.auctions = [{"soldPrice": 500_000, "soldDate": iso(h)} for h in range(1, 60, 2)]
    for i in range(5):
        agent.db.upsert_item(f"27-{i}", "")
    agent.plan = {"hot_cap": 2, "fits": True}
    for i in range(5):
        agent.check(agent.db.item(f"27-{i}"))
    assert agent.db.count_tier("hot") == 2
    assert agent.db.count_tier("cold") == 3


def test_listing_then_sale_alerts_once(tmp_path, monkeypatch):
    """A listing we alert on that later sells must not alert again as a cheap sale."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = DEFAULTS | {"discord_webhook_url": "x", "discover_all_players": False}
    agent = main.Agent(cfg)

    class Disc(FakeDiscord):
        def __init__(self):
            super().__init__(); self.listings = []
        def listing(self, name, url, d, platform):
            self.listings.append(d)

    agent.discord = Disc()
    agent.db.upsert_item("27-1", "")
    sales = [{"soldPrice": 500_000, "soldDate": iso(h)} for h in range(2, 40, 3)]
    end = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    agent.process(agent.db.item("27-1"), {"pricesData": {"completedAuctions": sales}})
    agent.process(agent.db.item("27-1"), {"pricesData": {"completedAuctions": sales,
        "liveAuctions": [{"buyNowPrice": 360_000, "endDate": end}]}})
    assert len(agent.discord.listings) == 1
    # The listing sells: it now appears as a completed sale at 360k.
    sold = [{"soldPrice": 360_000, "soldDate": iso(0.01)}] + sales
    agent.process(agent.db.item("27-1"), {"pricesData": {"completedAuctions": sold}})
    assert agent.discord.flips == [] and len(agent.discord.listings) == 1
