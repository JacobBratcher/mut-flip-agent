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
                      "discover_all_players": False,
                      "flip": DEFAULTS["flip"] | {"sale_alerts": True}}
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
        def listing(self, name, url, d, platform, **kw):
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


def test_snipes_only_by_default(tmp_path, monkeypatch):
    """A cheap sale is already gone, so by default it must not alert; a live listing must."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    agent = main.Agent(DEFAULTS | {"discord_webhook_url": "x", "discover_all_players": False})

    class Disc(FakeDiscord):
        def __init__(self):
            super().__init__(); self.listings = []
        def listing(self, name, url, d, platform, **kw):
            self.listings.append(d)

    agent.discord = Disc()
    agent.db.upsert_item("27-1", "")
    sales = [{"soldPrice": 500_000, "soldDate": iso(h)} for h in range(2, 40, 3)]
    agent.process(agent.db.item("27-1"), {"pricesData": {"completedAuctions": sales}})
    cheap_sale = [{"soldPrice": 300_000, "soldDate": iso(0.01)}] + sales
    agent.process(agent.db.item("27-1"), {"pricesData": {"completedAuctions": cheap_sale}})
    assert agent.discord.flips == []

    end = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    agent.process(agent.db.item("27-1"), {"pricesData": {"completedAuctions": cheap_sale,
        "liveAuctions": [{"buyNowPrice": 350_000, "endDate": end}]}})
    assert len(agent.discord.listings) == 1
    row = agent.db.recent_flips()[0]
    assert row["ends"] and row["buy"] == 350_000


def _stamp(sales, shift_seconds):
    """mut.gg's payload: sold dates recomputed at refresh time, so they drift a little."""
    return [{"soldPrice": p, "soldDate": (datetime.now(timezone.utc)
             - timedelta(hours=h) + timedelta(seconds=shift_seconds)).isoformat()} for p, h in sales]


def test_restamped_sales_are_stored_once(tmp_path, monkeypatch):
    """The same sales re-stamped on every refresh used to be stored again each check
    (an 85 Legend showed ~575 sales/day instead of 13)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    agent = main.Agent(DEFAULTS | {"discord_webhook_url": "x", "discover_all_players": False})
    agent.discord = FakeDiscord()
    agent.db.upsert_item("27-1", "")
    sales = [(180_000 + i * 1000, h) for i, h in enumerate(range(1, 40, 3))]
    for shift in (0, 40, -25, 95, 12):
        agent.process(agent.db.item("27-1"), {"pricesData": {"completedAuctions": _stamp(sales, shift)}})
    assert len(agent.db.sales_since("27-1", 0)) == len(sales)
    # A genuinely new sale is detected as new, and only it.
    new = agent.db.add_sales("27-1", [(p, d["soldDate"]) for p, d in
                                      zip([175_000] + [p for p, _ in sales],
                                          _stamp([(175_000, 0.01)] + sales, 30))])
    assert [p for p, _ in new] == [175_000]
    assert len(agent.db.sales_since("27-1", 0)) == len(sales) + 1


def test_old_history_beyond_mutgg_list_is_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    agent = main.Agent(DEFAULTS | {"discord_webhook_url": "x", "discover_all_players": False})
    old = [(p, d["soldDate"]) for p, d in
           zip([150_000] * 3, _stamp([(150_000, h) for h in (200, 210, 220)], 0))]
    agent.db.add_sales("27-1", old)
    recent = [(p, d["soldDate"]) for p, d in zip([160_000] * 2, _stamp([(160_000, 5), (160_000, 50)], 0))]
    agent.db.add_sales("27-1", recent)
    assert len(agent.db.sales_since("27-1", 0)) == 5


def test_existing_duplicates_are_cleaned_up_once(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    agent = main.Agent(DEFAULTS | {"discord_webhook_url": "x", "discover_all_players": False})
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    rows = [("27-1", 189_600, (base + timedelta(seconds=s)).isoformat()) for s in range(0, 900, 30)]
    rows += [("27-1", 128_800, (base + timedelta(hours=1, seconds=s)).isoformat()) for s in range(0, 300, 20)]
    rows += [("27-1", 189_600, (base + timedelta(hours=2)).isoformat())]     # a separate real sale
    agent.db.c.executemany("INSERT INTO sales VALUES(?,?,?)", rows)
    agent.db.c.execute("DELETE FROM state WHERE k='sales_deduped'")
    agent.db.c.commit()
    agent2 = main.Agent(DEFAULTS | {"discord_webhook_url": "x", "discover_all_players": False})
    assert sorted(p for p, _ in agent2.db.sales_since("27-1", 0)) == [128_800, 189_600, 189_600]
