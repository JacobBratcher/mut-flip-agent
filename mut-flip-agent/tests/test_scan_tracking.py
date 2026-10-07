from copy import deepcopy
from datetime import datetime, timezone

from app import main, feeder
from app.config import DEFAULTS
from app.db import DB
from app.scan_tracking import ScanTracking
from tests.test_feeder import Disc, call

NOW = 1_800_000_000
HISTORY = [(500_000, NOW - 3600 * n) for n in range(1, 12)]


def setup(tmp_path):
    db = DB(tmp_path / 'mut.db')
    db.upsert_item('27-1', 'https://www.mut.gg/players/1/27-1/')
    return db, ScanTracking(db), deepcopy(DEFAULTS)


def test_single_card_requests_cannot_starve_overdue_cards(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.time.time', lambda: NOW)
    path = tmp_path / 'fair.db'
    db = DB(path)
    for uid, tier in [('fresh', 'fresh'), ('old-a', 'cold'), ('old-b', 'new')]:
        db.upsert_item(uid, tier=tier)
        db.set_schedule(uid, tier, NOW - (1 if uid == 'fresh' else 3600))
    leased = []
    for i in range(5):
        row = db.lease_due(1, 300)[0]
        leased.append(row['uid'])
        if row['uid'] == 'fresh':
            db.set_schedule('fresh', 'fresh', NOW - 1)  # always due, as under a low budget
        if i == 1:
            db.c.close()
            db = DB(path)  # fairness survives keeper/server restarts
    assert leased == ['fresh', 'fresh', 'fresh', 'old-b', 'old-a']
    assert db.item('old-a')['lease_until'] == NOW + 300
    assert db.item('old-b')['lease_until'] == NOW + 300


def test_fair_batch_leases_are_unique(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.time.time', lambda: NOW)
    db = DB(tmp_path / 'batch.db')
    for uid in range(8):
        db.upsert_item(str(uid), tier='fresh' if uid < 4 else 'cold')
    rows = db.lease_due(8, 300)
    assert len(rows) == len({row['uid'] for row in rows}) == 8
    assert db.lease_due(8, 300) == []


def record(db, tracker, cfg, now, new=(), listings=(), history=None):
    tracker.record(db.item('27-1'), new, history or HISTORY, listings, 10, 500_000, cfg, now)


def test_baseline_then_miss_with_successful_scan_timestamps(tmp_path):
    db, tracker, cfg = setup(tmp_path)
    record(db, tracker, cfg, NOW, [(300_000, NOW - 10)])
    assert tracker.summary(NOW)['recent'] == []
    db.set_schedule('27-1', 'hot', NOW + 999)
    record(db, tracker, cfg, NOW + 180, [(300_000, NOW + 60)])
    report = tracker.summary(NOW + 180)
    event = report['recent'][0]
    assert report['likely_missed_24h'] == 1
    assert event['previous_scan_at'] == event['scan_before_sale'] == NOW
    assert event['seconds_after_scan'] == 60
    assert event['scan_gap_seconds'] == 180
    assert event['detection_delay_seconds'] == 120
    assert event['estimated_profit'] == 150_000
    assert event['timestamp_estimated']
    assert db.item('27-1')['last_success'] == NOW + 180


def test_restart_and_restamped_sale_do_not_double_count(tmp_path):
    db, tracker, cfg = setup(tmp_path)
    record(db, tracker, cfg, NOW)
    record(db, tracker, cfg, NOW + 100, [(300_000, NOW + 50)])
    db.c.close()
    db = DB(tmp_path / 'mut.db')
    tracker = ScanTracking(db)
    record(db, tracker, cfg, NOW + 200, [(300_000, NOW + 51)])
    assert tracker.summary(NOW + 200)['likely_missed_24h'] == 1
    assert len(tracker.summary(NOW + 200)['recent']) == 1


def test_confirmation_alerted_and_delayed_are_distinct(tmp_path):
    db, tracker, cfg = setup(tmp_path)
    record(db, tracker, cfg, NOW, listings=[(300_000, NOW + 3600)])
    record(db, tracker, cfg, NOW + 100, [(300_000, NOW + 50)])
    assert tracker.summary(NOW + 100)['recent'][0]['reason'] == 'seen_before_sale_unconfirmed'
    db.c.execute('INSERT INTO alerts VALUES(?,?,?)', ('27-1', 'price:310000', NOW + 110))
    record(db, tracker, cfg, NOW + 200, [(310_000, NOW + 150)])
    assert tracker.summary(NOW + 200)['recent'][0]['reason'] == 'price_match_alerted'
    record(db, tracker, cfg, NOW + 300, [(320_000, NOW + 90)])
    event = tracker.summary(NOW + 300)['recent'][0]
    assert event['reason'] == 'sale_reported_late'
    assert event['scan_before_sale'] == NOW
    assert tracker.summary(NOW + 300)['likely_missed_24h'] == 0


def test_no_hindsight_profit_and_filters_respected(tmp_path):
    db, tracker, cfg = setup(tmp_path)
    record(db, tracker, cfg, NOW, history=[(200_000, t) for _, t in HISTORY])
    record(db, tracker, cfg, NOW + 100, [(300_000, NOW + 50)], history=HISTORY)
    assert tracker.summary(NOW + 100)['recent'] == []
    cfg['flip']['max_buy_budget'] = 100_000
    record(db, tracker, cfg, NOW + 200, [(300_000, NOW + 150)])
    assert tracker.summary(NOW + 200)['recent'] == []


def test_future_and_prebaseline_sales_not_counted(tmp_path):
    db, tracker, cfg = setup(tmp_path)
    record(db, tracker, cfg, NOW)
    record(db, tracker, cfg, NOW + 100, [(300_000, NOW - 1), (310_000, NOW + 101)])
    assert tracker.summary(NOW + 100)['recent'] == []


def test_fill_does_not_duplicate_leases_or_overscan(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.time.time', lambda: NOW)
    db, tracker, cfg = setup(tmp_path)
    for uid in ['due', 'old', 'recent', 'inflight']:
        db.upsert_item(uid)
    db.c.execute('UPDATE items SET next_check=?,last_success=?', (NOW + 600, NOW - 100))
    db.c.execute("UPDATE items SET next_check=0 WHERE uid='due'")
    db.c.execute("UPDATE items SET last_success=? WHERE uid='old'", (NOW - 300,))
    db.c.execute("UPDATE items SET last_success=? WHERE uid='recent'", (NOW - 20,))
    db.c.execute("UPDATE items SET lease_until=? WHERE uid='inflight'", (NOW + 200,))
    assert [r['uid'] for r in db.lease_due(2, 300, True)] == ['due', 'old']
    assert [r['uid'] for r in db.lease_due(2, 300, True)] == ['27-1']
    assert db.lease_due(2, 300, True) == []
    monkeypatch.setattr('app.db.time.time', lambda: NOW + 301)
    assert db.lease_due(1, 300, True)


def test_migration_preserves_existing_items(tmp_path):
    import sqlite3
    p = tmp_path / 'mut.db'
    c = sqlite3.connect(p)
    c.execute('CREATE TABLE items(uid TEXT PRIMARY KEY,url TEXT,name TEXT,tier TEXT,next_check REAL,last_check REAL,last_sale_seen TEXT,score REAL)')
    c.execute("INSERT INTO items VALUES('kept','','','hot',10,5,'',42)")
    c.commit(); c.close()
    db = DB(p)
    assert db.item('kept')['score'] == 42
    assert db.item('kept')['last_success'] == db.item('kept')['lease_until'] == 0


def test_refresh_failure_not_scan_and_cannot_fill_around_retry(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setattr(main.time, 'time', lambda: NOW)
    agent = main.Agent(DEFAULTS | {'discord_webhook_url': 'x', 'feeder_token': 'secret', 'fill_scan_capacity': True})
    agent.discord = Disc()
    agent.db.upsert_item('27-1')
    sales = [{'soldPrice':p, 'soldDate':datetime.fromtimestamp(t, timezone.utc).isoformat()} for p,t in HISTORY]
    agent.process(agent.db.item('27-1'), {'pricesData': {'completedAuctions': sales}})
    monkeypatch.setattr(main.time, 'time', lambda: NOW + 100)
    agent.process(agent.db.item('27-1'), {'updating': True})
    assert agent.db.item('27-1')['last_success'] == NOW
    assert agent.db.lease_due(1, 300, True) == []
    assert agent.scan_tracking.summary(NOW + 100)['completed_checks_last_5m'] == 1
    httpd = feeder.serve(agent, 0)
    try:
        port = httpd.server_address[1]
        assert call(port, 'GET', '/scan-report', token='wrong')[0] == 401
        assert call(port, 'GET', '/scan-report')[1]['completed_checks_last_5m'] == 1
        assert 'scan_tracking' in call(port, 'GET', '/health')[1]
    finally:
        httpd.shutdown()


def test_existing_card_gets_early_baseline_without_calling_old_check_success(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.time.time', lambda: NOW)
    db, tracker, cfg = setup(tmp_path)
    db.c.execute('UPDATE items SET last_check=?, next_check=?', (NOW - 200, NOW + 3600))
    assert db.item('27-1')['last_success'] == 0
    assert db.lease_due(1, 300, False) == []
    assert [r['uid'] for r in db.lease_due(1, 300, True)] == ['27-1']
    assert db.lease_due(1, 300, True) == []


def test_reporting_network_wait_does_not_block_feeder(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    agent = main.Agent(DEFAULTS | {'discord_webhook_url': 'x', 'feeder_token': 'secret'})
    entered, release = threading.Event(), threading.Event()
    def slow_report():
        entered.set()
        release.wait(5)
    agent.maybe_market = slow_report
    agent.maybe_digest = lambda: None
    agent.publish = lambda: None
    agent.db.upsert_item('27-1')
    httpd = feeder.serve(agent, 0)
    thread = threading.Thread(target=agent.maintenance)
    thread.start()
    try:
        assert entered.wait(1)
        with ThreadPoolExecutor() as pool:
            future = pool.submit(call, httpd.server_address[1], 'GET', '/queue?n=1')
            assert future.result(timeout=1)[1]['items'][0]['uid'] == '27-1'
            assert call(httpd.server_address[1], 'GET', '/health')[0] == 200
    finally:
        release.set()
        thread.join(2)
        httpd.shutdown()


def test_coverage_exposes_stale_missing_and_leased_cards(tmp_path):
    db, tracker, cfg = setup(tmp_path)
    for uid in ['missing', 'stale', 'recent']:
        db.upsert_item(uid)
    db.c.execute("UPDATE items SET last_success=? WHERE uid='stale'", (NOW - 3600,))
    db.c.execute("UPDATE items SET last_success=?,lease_until=?,next_check=? WHERE uid='recent'",
                 (NOW - 30, NOW + 250, NOW + 250))
    report = tracker.summary(NOW)
    assert report['cards_scanned_last_60s'] == 1
    assert report['cards_stale_over_5m'] == 1
    assert report['cards_without_baseline'] == 2
    cards = {r['uid']: r for r in tracker.coverage(NOW)}
    assert cards['missing']['scan_age_seconds'] is None
    assert cards['stale']['scan_age_seconds'] == 3600
    assert cards['recent']['lease_remaining_seconds'] == 250
    assert cards['recent']['next_due_seconds'] == 250
    assert len(tracker.coverage(NOW, limit=2)) == 2
