"""Durable scan coverage and estimated missed opportunities, never buy alerts."""
import json
from statistics import median

from . import analysis
from .db import SAME_SALE_SECONDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_attempts (
    uid TEXT PRIMARY KEY, attempted_at REAL, worker_id TEXT, outcome TEXT,
    requests INTEGER, refreshing INTEGER
);
CREATE TABLE IF NOT EXISTS scan_observations (
    uid TEXT, scanned_at REAL, PRIMARY KEY(uid, scanned_at)
);
CREATE INDEX IF NOT EXISTS scans_time ON scan_observations(scanned_at);
CREATE TABLE IF NOT EXISTS scan_baselines (
    uid TEXT PRIMARY KEY, started_at REAL, scanned_at REAL, snapshot TEXT
);
CREATE TABLE IF NOT EXISTS missed_opportunities (
    id INTEGER PRIMARY KEY, uid TEXT, name TEXT, url TEXT, price INTEGER,
    sold_at REAL, detected_at REAL, previous_scan_at REAL, scan_before_sale REAL,
    market INTEGER, estimated_profit INTEGER, reason TEXT,
    UNIQUE(uid, price, sold_at)
);
CREATE INDEX IF NOT EXISTS missed_time ON missed_opportunities(detected_at);
CREATE INDEX IF NOT EXISTS missed_identity ON missed_opportunities(uid, price, sold_at);
"""


class ScanTracking:
    def __init__(self, db):
        self.db = db
        db.c.executescript(SCHEMA)
        self.next_prune = 0

    def record(self, row, new_sales, history, listings, volume, typical, cfg, now):
        c, uid = self.db.c, row['uid']
        previous = c.execute('SELECT * FROM scan_baselines WHERE uid=?', (uid,)).fetchone()
        # A first observation establishes coverage; never label imported history missed.
        if previous:
            prior = json.loads(previous['snapshot'])
            for price, sold_at in new_sales:
                if not previous['started_at'] < sold_at <= now or price <= 0:
                    continue
                if c.execute('SELECT 1 FROM missed_opportunities WHERE uid=? AND price=? '
                             'AND ABS(sold_at-?)<=?',
                             (uid, price, sold_at, SAME_SALE_SECONDS)).fetchone():
                    continue
                # Use only sales preceding the sale, excluding that sale itself.
                # For between-scan sales use information known at the previous scan.
                between = sold_at > previous['scanned_at']
                source = prior['history'] if between else history
                ref = [(p, t) for p, t in source if sold_at - cfg['flip']['lookback_hours'] * 3600 <= t < sold_at]
                comps = [(p, e) for p, e in prior['listings'] if e > sold_at] if between else []
                # Put the sold price in as a hypothetical listing; reuse liquidity,
                # tax, ROI, discount, budget, trend and safe-only rules.
                comps = [(p, e) for p, e in comps if p != price]
                deal = analysis.live_deal([(price, sold_at + 3600)] + comps, ref, cfg,
                                         cfg['tax_rate'], sold_at,
                                         sales_24h=prior['volume'] if between else None,
                                         mutgg_price=prior['typical'] if between else None)
                if not deal or deal.bin_price != price:
                    continue
                alerted = c.execute("SELECT 1 FROM alerts WHERE uid=? AND kind=? "
                                    "AND sent_at>=? AND sent_at<=? LIMIT 1",
                                    (uid, f'price:{price}', sold_at - max(180, cfg['flip']['alert_cooldown_hours'] * 3600), sold_at)).fetchone()
                seen = between and any(p == price and e > sold_at for p, e in prior['listings'])
                reason = ('price_match_alerted' if alerted else
                          'sale_reported_late' if not between else
                          'seen_before_sale_unconfirmed' if seen else 'likely_missed_between_scans')
                before = c.execute('SELECT MAX(scanned_at) FROM scan_observations '
                                   'WHERE uid=? AND scanned_at<=?', (uid, sold_at)).fetchone()[0]
                c.execute('INSERT OR IGNORE INTO missed_opportunities '
                          '(uid,name,url,price,sold_at,detected_at,previous_scan_at,scan_before_sale,'
                          'market,estimated_profit,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                          (uid, row['name'] or uid, row['url'], price, sold_at, now,
                           previous['scanned_at'], before, deal.market, deal.profit, reason))
        snapshot = json.dumps({'history': history, 'listings': listings,
                               'volume': volume, 'typical': typical})
        c.execute('INSERT INTO scan_baselines VALUES(?,?,?,?) ON CONFLICT(uid) DO UPDATE SET '
                  'scanned_at=excluded.scanned_at,snapshot=excluded.snapshot', (uid, now, now, snapshot))
        c.execute('INSERT OR IGNORE INTO scan_observations VALUES(?,?)', (uid, now))
        c.execute('UPDATE items SET last_success=? WHERE uid=?', (now, uid))
        if now >= self.next_prune:
            c.execute('DELETE FROM scan_observations WHERE scanned_at<?', (now - 7 * 86400,))
            c.execute('DELETE FROM missed_opportunities WHERE detected_at<?', (now - 35 * 86400,))
            c.execute('DELETE FROM scan_baselines WHERE uid NOT IN (SELECT uid FROM items)')
            c.execute('DELETE FROM scan_attempts WHERE uid NOT IN (SELECT uid FROM items)')
            self.next_prune = now + 3600
        c.commit()

    def attempt(self, uid, worker_id, outcome, now):
        self.db.c.execute(
            'INSERT INTO scan_attempts VALUES(?,?,?,?,1,?) ON CONFLICT(uid) DO UPDATE SET '
            'attempted_at=excluded.attempted_at,worker_id=excluded.worker_id,outcome=excluded.outcome,'
            'requests=scan_attempts.requests+1,refreshing=scan_attempts.refreshing+excluded.refreshing',
            (uid, now, worker_id, outcome, int(outcome == 'refreshing')))
        self.db.c.commit()

    def refresh_retries(self, uid, now):
        # Two complete unsuccessful retry rounds and no recent accepted snapshot
        # identify a stuck upstream refresh. Probe once per lease until it recovers.
        row = self.db.c.execute(
            'SELECT last_success,outcome,refreshing FROM items i '
            'LEFT JOIN scan_attempts a ON a.uid=i.uid WHERE i.uid=?', (uid,)).fetchone()
        if (row and row['outcome'] == 'refreshing' and row['refreshing'] >= 8
                and (not row['last_success'] or now - row['last_success'] >= 300)):
            return 0
        return 3

    def coverage(self, now, limit=200):
        """Bounded card diagnostics; never include price payloads or credentials."""
        rows = self.db.c.execute(
            'SELECT i.uid,name,tier,last_success,last_check,next_check,lease_until,'
            'attempted_at,worker_id,outcome,requests,refreshing FROM items i '
            'LEFT JOIN scan_attempts a ON a.uid=i.uid ORDER BY last_success,i.uid LIMIT ?', (limit,)).fetchall()
        return [{'uid': r['uid'], 'name': r['name'], 'tier': r['tier'],
                 'scan_age_seconds': round(max(0, now - r['last_success']), 1)
                 if r['last_success'] else None,
                 'last_scheduled_seconds_ago': round(max(0, now - r['last_check']), 1)
                 if r['last_check'] else None,
                 'next_due_seconds': round(max(0, r['next_check'] - now), 1),
                 'lease_remaining_seconds': round(max(0, r['lease_until'] - now), 1),
                 'last_attempt_age_seconds': round(max(0, now - r['attempted_at']), 1)
                 if r['attempted_at'] else None,
                 'last_worker': r['worker_id'], 'last_outcome': r['outcome'],
                 'requests_observed': r['requests'] or 0, 'refreshing_responses': r['refreshing'] or 0}
                for r in rows]

    def summary(self, now, limit=25):
        c = self.db.c
        counts = {r['reason']: r['n'] for r in c.execute(
            'SELECT reason,COUNT(*) n FROM missed_opportunities WHERE detected_at>=? GROUP BY reason',
            (now - 86400,))}
        ages = [now - r['last_success'] for r in c.execute('SELECT last_success FROM items WHERE last_success>0')]
        total = c.execute('SELECT COUNT(*) FROM items').fetchone()[0]
        checks = c.execute('SELECT COUNT(*) FROM scan_observations WHERE scanned_at>=?', (now - 300,)).fetchone()[0]
        recent = []
        for r in c.execute('SELECT * FROM missed_opportunities ORDER BY detected_at DESC LIMIT ?', (limit,)):
            event = dict(r)
            event['scan_gap_seconds'] = round(r['detected_at'] - r['previous_scan_at'], 1)
            event['detection_delay_seconds'] = round(r['detected_at'] - r['sold_at'], 1)
            event['seconds_after_scan'] = round(r['sold_at'] - r['scan_before_sale'], 1) if r['scan_before_sale'] else None
            event['timestamp_estimated'] = True
            recent.append(event)
        return {'likely_missed_24h': counts.get('likely_missed_between_scans', 0),
                'reasons_24h': counts, 'completed_checks_last_5m': checks,
                'completed_checks_per_minute_5m': round(checks / 5, 2),
                'median_scan_age_seconds': round(median(ages), 1) if ages else None,
                'oldest_scan_age_seconds': round(max(ages), 1) if ages else None,
                'cards_without_baseline': total - len(ages),
                'cards_scanned_last_60s': sum(age <= 60 for age in ages),
                'cards_stale_over_5m': sum(age > 300 for age in ages), 'recent': recent,
                'caveat': 'Estimated sold times and price matches cannot identify an auction or prove a missed purchase. '
                          'Same-price sales within 10 minutes are conservatively deduplicated. '
                          'Tracking begins at each card\'s first successful scan; absent source sales cannot be counted.'}
