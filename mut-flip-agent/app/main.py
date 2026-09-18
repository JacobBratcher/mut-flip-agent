"""Scheduler loop: discover items, poll prices by tier, alert flips, send daily digest."""
import json
import logging
import time
from datetime import datetime

import requests

from . import analysis, config
from .client import Blocked, MutGG, normalize_watch, parse_sales
from .db import DB, ts
from .notify import Discord

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agent")
DAY = 86400


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
        last = float(self.db.get("last_discovery", 0))
        if self.cfg["discover_all_players"] and time.time() - last > DAY:
            for uid, url in self.api.discover():
                self.db.upsert_item(uid, url)
            self.db.put("last_discovery", time.time())
        self.rebalance()

    def rebalance(self):
        """Keep the polling plan inside the request budget."""
        total = len(self.db.all_items())
        self.plan = analysis.plan(self.cfg, len(self.watched), total)
        demoted = self.db.demote_hot_beyond(self.plan["hot_cap"], time.time())
        p = self.plan
        msg = (f"Budget {p['budget_per_day']:,} req/day | watch {p['watch_load']:,} | "
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
        uid, now = row["uid"], time.time()
        data = self.api.prices(uid)
        sales = parse_sales(data)
        self.db.add_sales(uid, sales)

        last_seen = row["last_sale_seen"]
        new = [(p, ts(d)) for p, d in sales if d > last_seen] if last_seen else []
        if sales:
            self.db.set_last_seen(uid, max(d for _, d in sales))

        history = self.db.sales_since(uid, now - self.cfg["flip"]["lookback_hours"] * 3600)
        signal = analysis.flip_signal(new, history, self.cfg, self.tax, now)
        cooldown = self.cfg["flip"]["alert_cooldown_hours"] * 3600
        if signal and now - self.db.last_alert(uid, "flip") > cooldown:
            row = self.ensure_name(row)
            self.discord.flip(row["name"] or uid, row["url"], signal, self.cfg["platform"])
            self.db.log_alert(uid, "flip")
            log.info("FLIP %s buy<=%s profit=%s", uid, signal.max_buy, signal.profit)

        recent = self.db.sales_since(uid, now - 3 * DAY)
        tier = analysis.tier_for(recent, self.cfg, now, uid in self.watched)
        self.db.set_score(uid, sum(p for p, _ in recent) / 3 if recent else 0)
        self.db.set_schedule(uid, tier, now + analysis.interval_for(tier, self.cfg))
        if tier == "hot":
            # Over capacity? The lowest-value hot cards (possibly this one) drop to cold.
            self.db.demote_hot_beyond(self.plan["hot_cap"], now + analysis.interval_for("cold", self.cfg))
            tier = self.db.item(uid)["tier"]
        if tier in ("watch", "hot"):
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
    def maybe_digest(self):
        iv = self.cfg["invest"]
        if not iv["enabled"]:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if datetime.now().hour < iv["digest_hour"] or self.db.get("last_digest") == today:
            return
        now, picks = time.time(), []
        for row in self.db.all_items():
            hist = self.db.sales_since(row["uid"], now - iv["window_days"] * DAY)
            sig = analysis.invest_signal(hist, self.cfg, self.tax, now)
            if sig:
                picks.append((row["name"] or row["uid"], row["url"], sig))
        picks.sort(key=lambda x: x[2].score, reverse=True)
        self.discord.digest(picks[: iv["top_n"]], self.cfg["platform"])
        self.db.put("last_digest", today)
        self.db.prune(max(iv["window_days"], 7) + 2)
        log.info("Digest sent with %d picks", min(len(picks), iv["top_n"]))

    # ----------------------------------------------------------------- loop
    def run(self):
        self.discord.status(f"Started. Watching {self.cfg['platform'].upper()} market.", 0x95A5A6)
        last_sync = 0
        while True:
            try:
                if time.time() - last_sync > 3600:
                    self.sync_items()
                    last_sync = time.time()
                self.maybe_digest()
                row = self.db.next_due()
                if not row:
                    time.sleep(15)
                    continue
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

    def handle_block(self, e):
        self.backoff = min(3600, max(60, self.backoff * 2, e.retry_after))
        log.warning("Blocked by mut.gg (%s); pausing %ss", e, self.backoff)
        if time.time() - float(self.db.get("last_block_alert", 0)) > 6 * 3600:
            self.discord.status(
                "mut.gg is blocking requests (Cloudflare challenge or rate limit). "
                f"Pausing and retrying. Error: `{e}`. If this keeps happening, check that "
                "your access token is set and that mut.gg has allowed your server.")
            self.db.put("last_block_alert", time.time())
        time.sleep(self.backoff)


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
