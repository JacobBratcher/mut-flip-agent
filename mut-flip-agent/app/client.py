"""mut.gg HTTP client: rate-limited, identifies itself, and backs off when blocked.

It never tries to get around Cloudflare. If mut.gg serves a challenge page, the
agent pauses with exponential backoff and alerts you on Discord.
"""
import html
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger(__name__)
BASE = "https://www.mut.gg"
USER_AGENT = "MUT-Flip-Agent/1.0 (personal price-alert tool; operated with mut.gg permission)"
UNIQUE_ID_RE = re.compile(r"/players/[^/]+/(\d{2}-\d+)/?")


class Blocked(Exception):
    """mut.gg returned a challenge/403/429 instead of data."""

    def __init__(self, msg, retry_after=0):
        super().__init__(msg)
        self.retry_after = retry_after


class MutGG:
    def __init__(self, cfg):
        self.platform = cfg["platform"]
        self.game = str(cfg["game"])
        self.min_gap = 60.0 / max(1, int(cfg["requests_per_minute"]))
        self._last = 0.0
        self._lock = threading.Lock()
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        if cfg.get("api_token"):
            token = cfg["api_token"]
            header = cfg.get("api_token_header") or "Authorization"
            if header.lower() == "authorization" and " " not in token:
                token = f"Bearer {token}"
            self.s.headers[header] = token

    def _get(self, url, **kw):
        with self._lock:
            wait = self._last + self.min_gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        r = self.s.get(url, timeout=30, **kw)
        ctype = r.headers.get("content-type", "")
        if r.status_code in (403, 429, 503) or "Just a moment" in r.text[:600]:
            try:
                retry = int(r.headers.get("Retry-After", 0))
            except ValueError:
                retry = 0
            raise Blocked(f"{r.status_code} from {url}", retry)
        r.raise_for_status()
        return r, ctype

    # ---- prices -----------------------------------------------------------
    def prices(self, unique_id: str) -> dict:
        r, ctype = self._get(f"{BASE}/api/mutdb/prices/{unique_id}/{self.platform}/")
        if "json" not in ctype:
            raise Blocked(f"non-JSON response for {unique_id}")
        return r.json().get("data") or {}

    # ---- discovery (sitemap) ---------------------------------------------
    def discover(self) -> list[tuple[str, str]]:
        """Returns [(unique_id, url)] for every current-game player item."""
        found, page = {}, 1
        while True:
            url = f"{BASE}/sitemap-player-detail-{self.game}.xml" + (f"?p={page}" if page > 1 else "")
            try:
                r, _ = self._get(url, headers={"Accept": "application/xml"})
            except requests.HTTPError:
                break
            locs = [e.text for e in ET.fromstring(r.content).iter() if e.tag.endswith("loc")]
            new = 0
            for loc in locs:
                m = UNIQUE_ID_RE.search(loc or "")
                if m and m.group(1) not in found:
                    found[m.group(1)] = loc
                    new += 1
            if not new:
                break
            page += 1
        log.info("Discovered %d player items", len(found))
        return list(found.items())

    def item_name(self, url: str) -> str:
        """Full card name from the player page title, e.g. 'T.J. Watt Team of the Week 87 OVR'."""
        r, _ = self._get(url, headers={"Accept": "text/html"})
        m = re.search(r"<title>(.*?)</title>", r.text, re.S)
        title = html.unescape(m.group(1)).strip() if m else ""
        return re.sub(r"\s*-\s*Madden NFL \d+\s*-\s*MUT\.GG\s*$", "", title) or url


def parse_sales(data: dict) -> list[tuple[int, str]]:
    """[(price, iso_date)] from a prices payload."""
    out = []
    for a in (data.get("pricesData") or {}).get("completedAuctions") or []:
        p, d = a.get("soldPrice"), a.get("soldDate")
        if isinstance(p, (int, float)) and p > 0 and d:
            out.append((int(p), d))
    return out


def normalize_watch(entry: str) -> tuple[str, str] | None:
    """Accepts a full mut.gg URL or a unique id like '27-162004004'."""
    entry = entry.strip()
    m = UNIQUE_ID_RE.search(entry)
    if m:
        return m.group(1), entry
    if re.fullmatch(r"\d{2}-\d+", entry):
        return entry, ""
    return None
