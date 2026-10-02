"""Small HTTP API for the Chrome feeder extension.

The extension (in your own browser) asks which cards to check, fetches them from
mut.gg the same way mut.gg's pages do, and posts the results here. Each result is
processed immediately, so Discord alerts go out the moment a deal shows up.
"""
import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)
LEASE_SECONDS = 300
MAX_BODY = 2_000_000


def serve(agent, port):
    token = agent.cfg.get("feeder_token") or ""
    if not token:
        raise SystemExit("feeder_token must be set when fetch_mode is 'extension'")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

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
                rpm = max(1, int(agent.cfg["requests_per_minute"]))
                return self._send(200, {"platform": agent.cfg["platform"],
                                        "request_budget_version": 1,
                                        "interval_ms": int(60000 / rpm)})
            if u.path == "/queue":
                n = min(10, max(1, int(parse_qs(u.query).get("n", ["5"])[0])))
                with agent.lock:
                    rows = agent.db.lease_due(n, LEASE_SECONDS)
                return self._send(200, {"items": [{"uid": r["uid"], "url": r["url"]} for r in rows]})
            if u.path == "/health":
                # Used by the desktop keeper's watchdog: seconds since prices last arrived
                # (counted from agent start if none yet), so it can restart a stalled browser.
                with agent.lock:
                    since = max(agent.last_ingest, getattr(agent, "started", 0))
                    state = agent.feeder_state
                budget = agent.api.budget.snapshot()
                return self._send(200, {"ingest_age": int(time.time() - since),
                                        "state": "blocked" if budget["cooldown_seconds"] else state,
                                        "request_budget": budget})
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
            if u.path == "/request-permit":
                return self._send(200, agent.api.budget.acquire())
            if u.path == "/request-result":
                status = body.get("status")
                if type(status) is not int or not 0 <= status <= 599:
                    return self._send(400, {"error": "HTTP status required"})
                return self._send(200, agent.api.budget.record(status, body.get("retry_after")))
            if u.path == "/ingest":
                uid, data = body.get("uid"), body.get("data")
                if not isinstance(uid, str) or not isinstance(data, dict):
                    return self._send(400, {"error": "uid and data required"})
                with agent.lock:
                    row = agent.db.item(uid)
                    if not row:
                        return self._send(200, {"ok": False, "reason": "untracked card"})
                    try:
                        agent.process(row, data)
                    except Exception:
                        log.exception("ingest failed for %s", uid)
                        return self._send(500, {"error": "processing failed"})
                    agent.feeder_state = "ok"
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
