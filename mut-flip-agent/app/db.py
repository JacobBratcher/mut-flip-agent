"""SQLite storage: items, their sale history, and alert log."""
import sqlite3
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    uid TEXT PRIMARY KEY,
    url TEXT DEFAULT '',
    name TEXT DEFAULT '',
    tier TEXT DEFAULT 'new',          -- watch | hot | cold | new
    next_check REAL DEFAULT 0,
    last_check REAL DEFAULT 0,
    last_sale_seen TEXT DEFAULT '',
    score REAL DEFAULT 0              -- coins traded per day (value x volume)
);
CREATE TABLE IF NOT EXISTS sales (
    uid TEXT, price INTEGER, sold_at TEXT,
    PRIMARY KEY (uid, sold_at, price)
);
CREATE INDEX IF NOT EXISTS sales_uid_time ON sales(uid, sold_at);
CREATE TABLE IF NOT EXISTS alerts (
    uid TEXT, kind TEXT, sent_at REAL
);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
"""


def ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        cols = {r["name"] for r in self.c.execute("PRAGMA table_info(items)")}
        if "score" not in cols:
            self.c.execute("ALTER TABLE items ADD COLUMN score REAL DEFAULT 0")
        self.c.commit()

    # items
    def upsert_item(self, uid, url="", tier=None):
        self.c.execute(
            "INSERT INTO items(uid, url, tier) VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET "
            "url=CASE WHEN excluded.url!='' THEN excluded.url ELSE items.url END",
            (uid, url, tier or "new"))
        if tier:
            self.c.execute("UPDATE items SET tier=?, next_check=0 WHERE uid=?", (tier, uid))
        self.c.commit()

    def item(self, uid):
        return self.c.execute("SELECT * FROM items WHERE uid=?", (uid,)).fetchone()

    def next_due(self):
        order = "CASE tier WHEN 'watch' THEN 0 WHEN 'hot' THEN 1 WHEN 'new' THEN 2 ELSE 3 END"
        return self.c.execute(
            f"SELECT * FROM items WHERE next_check<=? ORDER BY {order}, next_check LIMIT 1",
            (time.time(),)).fetchone()

    def set_schedule(self, uid, tier, next_check):
        self.c.execute("UPDATE items SET tier=?, next_check=?, last_check=? WHERE uid=?",
                       (tier, next_check, time.time(), uid))
        self.c.commit()

    def set_score(self, uid, score):
        self.c.execute("UPDATE items SET score=? WHERE uid=?", (score, uid))
        self.c.commit()

    def count_tier(self, tier):
        return self.c.execute("SELECT COUNT(*) n FROM items WHERE tier=?", (tier,)).fetchone()["n"]

    def demote_hot_beyond(self, keep, next_check):
        rows = self.c.execute("SELECT uid FROM items WHERE tier='hot' ORDER BY score DESC").fetchall()
        extra = [r["uid"] for r in rows[keep:]]
        self.c.executemany("UPDATE items SET tier='cold', next_check=? WHERE uid=?",
                           [(next_check, u) for u in extra])
        self.c.commit()
        return len(extra)

    def set_name(self, uid, name):
        self.c.execute("UPDATE items SET name=? WHERE uid=?", (name, uid))
        self.c.commit()

    def set_last_seen(self, uid, iso):
        self.c.execute("UPDATE items SET last_sale_seen=? WHERE uid=?", (iso, uid))
        self.c.commit()

    def all_items(self):
        return self.c.execute("SELECT * FROM items").fetchall()

    # sales
    def add_sales(self, uid, sales):
        self.c.executemany("INSERT OR IGNORE INTO sales VALUES(?,?,?)",
                           [(uid, p, d) for p, d in sales])
        self.c.commit()

    def sales_since(self, uid, since_ts):
        rows = self.c.execute("SELECT price, sold_at FROM sales WHERE uid=?", (uid,)).fetchall()
        return sorted(((r["price"], ts(r["sold_at"])) for r in rows if ts(r["sold_at"]) >= since_ts),
                      key=lambda x: x[1])

    def prune(self, keep_days):
        cutoff = time.time() - keep_days * 86400
        rows = self.c.execute("SELECT rowid, sold_at FROM sales").fetchall()
        dead = [(r["rowid"],) for r in rows if ts(r["sold_at"]) < cutoff]
        self.c.executemany("DELETE FROM sales WHERE rowid=?", dead)
        self.c.commit()

    # alerts
    def last_alert(self, uid, kind):
        r = self.c.execute("SELECT MAX(sent_at) m FROM alerts WHERE uid=? AND kind=?",
                           (uid, kind)).fetchone()
        return r["m"] or 0

    def log_alert(self, uid, kind):
        self.c.execute("INSERT INTO alerts VALUES(?,?,?)", (uid, kind, time.time()))
        self.c.commit()

    # key/value
    def get(self, k, default=None):
        r = self.c.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def put(self, k, v):
        self.c.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (k, str(v)))
        self.c.commit()
