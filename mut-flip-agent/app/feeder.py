"""Small HTTP API for the Chrome feeder extension.

The extension (in your own browser) asks which cards to check, fetches them from
mut.gg the same way mut.gg's pages do, and posts the results here. Each result is
processed immediately, so Discord alerts go out the moment a deal shows up.
"""
import hmac
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)
LEASE_SECONDS = 300
MAX_BODY = 2_000_000


def serve(agent, port):
    token = agent.cfg.get("feeder_token") or ""
    if not token:
        raise SystemExit("feeder_token must be set when fetch_mode is 'extension'")
    # Bounded, authenticated diagnostics only; never retain tokens or price bodies.
    clients = OrderedDict()
    clients_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _worker_id(self):
            worker_id = self.headers.get("X-Feeder-Id", "legacy")
            return worker_id if re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", worker_id) else "unknown"

        def _activity(self, route, items=0, result=None, accepted=False):
            worker_id = self._worker_id()
            key = (self.client_address[0], self.headers.get("User-Agent", "")[:200],
                   self.headers.get("X-Feeder-Version", "unknown")[:30], worker_id)
            with clients_lock:
                client = clients.setdefault(key, {"address": key[0], "user_agent": key[1],
                                                  "feeder_version": key[2], "worker_id": key[3],
                                                  "first_seen": time.time(), "last_ingest_at": 0,
                                                  "results": {}, "routes": {}})
                clients.move_to_end(key)
                if len(clients) > 64:
                    clients.popitem(last=False)
                metric = client["routes"].setdefault(route, {"calls": 0, "items": 0, "last_at": 0})
                metric["calls"] += 1
                metric["items"] += items
                metric["last_at"] = time.time()
                if accepted:
                    client["last_ingest_at"] = time.time()
                if result is not None:
                    client["results"][result] = client["results"].get(result, 0) + 1

        def _auth(self):
            got = self.headers.get("X-Feeder-Token", "")
            if hmac.compare_digest(got, token):
                return True
            self._send(401, {"error": "bad token"})
            return False

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return None
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                return None

        def do_GET(self):
            if not self._auth():
                return
            u = urlparse(self.path)
            if u.path == "/config":
                self._activity("config")
                rpm = max(1, int(agent.cfg["requests_per_minute"]))
                return self._send(200, {"platform": agent.cfg["platform"],
                                        "request_budget_version": 1,
                                        "worker_tracking_version": 1,
                                        "scan_diagnostics_version": 1,
                                        "interval_ms": int(60000 / rpm)})
            if u.path == "/queue":
                n = min(10, max(1, int(parse_qs(u.query).get("n", ["5"])[0])))
                with agent.lock:
                    rows = agent.db.lease_due(n, LEASE_SECONDS,
                                              agent.cfg.get("fill_scan_capacity", False),
                                              agent.cfg.get("min_scan_seconds", 65))
                self._activity(f"queue:{n}", len(rows))
                return self._send(200, {"items": [{"uid": r["uid"], "url": r["url"]} for r in rows]})
            if u.path == "/scan-report":
                with agent.lock:
                    report = agent.scan_tracking.summary(time.time(), limit=100)
                    report["cards"] = agent.scan_tracking.coverage(time.time())
                return self._send(200, report)
            if u.path == "/health":
                # Used by the desktop keeper's watchdog: seconds since prices last arrived
                # (counted from agent start if none yet), so it can restart a stalled browser.
                with agent.lock:
                    since = max(agent.last_ingest, getattr(agent, "started", 0))
                    state = agent.feeder_state
                    refreshes_rejected = agent.refreshes_rejected
                    verification = {"pending": len(agent.listing_checks.pending),
                                    "confirmed": agent.listing_checks.confirmed,
                                    "ambiguous_skipped": agent.listing_checks.ambiguous}
                    now = time.time()
                    scans = agent.scan_tracking.summary(now, limit=0)
                    row = agent.db.c.execute(
                        "SELECT COUNT(*) total, COALESCE(SUM(next_check<=?), 0) due, "
                        "MIN(next_check) next_at FROM items", (now,)).fetchone()
                    queue = {"tracked": row["total"], "due": row["due"],
                             "next_due_seconds": max(0, int(row["next_at"] - now))
                             if row["next_at"] is not None else None}
                with clients_lock:
                    activity = [{"address": c["address"], "user_agent": c["user_agent"],
                                 "feeder_version": c["feeder_version"],
                                 "worker_id": c["worker_id"], "first_seen": c["first_seen"],
                                 "last_ingest_at": c["last_ingest_at"], "results": dict(c["results"]),
                                 "routes": {k: dict(v) for k, v in c["routes"].items()}}
                                for c in clients.values()]
                budget = agent.api.budget.snapshot()
                return self._send(200, {"ingest_age": int(time.time() - since),
                                        "state": "blocked" if budget["cooldown_seconds"] else state,
                                        "refreshes_rejected": refreshes_rejected,
                                        "listing_verification": verification,
                                        "request_budget": budget, "queue": queue, "clients": activity,
                                        "scan_tracking": scans})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return
            u, body = urlparse(self.path), self._body()
            if body is None:
                return self._send(400, {"error": "bad body"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "object required"})
            # Independent of agent.lock: discovery may hold it while waiting for a slot.
            if u.path == "/request-budget/relearn":
                agent.api.budget.relearn()
                return self._send(200, agent.api.budget.snapshot())
            if u.path == "/request-permit":
                self._activity("request-permit")
                return self._send(200, agent.api.budget.acquire(self._worker_id()))
            if u.path == "/request-result":
                status = body.get("status")
                if type(status) is not int or not 0 <= status <= 599:
                    return self._send(400, {"error": "HTTP status required"})
                outcome = body.get("outcome", "http")
                if outcome not in ("http", "timeout", "network_error", "api_error") or (outcome != "http" and status != 0):
                    return self._send(400, {"error": "invalid result outcome"})
                self._activity("request-result", result=f"http_{status}" if outcome == "http" else outcome)
                feedback = agent.api.budget.record(status, body.get("retry_after"), outcome)
                uid = body.get("uid")
                if isinstance(uid, str):
                    with agent.lock:
                        if agent.db.item(uid):
                            result = ("refreshing" if status == 200 and body.get("refreshing") is True
                                      else f"http_{status}" if outcome == "http" else outcome)
                            agent.scan_tracking.attempt(uid, self._worker_id(), result, time.time())
                return self._send(200, feedback)
            if u.path == "/ingest":
                uid, data = body.get("uid"), body.get("data")
                if not isinstance(uid, str) or not isinstance(data, dict):
                    return self._send(400, {"error": "uid and data required"})
                self._activity("ingest")
                with agent.lock:
                    row = agent.db.item(uid)
                    if not row:
                        return self._send(200, {"ok": False, "reason": "untracked card"})
                    try:
                        accepted = agent.process(row, data)
                    except Exception:
                        log.exception("ingest failed for %s", uid)
                        return self._send(500, {"error": "processing failed"})
                    if not accepted:
                        return self._send(200, {"ok": False, "reason": "prices refreshing",
                                                "retry_after": 60})
                    agent.feeder_state = "ok"
                self._activity("ingest-accepted", accepted=True)
                return self._send(200, {"ok": True})
            if u.path == "/status":
                state = str(body.get("state", ""))[:20]
                with agent.lock:
                    changed = state != agent.feeder_state
                    agent.feeder_state = state
                    agent.last_publish = 0
                if changed and state == "blocked":
                    agent.discord.status("Feeder reports mut.gg is refusing requests in your browser. "
                                         f"It will pause and retry. Detail: `{str(body.get('detail',''))[:200]}`")
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("Feeder API listening on port %d", port)
    return httpd
