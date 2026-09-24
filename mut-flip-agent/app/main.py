"""Scheduler loop: discover items, poll prices by tier, alert snipes, report on the market."""
import json
import logging
import threading
import time
from datetime import datetime

import requests

from . import analysis, config, market, youtube
from .client import Blocked, MutGG, normalize_watch, parse_live, parse_price, parse_sales, parse_volume
from .db import DB
from .ha import HA
from .notify import Discord

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agent")
DAY = 86400
KEEP_DAYS = 35          # sales history kept: enough to learn promo reactions and weekday patterns


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.api = MutGG(cfg)
        self.db = DB(config.data_dir() / "mut.db")
        self.discord = Discord(cfg["discord_webhook_url"])
        self.tax = cfg["tax_rate"]
        self.backoff = 0
        self.watched = set()
        self.plan = analysis.plan(cfg, 0, 0)
        self.ha = HA()
        self.status = "starting"
        self.picks = []
        self.last_picks = 0
        self.last_publish = 0
        self.last_price_ts = None
        self.checks_day, self.checks_today = datetime.now().date(), 0
        self.last_ingest = 0
        self.feeder_state = ""
        self.lock = threading.RLock()
        self.web = requests.Session()
        self.web.headers["User-Agent"] = "MUT-Flip-Agent (personal market report)"
        self.move = None
        self.drops = None
        self.news = []
        self.videos = []
        self.reactions = []
        self.weekdays = {}
        self.next_weekdays = 0
        self._backfilling = False
        self.next = {"news": 0, "drops": 0, "market": 0, "youtube": 0}
        self.fresh_map = {}              # uid -> when a newly released card first appeared
        self.last_discovery_try = 0

    # ---------------------------------------------------------------- setup
    def sync_items(self):
        for entry in self.cfg["watchlist"]:
            parsed = normalize_watch(entry)
            if not parsed:
                log.warning("Skipping watchlist entry %r (not a mut.gg URL or id)", entry)
                continue
            uid, url = parsed
            self.watched.add(uid)
            self.db.upsert_item(uid, url, tier="watch")
        # Anything no longer on the watchlist drops back to normal scheduling.
        for row in self.db.all_items():
            if row["tier"] == "watch" and row["uid"] not in self.watched:
                self.db.set_schedule(row["uid"], "new", 0)
        min_ovr = int(self.cfg.get("min_ovr") or 0)
        changed = self.db.get("discovery_min_ovr") != str(min_ovr)
        if self.cfg["discover_all_players"] and (changed or self.discovery_due()):
            self.last_discovery_try = time.time()
            found = self.api.discover(min_ovr)
            if found:
                # First run or a new OVR filter: everything found is "already out", not new.
                baseline = changed or not self.db.all_items()
                for uid, url, name in found:
                    self.db.upsert_item(uid, url)
                    if name and not (self.db.item(uid)["name"] or ""):
                        self.db.set_name(uid, name)
                dropped = self.db.keep_only([u for u, _, _ in found] + list(self.watched))
                if dropped:
                    log.info("Stopped tracking %d cards outside the filter", dropped)
                fresh = self.db.mark_seen([u for u, _, _ in found], time.time(), baseline)
                if fresh:
                    names = {u: n or u for u, _, n in found}
                    for uid in fresh:
                        self.db.upsert_item(uid, tier="fresh")      # check them right away
                    self.fresh_map = self.db.first_seen()
                    self.rebalance()
                    self.discord.new_cards([names[u] for u in fresh], self.plan["fresh_seconds"],
                                           self.cfg["tiers"])
                    log.info("NEW %d cards: %s", len(fresh), ", ".join(names[u] for u in fresh[:10]))
                self.db.put("last_discovery", time.time())
                self.db.put("discovery_min_ovr", min_ovr)
        self.rebalance()

    def discovery_due(self):
        """Look for new cards every 6 h, and every 30 min for 3 h after a promo drops."""
        now = time.time()
        if now - self.last_discovery_try < 1800:
            return False
        if now - float(self.db.get("last_discovery", 0)) > 6 * 3600:
            return True
        latest_promo = max((t for t, _ in self.promo_events()), default=0)
        return now - latest_promo < 3 * 3600

    def fresh_uids(self, items=None, now=None):
        now = now or time.time()
        items = items if items is not None else self.db.all_items()
        return [r for r in items if r["uid"] not in self.watched and
                analysis.is_fresh(r["name"], self.fresh_map.get(r["uid"]), now, self.cfg)]

    def rebalance(self):
        """Keep the polling plan inside the request budget."""
        items = self.db.all_items()
        total = len(items)
        self.fresh_map = self.db.first_seen()
        n_fresh = len(self.fresh_uids(items))
        self.plan = analysis.plan(self.cfg, len(self.watched), total, n_fresh)
        demoted = self.db.demote_hot_beyond(self.plan["hot_cap"], time.time())
        p = self.plan
        msg = (f"Budget {p['budget_per_day']:,} req/day | watch {p['watch_load']:,} | "
               f"new releases {n_fresh} every {p['fresh_seconds'] // 60} min ({p['fresh_load']:,}) | "
               f"cold {p['cold_load']:,} | hot cap {p['hot_cap']} cards | {total:,} cards total")
        log.info("Poll plan: %s%s", msg, f" (demoted {demoted})" if demoted else "")
        if not p["fits"] and self.db.get("warned_plan") != msg:
            self.discord.status(
                f"Poll plan is over budget: {msg}. Cold cards will be checked less often than "
                "cold_hours. Shrink the watchlist, raise cold_hours, or raise requests_per_minute "
                "if mut.gg allows it.", 0xE67E22)
            self.db.put("warned_plan", msg)

    # ------------------------------------------------------------ one item
    def check(self, row):
        data = self.api.prices(row["uid"], row["url"])
        self.process(row, data)

    def process(self, row, data):
        """Handle one card's price payload. Alerts go out immediately."""
        uid, now = row["uid"], time.time()
        self.status = "running"
        self.last_ingest = now
        self.last_price_ts = datetime.now().astimezone().isoformat()
        if datetime.now().date() != self.checks_day:
            self.checks_day, self.checks_today = datetime.now().date(), 0
        self.checks_today += 1
        new = self.db.add_sales(uid, parse_sales(data))
        history = self.db.sales_since(uid, now - self.cfg["flip"]["lookback_hours"] * 3600)
        listings = parse_live(data)
        signal = analysis.flip_signal(new, history, self.cfg, self.tax, now, listings)
        cooldown = self.cfg["flip"]["alert_cooldown_hours"] * 3600
        # A listing we already alerted on that then sells shows up as a "cheap sale";
        # don't alert the same card at the same price twice.
        already = signal and now - self.db.last_alert(uid, f"price:{signal.buy_seen}") < cooldown
        # Off by default: a cheap *sale* means someone already bought it, so it can't be sniped.
        if not self.cfg["flip"].get("sale_alerts"):
            signal = None
        if signal and not already and now - self.db.last_alert(uid, "flip") > cooldown:
            row = self.ensure_name(row)
            self.discord.flip(row["name"] or uid, row["url"], signal, self.cfg["platform"])
            self.db.log_alert(uid, "flip")
            self.db.log_flip(uid, row["name"] or uid, row["url"], signal)
            self.last_publish = 0
            log.info("FLIP %s buy<=%s profit=%s", uid, signal.max_buy, signal.profit)

        fresh = analysis.is_fresh(row["name"], self.fresh_map.get(uid), now, self.cfg)
        deal = analysis.live_deal(listings, history, self.cfg, self.tax, now,
                                  sales_24h=parse_volume(data), mutgg_price=parse_price(data))
        if deal:
            key = f"live:{deal.bin_price}:{int(deal.ends)}"
            if not self.db.last_alert(uid, key):
                self.discord.listing(row["name"] or uid, row["url"], deal, self.cfg["platform"],
                                     promo_today=self.promo_today(), fresh=fresh)
                self.db.log_alert(uid, key)
                self.db.log_alert(uid, f"price:{deal.bin_price}")
                self.db.log_flip(uid, row["name"] or uid, row["url"], deal)
                self.last_publish = 0
                log.info("LISTING %s bin=%s profit=%s", uid, deal.bin_price, deal.profit)

        recent = self.db.sales_since(uid, now - 3 * DAY)
        tier = analysis.tier_for(recent, self.cfg, now, uid in self.watched, fresh)
        self.db.set_score(uid, sum(p for p, _ in recent) / 3 if recent else 0)
        self.db.set_schedule(uid, tier, now + analysis.interval_for(tier, self.cfg, self.plan))
        if tier == "hot":
            # Over capacity? The lowest-value hot cards (possibly this one) drop to cold.
            self.db.demote_hot_beyond(self.plan["hot_cap"], now + analysis.interval_for("cold", self.cfg))
            tier = self.db.item(uid)["tier"]
        if tier in ("watch", "fresh", "hot") and self.cfg["fetch_mode"] != "extension":
            self.ensure_name(self.db.item(uid))

    def ensure_name(self, row):
        """Fetch the full card name only for cards we care about (saves ~4k requests)."""
        if row["name"] or not row["url"]:
            return row
        try:
            self.db.set_name(row["uid"], self.api.item_name(row["url"]))
        except (requests.RequestException, Blocked):
            return row
        return self.db.item(row["uid"])

    # --------------------------------------------------------------- digest
    def compute_picks(self):
        iv = self.cfg["invest"]
        now, picks = time.time(), []
        for row in self.db.all_items():
            hist = self.db.sales_since(row["uid"], now - iv["window_days"] * DAY)
            sig = analysis.invest_signal(hist, self.cfg, self.tax, now)
            if sig:
                picks.append((row["name"] or row["uid"], row["url"], sig))
        picks.sort(key=lambda x: x[2].score, reverse=True)
        self.picks = picks[: iv["top_n"]]
        self.last_picks = now
        return self.picks

    def maybe_digest(self):
        iv = self.cfg["invest"]
        if not iv["enabled"]:
            return
        if time.time() - self.last_picks > 3600:
            self.compute_picks()
            self.last_publish = 0
        today = datetime.now().strftime("%Y-%m-%d")
        if datetime.now().hour < iv["digest_hour"] or self.db.get("last_digest") == today:
            return
        picks = self.compute_picks()
        self.discord.digest(picks, self.cfg["platform"])
        self.db.put("last_digest", today)
        log.info("Digest sent with %d picks", len(picks))

    # --------------------------------------------------------------- market
    def maybe_market(self):
        """News every 30 min, Twitch drops every 3 h, market index hourly, report daily."""
        m, now = self.cfg["market"], time.time()
        if m["news"] and now >= self.next["news"]:
            self.next["news"] = now + 1800
            self._check_news()
        if m["twitch_drops"] and now >= self.next["drops"]:
            self.next["drops"] = now + 3 * 3600
            self._check_drops()
        if self.cfg.get("youtube_channels") and now >= self.next["youtube"]:
            self.next["youtube"] = now + 3600
            self._check_youtube()
        if now >= self.next["market"]:
            self.next["market"] = now + 3600
            self._check_market()
        today = datetime.now().strftime("%Y-%m-%d")
        if (m["report"] and datetime.now().hour >= m["report_hour"]
                and self.db.get("last_report") != today and self.move is not None):
            self.db.put("last_report", today)
            day_ago = now - DAY
            news = [a for a in self.news if a.published.timestamp() >= day_ago]
            snipes = self.db.recent_flips(hours=24, limit=1000)
            self.discord.market_report(self.move, news, self.drops, snipes, self.cfg["platform"],
                                       tips=self.tips(), videos=self.videos[:6],
                                       threshold=m["alert_pct"] / 100)
            keep = KEEP_DAYS
            if self.cfg["invest"]["enabled"]:
                keep = max(keep, self.cfg["invest"]["window_days"] + 2)
            self.db.prune(keep)
            log.info("Market report sent")

    def _check_news(self):
        try:
            articles = market.fetch_news(self.web)
        except (requests.RequestException, ValueError) as e:
            log.warning("News check failed: %s", e)
            return
        self.news = articles
        self._record_promos([(a.published.timestamp(), a.title) for a in articles
                             if market.is_promo(a.title)])
        seen_raw = self.db.get("news_seen")
        seen = set(json.loads(seen_raw)) if seen_raw else None
        if seen is not None:          # first run just records what's already out
            for a in reversed(articles):
                if a.url not in seen:
                    self.discord.news(a, market.is_promo(a.title))
                    log.info("NEWS %s", a.title)
        urls = [a.url for a in articles] + [u for u in (seen or []) if u not in {a.url for a in articles}]
        self.db.put("news_seen", json.dumps(urls[:100]))
        self.last_publish = 0

    def _check_drops(self):
        try:
            drops = market.fetch_drops(self.web)
        except requests.RequestException as e:
            log.warning("Twitch drops check failed: %s", e)
            return
        self.drops = drops
        if drops and drops != self.db.get("drops_last"):
            self.discord.drops(drops)
            log.info("DROPS %s", drops)
        self.db.put("drops_last", drops or "")
        self.last_publish = 0

    def promo_events(self):
        return [tuple(x) for x in json.loads(self.db.get("promo_events") or "[]")]

    def _record_promos(self, found):
        if self.db.get("promo_backfilled") is None and not self._backfilling:
            # One-time read of recent article pages (slow on purpose), off the main loop.
            self._backfilling = True
            threading.Thread(target=self._backfill_promos, daemon=True).start()
        events = {round(t): title for t, title in self.promo_events()}
        for t, title in found:
            events.setdefault(round(t), title)
        keep = sorted((t, title) for t, title in events.items() if t >= time.time() - KEEP_DAYS * DAY)
        self.db.put("promo_events", json.dumps(keep))

    def _backfill_promos(self):
        try:
            found = market.backfill_promos(self.web, time.time() - KEEP_DAYS * DAY)
        except (requests.RequestException, ValueError) as e:
            log.warning("Promo backfill failed (will retry): %s", e)
            self._backfilling = False
            return
        with self.lock:
            self.db.put("promo_backfilled", "1")
            self._record_promos(found)
            self._backfilling = False
        log.info("Backfilled %d promo releases", len(found))

    def promo_today(self):
        today = datetime.now().astimezone().date()
        return any(market.is_promo(a.title) and a.published.astimezone().date() == today
                   for a in self.news)

    def tips(self):
        return market.timing_tips(datetime.now().astimezone(), self.promo_today(),
                                  self.reactions, self.weekdays,
                                  market.promo_schedule(self.promo_events()))

    def _check_youtube(self):
        seen_raw = self.db.get("yt_seen")
        seen = set(json.loads(seen_raw)) if seen_raw else set()
        known = set(json.loads(self.db.get("yt_channels_seen") or "[]"))
        found = []
        for ident in self.cfg["youtube_channels"]:
            try:
                cid = self.db.get(f"yt_id:{ident}") or youtube.resolve(self.web, ident)
                if not cid:
                    log.warning("YouTube channel %s not found", ident)
                    continue
                self.db.put(f"yt_id:{ident}", cid)
                vids = youtube.latest(self.web, cid)[:10]
            except requests.RequestException as e:
                log.warning("YouTube check for %s failed: %s", ident, e)
                continue
            if cid in known:                         # first look at a channel is silent
                for v in reversed(vids):
                    kind = youtube.classify(v.title)
                    if v.id in seen or not kind:         # others wait for the daily report
                        continue
                    # market videos always post; leak/update videos at most once per 8h per
                    # channel, so a creator who uploads 4x a day doesn't flood Discord
                    if kind == "leak" and time.time() - self.db.last_alert("yt", cid) < 8 * 3600:
                        continue
                    self.discord.video(v, kind)
                    if kind == "leak":
                        self.db.log_alert("yt", cid)
                    log.info("VIDEO %s: %s", v.channel, v.title)
            known.add(cid)
            seen.update(v.id for v in vids)
            found.extend(vids[:3])
        if found:
            # flagged (market / leak) first, then the rest, newest first within each
            self.videos = sorted(found, key=lambda v: youtube.classify(v.title) is None)
        self.db.put("yt_seen", json.dumps(sorted(seen)[-500:]))
        self.db.put("yt_channels_seen", json.dumps(sorted(known)))
        self.last_publish = 0

    def _check_market(self):
        now = time.time()
        cards = [(r["uid"], r["name"], r["url"], self.db.sales_since(r["uid"], now - KEEP_DAYS * DAY))
                 for r in self.db.all_items()]
        self.move = market.market_move(cards, now)
        self.reactions = market.promo_reactions(cards, self.promo_events(), now)
        if now >= self.next_weekdays:
            self.next_weekdays = now + 6 * 3600
            self.weekdays = market.weekday_pattern(cards, now)
        kind = market.classify(self.move.change_24h, self.cfg["market"]["alert_pct"] / 100)
        if kind and now - self.db.last_alert("market", kind) > 12 * 3600:
            self.discord.market_alert(kind, self.move, self.cfg["platform"])
            self.db.log_alert("market", kind)
            log.info("MARKET %s %.1f%%", kind, self.move.change_24h * 100)
        limit = self.cfg["market"]["program_alert_pct"] / 100
        for p in self.move.programs:
            pk = market.classify(p.change, limit)
            key = f"program:{p.program}:{pk}"
            if pk and now - self.db.last_alert("market", key) > 12 * 3600:
                self.discord.program_alert(pk, p, self.move, self.cfg["platform"])
                self.db.log_alert("market", key)
                log.info("PROGRAM %s %s %.1f%%", p.program, pk, p.change * 100)
        self.last_publish = 0

    def publish(self):
        if time.time() - self.last_publish < 60:
            return
        self.last_publish = time.time()
        flips = [{"name": r["name"], "url": r["url"], "buy": r["buy"], "max_buy": r["max_buy"],
                  "market": r["market"], "profit": r["profit"], "roi": round(r["roi"], 3),
                  "falling": bool(r["falling"]),
                  "when": datetime.fromtimestamp(r["ts"]).astimezone().isoformat(),
                  "ends": (datetime.fromtimestamp(r["ends"]).astimezone().isoformat()
                           if r["ends"] else None),
                  "grade": r["grade"] or "good", "sales_24h": r["sales_24h"],
                  "trend": _pct(r["trend"]), "typical": r["typical"],
                  "typical_profit": r["typical_profit"]}
                 for r in self.db.recent_flips()]
        picks = [{"name": n, "url": u, "buy": p.current, "target": p.target, "high": p.high,
                  "profit": p.profit, "roi": round(p.roi, 3), "drawdown": round(p.drawdown, 3),
                  "daily_sales": round(p.daily_sales, 1), "record_low": p.record_low}
                 for n, u, p in self.picks]
        now = time.time()
        fresh = [{"name": r["name"] or r["uid"], "url": r["url"],
                  "since": datetime.fromtimestamp(self.fresh_map[r["uid"]]).astimezone().isoformat()}
                 for r in self.fresh_uids(now=now)]
        fresh.sort(key=lambda x: x["since"], reverse=True)
        status = self.status
        if self.cfg["fetch_mode"] == "extension":
            if self.feeder_state == "blocked":
                status = "blocked"
            elif time.time() - self.last_ingest > 300:
                status = "waiting for feeder"
            else:
                status = "running"
        state = {
            "status": status,
            "cards_tracked": len(self.db.all_items()),
            "hot_cards": self.db.count_tier("hot"),
            "new_cards": len(fresh),
            "checks_today": self.checks_today,
            "flips_24h": len(self.db.recent_flips(limit=1000)),
            "invest_picks": len(picks),
            "last_price_update": self.last_price_ts,
            "market_24h": _pct(self.move.change_24h if self.move else None),
            "market_7d": _pct(self.move.change_7d if self.move else None),
            "twitch_drop": "live" if self.drops else "none",
            "promos_24h": len([a for a in self.news if time.time() - a.published.timestamp() < DAY]),
            "latest_video": (self.videos[0].title[:250] if self.videos else None),
        }
        extra = {
            "market": {
                "fallers": [_mover(x) for x in (self.move.fallers if self.move else [])],
                "risers": [_mover(x) for x in (self.move.risers if self.move else [])],
                "cards": self.move.cards_24h if self.move else 0,
                "breadth_down": _pct(self.move.breadth_down if self.move else None),
                "programs": [{"program": p.program, "change": round(p.change * 100, 1),
                              "cards": p.cards} for p in (self.move.programs if self.move else [])],
                "tips": self.tips(),
                "promo_reactions": [{"title": r.title, "dip": round(r.dip * 100, 1),
                                     "next_day": round(r.next_day * 100, 1), "cards": r.cards,
                                     "when": datetime.fromtimestamp(r.ts).astimezone().isoformat()}
                                    for r in self.reactions[-5:]],
                "weekdays": {market.DAYS[k]: round(v * 100, 1) for k, v in sorted(self.weekdays.items())},
                "schedule": [{"program": p, "day": market.DAYS[wd], "time": t}
                             for p, wd, t in market.promo_schedule(self.promo_events())],
            },
            "youtube": {"videos": [{"title": v.title, "url": v.url, "channel": v.channel,
                                    "kind": youtube.classify(v.title),
                                    "when": v.published.isoformat() if v.published else None,
                                    "age": v.age} for v in self.videos]},
            "drops": {"text": self.drops or "", "page": market.DROPS_PAGE},
            "fresh": {"cards": fresh[:40], "every_minutes": self.plan.get("fresh_seconds", 180) // 60},
            "news": {"articles": [{"title": a.title, "url": a.url, "promo": market.is_promo(a.title),
                                   "when": a.published.isoformat()} for a in self.news[:10]]},
        }
        self.ha.publish(state, flips, picks, extra)

    # ----------------------------------------------------------------- loop
    def run(self):
        self.discord.status(f"Started. Watching {self.cfg['platform'].upper()} market "
                            f"({self.cfg['fetch_mode']} mode).", 0x95A5A6)
        if self.cfg["fetch_mode"] == "extension":
            from .feeder import serve
            serve(self, int(self.cfg.get("feeder_port", 8099)))
        last_sync = 0
        while True:
            try:
                with self.lock:
                    if time.time() - last_sync > 3600 or self.discovery_due():
                        self.sync_items()
                        last_sync = time.time()
                    self.maybe_digest()
                    self.maybe_market()
                    self.publish()
                if self.cfg["fetch_mode"] == "extension":
                    self.upgrade_names()
                    time.sleep(5)
                    continue
                with self.lock:
                    row = self.db.next_due()
                if not row:
                    time.sleep(15)
                    continue
                with self.lock:
                    self.check(row)
                self.backoff = 0
            except Blocked as e:
                self.handle_block(e)
            except requests.RequestException as e:
                log.warning("Network error: %s", e)
                time.sleep(30)
            except Exception:
                log.exception("Unexpected error")
                time.sleep(30)

    def upgrade_names(self):
        """In extension mode, fill full card names (program + OVR) for hot/watch cards, slowly."""
        with self.lock:
            row = self.db.c.execute(
                "SELECT * FROM items WHERE tier IN ('watch','fresh','hot') AND name='' AND url!='' LIMIT 1").fetchone()
        if not row:
            return
        try:
            name = self.api.item_name(row["url"])
        except (requests.RequestException, Blocked):
            return
        with self.lock:
            self.db.set_name(row["uid"], name)

    def handle_block(self, e):
        self.status = "blocked"
        self.last_publish = 0
        self.publish()
        self.backoff = min(3600, max(60, self.backoff * 2, e.retry_after))
        log.warning("Blocked by mut.gg (%s); pausing %ss", e, self.backoff)
        if time.time() - float(self.db.get("last_block_alert", 0)) > 6 * 3600:
            self.discord.status(
                "mut.gg is blocking requests (Cloudflare challenge or rate limit). "
                f"Pausing and retrying. Error: `{e}`. If this keeps happening, check that "
                "your access token is set and that mut.gg has allowed your server.")
            self.db.put("last_block_alert", time.time())
        time.sleep(self.backoff)


def _pct(x):
    return round(x * 100, 1) if x is not None else None


def _mover(m):
    return {"name": m.name, "url": m.url, "price": m.now_price, "change": round(m.change * 100, 1)}


def run():
    Agent(config.load()).run()


def inspect(uid):
    """Debug helper: print the raw price payload structure for one item."""
    cfg = config.DEFAULTS | {"discord_webhook_url": "x"}
    try:
        cfg = config.load()
    except SystemExit:
        pass
    data = MutGG(cfg).prices(uid)
    print(json.dumps(data, indent=2)[:6000])
