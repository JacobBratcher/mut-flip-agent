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


def call(port, method, path, body=None, token="secret"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Feeder-Token": token, "Content-Type": "application/json"})
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
        assert len(agent.discord.listings) == 1 and agent.discord.listings[0][1].bin_price == 360_000
        call(port, "POST", "/ingest", {"uid": "27-1", "data": payload})   # same listing: no repeat
        assert len(agent.discord.listings) == 1
        assert call(port, "POST", "/ingest", {"uid": "27-999", "data": payload})[1]["ok"] is False
    finally:
        httpd.shutdown()
