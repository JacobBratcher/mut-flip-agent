"""Pure signal logic (no I/O) so it can be unit tested.

Sales are [(price, unix_ts)].
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median

HOUR = 3600
DAY = 86400


@dataclass
class Flip:
    buy_seen: int        # the cheap sale that triggered this
    market: int          # conservative market value
    max_buy: int         # highest price that still meets your profit rules
    profit: int          # expected profit at buy_seen after tax
    roi: float
    discount: float
    falling: bool
    basis: str = "sales"   # what the resale price is based on: "listings" or "sales"


@dataclass
class Listing:
    bin_price: int
    ends: float          # unix time the listing ends
    market: int
    max_buy: int
    profit: int
    roi: float
    discount: float
    falling: bool
    basis: str = "sales"
    grade: str = "good"             # "safe" or "good"
    sales_24h: int = 0              # how fast it resells
    trend: float | None = None      # latest sales vs. this time yesterday


@dataclass
class Invest:
    current: int
    high: int
    drawdown: float
    target: int
    profit: int
    roi: float
    daily_sales: float
    record_low: bool
    score: float


RECENT_SALES = 3        # how many of the latest sales define "what buyers are paying"
UNDERCUT = 0.99         # list just under the cheapest competitor to sell
MAX_ABOVE_SALES = 0.10  # never price more than 10% above the latest sales


def resale_value(history, listings, now, exclude_price=None):
    """What a card can realistically be resold for right now.

    PC has little volume, so a multi-day average lags. Instead:
    - the cheapest competing Buy Now is the price you'd have to undercut to sell,
      so the estimate follows listings down immediately;
    - the last few sales are what buyers actually paid, which caps how far above
      them a listing is trusted (one optimistic seller can't inflate a flip).

    history: [(price, ts)]; listings: [(buy_now_price, end_ts)].
    exclude_price: the listing you'd be buying (so it isn't its own competitor).
    Returns (value, basis, falling), or (None, None, False) without enough data.
    """
    recent = [p for p, _ in sorted(history, key=lambda s: s[1])[-RECENT_SALES:]]
    last = median(recent) if recent else None
    comps = sorted(p for p, e in listings if p and e > now)
    if exclude_price is not None and exclude_price in comps:
        comps.remove(exclude_price)
    ask = comps[0] * UNDERCUT if comps else None
    if last is None:
        return None, None, False
    if ask is None:
        return last, "sales", False
    cap = last * (1 + MAX_ABOVE_SALES)
    if ask <= cap:
        return ask, "listings", ask < last * 0.9
    return cap, "sales", False


def _max_buy(sell_net, f):
    return int(min(sell_net - f["min_profit"], sell_net / (1 + f["min_roi"])))


def _passes(profit, roi, discount, price, f):
    if profit < f["min_profit"] or roi < f["min_roi"] or discount < f["min_discount"]:
        return False
    return not (f.get("max_buy_budget") and price > f["max_buy_budget"])


def flip_signal(new_sales, history, cfg, tax, now, listings=()) -> Flip | None:
    """A sale just went through well under resale value."""
    f = cfg["flip"]
    new_keys = {(p, t) for p, t in new_sales}
    ref_hist = [s for s in history if s not in new_keys]
    if not new_sales or len(ref_hist) < f["min_sales"]:
        return None
    market, basis, falling = resale_value(ref_hist, listings, now)
    if not market:
        return None
    sell_net = market * (1 - tax)
    buy = min(p for p, _ in new_sales)
    profit, roi, discount = sell_net - buy, (sell_net - buy) / buy, 1 - buy / market
    if not _passes(profit, roi, discount, buy, f):
        return None
    return Flip(buy, int(market), _max_buy(sell_net, f), int(profit), roi, discount, falling, basis)


def card_trend(history, now):
    """Latest sales vs. the same card 24-48h ago (e.g. -0.2 = down 20%).
    None when either side has too few sales to say."""
    latest = [p for p, t in sorted(history, key=lambda s: s[1]) if t >= now - DAY][-RECENT_SALES:]
    before = [p for p, t in history if now - 2 * DAY <= t < now - DAY]
    if len(latest) < 2 or len(before) < 2:
        return None
    return median(latest) / median(before) - 1


def live_deal(listings, history, cfg, tax, now) -> Listing | None:
    """Cheapest active Buy Now listing that clears the flip rules, graded for safety.

    Skips cards that don't resell fast enough (min_sales_24h) and falling knives
    (down max_drop or more since yesterday: the "resale" price is still sliding).
    Safe = liquid, flat or rising, and a fat margin.
    """
    f = cfg["flip"]
    if len(history) < f["min_sales"]:
        return None
    active = [(p, e) for p, e in listings if p and e > now]
    if not active:
        return None
    price, ends = min(active)
    market, basis, falling = resale_value(history, listings, now, exclude_price=price)
    if not market:
        return None
    sell_net = market * (1 - tax)
    profit, roi, discount = sell_net - price, (sell_net - price) / price, 1 - price / market
    if not _passes(profit, roi, discount, price, f):
        return None
    sales_24h = sum(1 for _, t in history if t >= now - DAY)
    if sales_24h < f.get("min_sales_24h", 0):
        return None
    trend = card_trend(history, now)
    if trend is not None and trend <= -f.get("max_drop", 1):
        return None
    safe = (sales_24h >= f.get("safe_sales_24h", 8) and trend is not None and trend > -0.05
            and roi >= f.get("safe_roi", 0.15) and not falling)
    if f.get("only_safe") and not safe:
        return None
    return Listing(int(price), ends, int(market), _max_buy(sell_net, f), int(profit), roi,
                   discount, falling, basis, "safe" if safe else "good", sales_24h, trend)


def daily_medians(history):
    days = defaultdict(list)
    for p, t in history:
        days[datetime.fromtimestamp(t, timezone.utc).date()].append(p)
    return [median(days[d]) for d in sorted(days)]


def invest_signal(history, cfg, tax, now) -> Invest | None:
    iv = cfg["invest"]
    window = [s for s in history if s[1] >= now - iv["window_days"] * DAY]
    daily = daily_medians(window)
    if len(daily) < iv["min_days_history"]:
        return None
    span_days = max(1.0, (now - min(t for _, t in window)) / DAY)
    daily_sales = len(window) / span_days
    if daily_sales < iv["min_daily_sales"]:
        return None
    last24 = [p for p, t in window if t >= now - DAY]
    current = median(last24) if len(last24) >= 3 else daily[-1]
    high = max(daily)
    drawdown = 1 - current / high
    if drawdown < iv["min_drawdown"]:
        return None
    # Still actively dropping? Skip until it levels off.
    if len(daily) >= 2 and daily[-1] < daily[-2] * 0.97:
        return None
    target = current + iv["recovery_target"] * (high - current)
    profit = target * (1 - tax) - current
    if profit <= 0:
        return None
    roi = profit / current
    liquidity = min(1.0, daily_sales / (2 * iv["min_daily_sales"]))
    return Invest(int(current), int(high), drawdown, int(target), int(profit), roi,
                  daily_sales, current <= min(daily) * 1.02, roi * liquidity)


def tier_for(history, cfg, now, watched):
    if watched:
        return "watch"
    t = cfg["tiers"]
    recent = [p for p, ts in history if ts >= now - 3 * DAY]
    if not recent:
        return "cold"
    per_day = len(recent) / 3
    if median(recent) >= t["hot_min_value"] and per_day >= t["hot_min_daily_sales"]:
        return "hot"
    return "cold"


def interval_for(tier, cfg):
    t = cfg["tiers"]
    return {"watch": t["watch_minutes"] * 60, "hot": t["hot_minutes"] * 60}.get(
        tier, t["cold_hours"] * 3600)


def plan(cfg, n_watch, n_total):
    """Request budget per day and how many cards can be 'hot' without starving the rest.

    Keeps 15% headroom for retries, discovery and name lookups.
    """
    t = cfg["tiers"]
    budget = cfg["requests_per_minute"] * 1440 * 0.85
    watch_load = n_watch * 1440 / t["watch_minutes"]
    cold_each = 24 / t["cold_hours"]
    cold_load = max(0, n_total - n_watch) * cold_each
    hot_extra_each = 1440 / t["hot_minutes"] - cold_each
    spare = budget - watch_load - cold_load
    hot_cap = max(0, int(spare // hot_extra_each)) if hot_extra_each > 0 else 0
    return {
        "budget_per_day": int(budget),
        "watch_load": int(watch_load),
        "cold_load": int(cold_load),
        "hot_cap": hot_cap,
        "fits": spare >= 0,
    }
