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


def market_value(history, now):
    """Median of the lookback window, but never above the last-12h median when
    the market is sliding (so a falling card doesn't look like a bargain)."""
    prices = [p for p, _ in history]
    base = median(prices)
    recent = [p for p, t in history if t >= now - 12 * HOUR]
    if len(recent) >= 3:
        r = median(recent)
        return min(base, r), r < base * 0.9
    return base, False


def flip_signal(new_sales, history, cfg, tax, now) -> Flip | None:
    f = cfg["flip"]
    new_keys = {(p, t) for p, t in new_sales}
    ref_hist = [s for s in history if s not in new_keys]
    if not new_sales or len(ref_hist) < f["min_sales"]:
        return None
    market, falling = market_value(ref_hist, now)
    sell_net = market * (1 - tax)
    buy = min(p for p, _ in new_sales)
    profit = sell_net - buy
    roi = profit / buy
    discount = 1 - buy / market
    max_buy = int(min(sell_net - f["min_profit"], sell_net / (1 + f["min_roi"])))
    if profit < f["min_profit"] or roi < f["min_roi"] or discount < f["min_discount"]:
        return None
    if f.get("max_buy_budget") and buy > f["max_buy_budget"]:
        return None
    return Flip(buy, int(market), max_buy, int(profit), roi, discount, falling)


def live_deal(listings, history, cfg, tax, now) -> Listing | None:
    """Cheapest active Buy Now listing that clears the flip rules.

    listings: [(buy_now_price, end_unix_ts)]
    """
    f = cfg["flip"]
    if len(history) < f["min_sales"]:
        return None
    active = [(p, e) for p, e in listings if p and e > now]
    if not active:
        return None
    market, falling = market_value(history, now)
    sell_net = market * (1 - tax)
    price, ends = min(active)
    profit = sell_net - price
    roi = profit / price
    discount = 1 - price / market
    if profit < f["min_profit"] or roi < f["min_roi"] or discount < f["min_discount"]:
        return None
    if f.get("max_buy_budget") and price > f["max_buy_budget"]:
        return None
    max_buy = int(min(sell_net - f["min_profit"], sell_net / (1 + f["min_roi"])))
    return Listing(int(price), ends, int(market), max_buy, int(profit), roi, discount, falling)


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
