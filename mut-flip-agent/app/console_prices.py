"""Cached list-preview medians. Never substitutes console values for PC valuation."""
import math
import re
import uuid

PLATFORMS = ('pc', 'xbox-series-x', 'playstation-5')


def price(value):
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?[kKmM]?', value):
            return None
        multiplier = {'k': 1000, 'm': 1000000}.get(value[-1:].lower(), 1)
        value = float(value.rstrip('kKmM').replace(',', '')) * multiplier
    if type(value) not in (int, float) or not 0 < value <= 1_000_000_000 or not math.isfinite(value):
        return None
    return int(value) or None


class ConsolePrices:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg
        db.c.executescript('''
            CREATE TABLE IF NOT EXISTS console_previews (
                uid TEXT PRIMARY KEY, pc INTEGER, xbox INTEGER, ps5 INTEGER,
                observed_at REAL DEFAULT 0, next_check REAL DEFAULT 0,
                lease_id TEXT DEFAULT '', lease_until REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS console_preview_gate (id INTEGER PRIMARY KEY, next_at REAL);
            INSERT OR IGNORE INTO console_preview_gate VALUES (1, 0);
        ''')
        db.c.commit()

    @property
    def enabled(self):
        return self.cfg.get('console_comparison', {}).get('enabled', False) and self.cfg.get('platform') == 'pc'

    def lease(self, now):
        # Caller holds the database lock. At most one batch/minute across ALL profiles.
        if not self.enabled or self.db.c.execute('SELECT next_at FROM console_preview_gate').fetchone()[0] > now:
            return {'external_ids': []}
        game = str(self.cfg['game'])
        rows = self.db.c.execute('''SELECT i.uid FROM items i LEFT JOIN console_previews p ON p.uid=i.uid
            WHERE COALESCE(p.next_check,0)<=? AND COALESCE(p.lease_until,0)<=?
            ORDER BY COALESCE(p.next_check,0), i.uid''', (now, now)).fetchall()
        ids = [r['uid'] for r in rows if re.fullmatch(re.escape(game) + r'-[1-9]\d{0,11}', r['uid'])][:20]
        if not ids:
            return {'external_ids': []}
        lease_id = uuid.uuid4().hex
        for uid in ids:
            self.db.c.execute('INSERT OR IGNORE INTO console_previews(uid) VALUES (?)', (uid,))
            self.db.c.execute('UPDATE console_previews SET lease_id=?,lease_until=? WHERE uid=?',
                              (lease_id, now + 180, uid))
        self.db.c.execute('UPDATE console_preview_gate SET next_at=?', (now + 60,))
        self.db.c.commit()
        return {'external_ids': [int(uid.split('-')[1]) for uid in ids], 'lease_id': lease_id}

    def ingest(self, body, now):
        records, lease_id = body.get('records'), body.get('lease_id')
        if not self.enabled or not isinstance(lease_id, str) or not re.fullmatch(r'[a-f0-9]{32}', lease_id):
            raise ValueError('invalid or disabled preview lease')
        if not isinstance(records, list) or len(records) > 20:
            raise ValueError('preview batch required (max 20)')
        leased = self.db.c.execute('''SELECT p.uid FROM console_previews p JOIN items i ON p.uid=i.uid
            WHERE p.lease_id=? AND p.lease_until>=?''', (lease_id, now)).fetchall()
        allowed = {int(r['uid'].split('-')[1]): r['uid'] for r in leased}
        if not allowed:
            raise ValueError('expired preview lease')
        parsed = {}
        for r in records:
            if not isinstance(r, dict) or type(r.get('externalId')) is not int or r['externalId'] not in allowed:
                raise ValueError('unexpected preview item')
            display = r.get('priceDisplay')
            if not isinstance(display, dict) or r['externalId'] in parsed:
                raise ValueError('invalid or duplicate preview item')
            parsed[r['externalId']] = [price(display.get(p)) for p in PLATFORMS]
        for external_id, uid in allowed.items():
            # Omitted/unpriced cards clear old medians, rather than extending their apparent freshness.
            values = parsed.get(external_id, [None, None, None])
            self.db.c.execute('''UPDATE console_previews SET pc=?,xbox=?,ps5=?,observed_at=?,
                next_check=?,lease_id='',lease_until=0 WHERE uid=?''', (*values, now, now + 600, uid))
        self.db.c.commit()
        return len(allowed)

    def context(self, uid, now):
        if not self.enabled:
            return None
        row = self.db.c.execute('SELECT * FROM console_previews WHERE uid=? AND observed_at>=?',
                                (uid, now - 1800)).fetchone()
        if not row or not row['observed_at'] or not any(row[k] for k in ('xbox', 'ps5')):
            return None
        return {k: row[k] for k in ('pc', 'xbox', 'ps5', 'observed_at')} | {'age_seconds': max(0, int(now-row['observed_at']))}

    def report(self, now):
        options = self.cfg.get('console_comparison', {})
        pct, coins = options.get('min_gap_pct', 0.30), options.get('min_gap_coins', 50000)
        gaps, fresh = [], 0
        if self.enabled:
            rows = self.db.c.execute('''SELECT p.*,i.name,i.url FROM console_previews p
                JOIN items i ON i.uid=p.uid WHERE observed_at>0 AND observed_at>=?''', (now - 1800,)).fetchall()
            for r in rows:
                if not r['pc']:
                    continue
                if r['xbox'] or r['ps5']:
                    fresh += 1
                for key, platform in [('xbox', 'xbox-series-x'), ('ps5', 'playstation-5')]:
                    if not r[key]:
                        continue
                    delta = r['pc'] - r[key]
                    change = delta / r[key]
                    if abs(delta) >= coins and abs(change) >= pct:
                        gaps.append({'uid': r['uid'], 'name': r['name'], 'url': r['url'], 'console': platform,
                                     'pc_median': r['pc'], 'console_median': r[key], 'coin_gap': delta,
                                     'pc_vs_console_pct': round(change * 100, 1),
                                     'observed_at': r['observed_at'], 'age_seconds': max(0, int(now-r['observed_at']))})
        gaps.sort(key=lambda r: abs(r['coin_gap']), reverse=True)
        return {'enabled': bool(self.enabled), 'fresh_cards': fresh, 'gap_count': len(gaps), 'gaps': gaps[:100],
                'min_gap_pct': pct, 'min_gap_coins': coins, 'refresh_seconds': 600, 'max_age_seconds': 1800,
                'caveat': 'List-preview medians, not executable offers. Age is time since fetch; source sale age is unknown. PC resale estimates still use PC sales.'}
