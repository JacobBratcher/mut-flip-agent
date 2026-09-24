import time

from app import analysis
from app.config import DEFAULTS

NOW = time.time()
H, D = 3600, 86400
TAX = 0.10


def hist(prices_hours_ago):
    return [(p, NOW - h * H) for p, h in prices_hours_ago]


def test_flip_triggers_on_cheap_sale():
    history = hist([(500_000, h) for h in range(1, 30, 3)])
    new = [(380_000, NOW - 60)]
    f = analysis.flip_signal(new, history + new, DEFAULTS, TAX, NOW)
    assert f is not None
    assert f.market == 500_000
    assert f.profit == 500_000 * 0.9 - 380_000
    assert f.max_buy < 450_000


def test_no_flip_when_margin_eaten_by_tax():
    history = hist([(100_000, h) for h in range(1, 30, 3)])
    new = [(92_000, NOW - 60)]
    assert analysis.flip_signal(new, history + new, DEFAULTS, TAX, NOW) is None


def test_no_flip_without_enough_history():
    history = hist([(500_000, 2), (500_000, 4)])
    new = [(300_000, NOW - 60)]
    assert analysis.flip_signal(new, history + new, DEFAULTS, TAX, NOW) is None


def test_resale_tracks_last_sales_not_the_average():
    # Sold at 600k for two days, then the last few sales dropped to 450k.
    old = [(600_000, h) for h in range(20, 46, 2)]
    recent = [(450_000, h) for h in (1, 3, 5)]
    history = hist(old + recent)
    new = [(330_000, NOW - 60)]
    f = analysis.flip_signal(new, history + new, DEFAULTS, TAX, NOW)
    assert f is not None and f.market == 450_000 and f.basis == "sales"


def test_invest_finds_leveled_off_dip():
    sales = []
    for day in range(10, 0, -1):
        price = 200_000 if day > 5 else 120_000
        sales += [(price, NOW - day * D + i * H) for i in range(6)]
    sales += [(121_000, NOW - i * H) for i in range(1, 6)]
    iv = analysis.invest_signal(sorted(sales, key=lambda s: s[1]), DEFAULTS, TAX, NOW)
    assert iv is not None
    assert iv.high == 200_000 and iv.drawdown > 0.35 and iv.profit > 0


def test_invest_skips_still_falling():
    sales = []
    for day in range(10, -1, -1):
        price = 200_000 - (10 - day) * 12_000
        sales += [(price, NOW - day * D + i * H) for i in range(5)]
    sales = [s for s in sales if s[1] <= NOW]
    assert analysis.invest_signal(sales, DEFAULTS, TAX, NOW) is None


def test_tiers():
    hot = hist([(60_000, h) for h in range(1, 72, 4)])
    assert analysis.tier_for(hot, DEFAULTS, NOW, False) == "hot"
    assert analysis.tier_for(hist([(900, 5)]), DEFAULTS, NOW, False) == "cold"
    assert analysis.tier_for([], DEFAULTS, NOW, True) == "watch"


def test_plan_default_fits_full_market():
    p = analysis.plan(DEFAULTS, n_watch=10, n_total=420)
    assert p["fits"]
    assert p["budget_per_day"] == int(20 * 1440 * 0.85)
    assert 100 <= p["hot_cap"] <= 200


def test_plan_over_budget_flags():
    cfg = DEFAULTS | {"requests_per_minute": 1}
    assert not analysis.plan(cfg, n_watch=10, n_total=4000)["fits"]


def test_live_deal_finds_cheap_listing():
    history = hist([(500_000, h) for h in range(1, 30, 3)])
    listings = [(480_000, NOW + 600), (370_000, NOW + 300), (300_000, NOW - 10)]  # last one expired
    d = analysis.live_deal(listings, history, DEFAULTS, TAX, NOW)
    # You buy the 370k listing; to resell you must undercut the next one at 480k.
    assert d is not None and d.bin_price == 370_000
    assert d.market == int(480_000 * 0.99) and d.basis == "listings"
    assert d.profit == int(480_000 * 0.99 * 0.9 - 370_000)


def test_live_deal_none_when_listings_at_market():
    history = hist([(500_000, h) for h in range(1, 30, 3)])
    assert analysis.live_deal([(495_000, NOW + 600)], history, DEFAULTS, TAX, NOW) is None


def test_listings_pull_resale_down_immediately():
    # Sales say 500k, but sellers have already dropped to 420k: resale follows them.
    history = hist([(500_000, h) for h in (2, 5, 9)])
    v, basis, falling = analysis.resale_value(history, [(420_000, NOW + 900)], NOW)
    assert v == 420_000 * 0.99 and basis == "listings" and falling


def test_lone_optimistic_listing_is_capped_by_sales():
    # Only competitor asks 800k while buyers paid 500k: trust at most +10% over sales.
    history = hist([(500_000, h) for h in (2, 5, 9)])
    v, basis, _ = analysis.resale_value(history, [(800_000, NOW + 900)], NOW)
    assert v == 500_000 * 1.10 and basis == "sales"


def test_low_volume_card_with_three_sales_qualifies():
    # Three sales spread over five days: enough on PC now (was 8 in 48h).
    history = hist([(200_000, 20), (205_000, 60), (198_000, 110)])
    listings = [(150_000, NOW + 600), (210_000, NOW + 900)]
    d = analysis.live_deal(listings, history, DEFAULTS, TAX, NOW)
    assert d is not None and d.bin_price == 150_000


def test_own_listing_is_not_its_own_competitor():
    history = hist([(500_000, h) for h in (2, 5, 9)])
    v, basis, _ = analysis.resale_value(history, [(370_000, NOW + 600)], NOW, exclude_price=370_000)
    assert v == 500_000 and basis == "sales"
