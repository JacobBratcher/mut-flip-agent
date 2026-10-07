"""Feeder API end to end: queue -> ingest -> instant live-listing alert."""
import json
import urllib.request
from datetime import datetime, timedelta, timezone

from app import feeder, main
from app.config import DEFAULTS
from tests.test_agent import FakeDiscord, iso


class Disc(FakeDiscord):
    def __init__(self):
        super().__init__()
        self.listings = []

    def listing(self, name, url, d, platform, **kw):
        self.listings.append((name, d))


def call(port, method, path, body=None, token="secret", worker_id=None):
    headers = {"X-Feeder-Token": token, "Content-Type": "application/json"}
    if worker_id is not None:
        headers["X-Feeder-Id"] = worker_id
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_feeder_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = DEFAULTS | {"discord_webhook_url": "x", "discover_all_players": False,
                      "fetch_mode": "extension", "feeder_token": "secret"}
    agent = main.Agent(cfg)
    agent.discord = Disc()
    agent.db.upsert_item("27-1", "https://www.mut.gg/players/1-x/27-1/")
    agent.db.set_name("27-1", "Test Player 90 OVR")
    httpd = feeder.serve(agent, 0)
    port = httpd.server_address[1]
    try:
        assert call(port, "GET", "/queue?n=5", token="nope")[0] == 401
        code, health = call(port, "GET", "/health")
        assert health["queue"] == {"tracked": 1, "due": 1, "next_due_seconds": 0}
        assert health["clients"] == []
        # Reading health must not lease the due card.
        assert call(port, "GET", "/health")[1]["queue"]["due"] == 1
        assert call(port, "GET", "/config")[1]["request_budget_version"] == 1
        assert call(port, "POST", "/request-permit", {}, token="nope")[0] == 401
        assert call(port, "POST", "/request-permit", {})[1]["allowed"]
        assert not call(port, "POST", "/request-permit", {})[1]["allowed"]
        assert call(port, "POST", "/request-result", {"status": "429"})[0] == 400
        assert call(port, "POST", "/request-result", {"status": 429, "retry_after": "7200"})[1]["wait_ms"] > 7199000
        assert call(port, "POST", "/request-permit", {})[1]["blocked"]
        learned = agent.api.budget.target
        pace = agent.api.budget.rate
        assert call(port, "POST", "/request-budget/relearn", {}, token="nope")[0] == 401
        assert agent.api.budget.target == learned
        code, relearn = call(port, "POST", "/request-budget/relearn", {})
        assert code == 200 and relearn['learned_ceiling'] == relearn['ceiling']
        assert relearn['rpm'] == pace
        assert relearn['cooldown_seconds'] > 7199
        code, q = call(port, "GET", "/queue?n=5")
        assert code == 200 and [i["uid"] for i in q["items"]] == ["27-1"]
        assert call(port, "GET", "/queue?n=5")[1]["items"] == []      # leased, not reissued

        sales = [{"soldPrice": 500_000, "soldDate": iso(h)} for h in range(1, 40, 3)]
        call(port, "POST", "/ingest", {"uid": "27-1", "data": {"pricesData": {"completedAuctions": sales}}})
        end = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        payload = {"pricesData": {"completedAuctions": sales,
                                  "liveAuctions": [{"buyNowPrice": 360_000, "endDate": end, "bidCount": 0}]}}
        code, r = call(port, "POST", "/ingest", {"uid": "27-1", "data": payload})
        assert code == 200 and r["ok"]
        assert agent.discord.listings == []
        verified_at = main.time.time() + 66
        monkeypatch.setattr(main.time, "time", lambda: verified_at)
        call(port, "POST", "/ingest", {"uid": "27-1", "data": payload})
        assert len(agent.discord.listings) == 1 and agent.discord.listings[0][1].bin_price == 360_000
        call(port, "POST", "/ingest", {"uid": "27-1", "data": payload})   # same listing: no repeat
        assert len(agent.discord.listings) == 1
        assert call(port, "POST", "/ingest", {"uid": "27-999", "data": payload})[1]["ok"] is False
        code, h = call(port, "GET", "/health")
        assert code == 200 and h["ingest_age"] < 5 and h["state"] == "blocked"
        assert h["request_budget"]["cooldown_seconds"] > 7100
        assert h["queue"]["tracked"] == 1 and h["queue"]["due"] == 0
        activity = h["clients"][0]["routes"]
        assert activity["queue:5"]["calls"] == 2
        assert activity["queue:5"]["items"] == 1
        assert activity["request-permit"]["calls"] == 3  # excludes unauthorized request
        assert activity["ingest"]["calls"] == 5
        assert h["listing_verification"]["confirmed"] == 1
        assert "secret" not in json.dumps(h)
        assert call(port, "GET", "/health", token="nope")[0] == 401
    finally:
        httpd.shutdown()


def test_refreshing_snapshot_cannot_alert_or_count_as_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = DEFAULTS | {"discord_webhook_url": "x", "discover_all_players": False,
                      "fetch_mode": "extension", "feeder_token": "secret"}
    agent = main.Agent(cfg)
    agent.discord = Disc()
    agent.db.upsert_item("27-1", "")
    httpd = feeder.serve(agent, 0)
    port = httpd.server_address[1]
    sales = [{"soldPrice": 500_000, "soldDate": iso(h)} for h in range(1, 40, 3)]
    end = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    data = {"updating": True, "pricesData": {"completedAuctions": sales,
            "liveAuctions": [{"buyNowPrice": 360_000, "endDate": end}]}}
    try:
        call(port, "GET", "/queue?n=5")
        code, response = call(port, "POST", "/ingest", {"uid": "27-1", "data": data})
        assert code == 200 and response == {"ok": False, "reason": "prices refreshing", "retry_after": 60}
        assert agent.discord.listings == []
        assert agent.db.sales_since("27-1", 0) == []
        assert agent.checks_today == 0 and agent.last_ingest == 0 and agent.last_price_ts is None
        health = call(port, "GET", "/health")[1]
        assert health["refreshes_rejected"] == 1
        assert 55 <= health["queue"]["next_due_seconds"] <= 60

        # The finished refresh removed the sold auction: no stale alert was logged.
        data["updating"] = False
        data["pricesData"]["liveAuctions"] = []
        assert call(port, "POST", "/ingest", {"uid": "27-1", "data": data})[1]["ok"]
        assert agent.checks_today == 1 and agent.last_ingest > 0
        assert agent.discord.listings == [] and agent.db.recent_flips() == []

        # A later finished snapshot with a real opportunity still alerts normally.
        data["pricesData"]["liveAuctions"] = [{"buyNowPrice": 360_000, "endDate": end}]
        assert call(port, "POST", "/ingest", {"uid": "27-1", "data": data})[1]["ok"]
        assert agent.discord.listings == []
        verified_at = main.time.time() + 66
        monkeypatch.setattr(main.time, "time", lambda: verified_at)
        assert call(port, "POST", "/ingest", {"uid": "27-1", "data": data})[1]["ok"]
        assert len(agent.discord.listings) == 1
    finally:
        httpd.shutdown()


def test_two_sessions_share_leases_permits_and_cooldowns_but_have_separate_health(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    agent = main.Agent(DEFAULTS | {'discord_webhook_url': 'x', 'feeder_token': 'secret',
                                  'http_requests_per_minute': 16})
    agent.discord = Disc()
    for uid in ('27-1', '27-2'):
        agent.db.upsert_item(uid)
    now = [1_800_000_000.0]
    agent.api.budget.clock = lambda: now[0]
    httpd = feeder.serve(agent, 0)
    port = httpd.server_address[1]
    workers = ['primary', 'secondary']
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            leased = list(pool.map(lambda w: call(port, 'GET', '/queue?n=1', worker_id=w)[1]['items'][0]['uid'], workers))
            permits = list(pool.map(lambda w: call(port, 'POST', '/request-permit', {}, worker_id=w)[1], workers))
        assert set(leased) == {'27-1', '27-2'}
        assert sum(p['allowed'] for p in permits) == 1  # 16 total, never 16 per session
        now[0] += 3.75
        assert call(port, 'POST', '/request-permit', {}, worker_id='secondary')[1]['allowed']
        assert call(port, 'POST', '/request-result', {'status': 0, 'outcome': 'timeout'}, worker_id='secondary')[0] == 200
        assert agent.api.budget.refusals == 0
        assert call(port, 'POST', '/request-result', {'status': 403, 'outcome': 'timeout'}, worker_id='secondary')[0] == 400
        assert call(port, 'POST', '/ingest', {'uid': leased[0], 'data': {'pricesData': {}}}, worker_id='primary')[1]['ok']
        assert not call(port, 'POST', '/ingest', {'uid': leased[1], 'data': {'updating': True}}, worker_id='secondary')[1]['ok']
        health = call(port, 'GET', '/health')[1]
        clients = {c['worker_id']: c for c in health['clients']}
        assert clients['primary']['last_ingest_at'] > 0
        assert clients['secondary']['last_ingest_at'] == 0
        assert clients['secondary']['results'] == {'timeout': 1}
        assert 'secret' not in json.dumps(health)
        call(port, 'POST', '/request-result', {'status': 429, 'retry_after': '7200'}, worker_id='primary')
        other = call(port, 'POST', '/request-permit', {}, worker_id='secondary')[1]
        assert other['blocked'] and not other['allowed'] and other['wait_ms'] == 7200000
        assert agent.api.budget.refusals == 1
    finally:
        httpd.shutdown()
