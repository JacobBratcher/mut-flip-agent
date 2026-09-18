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

    # ------------------------------------------------------------ one item
    def check(self, row):
        uid, now = row["uid"], time.time()
        data = self.api.prices(uid)
        sales = parse_sales(data)
        self.db.add_sales(uid, sales)

        if not row["name"]:
            name = uid
            if row["url"]:
                try:
                    name = self.api.item_name(row["url"])
                except (requests.RequestException, Blocked):
                    pass
            self.db.set_name(uid, name)
            row = self.db.item(uid)

        last_seen = row["last_sale_seen"]
        new = [(p, ts(d)) for p, d in sales if d > last_seen] if last_seen else []
        if sales:
            self.db.set_last_seen(uid, max(d for _, d in sales))

        history = self.db.sales_since(uid, now - self.cfg["flip"]["lookback_hours"] * 3600)
        signal = analysis.flip_signal(new, history, self.cfg, self.tax, now)
        cooldown = self.cfg["flip"]["alert_cooldown_hours"] * 3600
        if signal and now - self.db.last_alert(uid, "flip") > cooldown:
            self.discord.flip(row["name"] or uid, row["url"], signal, self.cfg["platform"])
            self.db.log_alert(uid, "flip")
            log.info("FLIP %s buy<=%s profit=%s", uid, signal.max_buy, signal.profit)

        recent = self.db.sales_since(uid, now - 3 * DAY)
        tier = analysis.tier_for(recent, self.cfg, now, uid in self.watched)
        self.db.set_schedule(uid, tier, now + analysis.interval_for(tier, self.cfg))

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
        self.backoff = min(3600, max(60, self.backoff * 2))
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
