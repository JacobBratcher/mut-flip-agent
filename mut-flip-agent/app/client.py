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
        self.mode = cfg.get("fetch_mode", "browser")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        if cfg.get("api_token"):
            token = cfg["api_token"]
            header = cfg.get("api_token_header") or "Authorization"
            if header.lower() == "authorization" and " " not in token:
                token = f"Bearer {token}"
            self.s.headers[header] = token

    def _throttle(self):
        with self._lock:
            wait = self._last + self.min_gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _get(self, url, **kw):
        self._throttle()
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
    def prices(self, unique_id: str, card_url: str = "") -> dict:
        path = f"/api/mutdb/prices/{unique_id}/{self.platform}/"
        r, ctype = self._get(BASE + path)
        if "json" not in ctype:
            raise Blocked(f"non-JSON response for {unique_id}")
        return r.json().get("data") or {}

    # ---- discovery ---------------------------------------------------------
    def discover(self, min_ovr=0) -> list[tuple[str, str, str]]:
        """[(unique_id, url)] for current-game player items at or above min_ovr."""
        if min_ovr:
            return self._discover_by_ovr(min_ovr)
        return self._discover_sitemap()

    def _discover_by_ovr(self, min_ovr) -> list[tuple[str, str]]:
        """Walks mut.gg's filtered player list (HTML, 15 cards/page)."""
        found, page = {}, 1
        while page <= 200:
            try:
                r, _ = self._get(f"{BASE}/players/", params={"overall__gte": min_ovr, "page": page},
                                 headers={"Accept": "text/html"})
            except requests.HTTPError:          # past the last page
                break
            new = 0
            for uid, url, ovr, name in parse_player_list(r.text):
                if ovr >= min_ovr and uid.startswith(f"{self.game}-") and uid not in found:
                    found[uid] = (url, name)
                    new += 1
            if not new:
                break
            page += 1
        log.info("Discovered %d player items at %d+ OVR", len(found), min_ovr)
        return [(uid, url, name) for uid, (url, name) in found.items()]

    def _discover_sitemap(self) -> list[tuple[str, str]]:
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
        return [(uid, url, "") for uid, url in found.items()]

    def item_name(self, url: str) -> str:
        """Full card name from the player page title, e.g. 'T.J. Watt Team of the Week 87 OVR'."""
        r, _ = self._get(url, headers={"Accept": "text/html"})
        m = re.search(r"<title>(.*?)</title>", r.text, re.S)
        title = html.unescape(m.group(1)).strip() if m else ""
        return re.sub(r"\s*-\s*Madden NFL \d+\s*-\s*MUT\.GG\s*$", "", title) or url


def parse_player_list(page_html: str) -> list[tuple[str, str, int, str]]:
    """[(unique_id, url, ovr, name)] from a mut.gg /players/ list page."""
    out = []
    for block in page_html.split('<div class="player-list-item"')[1:]:
        link = re.search(r'href="(/players/[^"]+/(\d{2}-\d+)/)"', block)
        text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block)))
        m = re.search(r"OVR\s+(\d{2})\s+(.+?)\s+[A-Z]{3}\s+\d{2}\b", text)
        if link and m:
            ovr = int(m.group(1))
            out.append((link.group(2), BASE + link.group(1), ovr, f"{m.group(2).strip()} {ovr} OVR"))
    return out


def parse_sales(data: dict) -> list[tuple[int, str]]:
    """[(price, iso_date)] from a prices payload."""
    out = []
    for a in (data.get("pricesData") or {}).get("completedAuctions") or []:
        p, d = a.get("soldPrice"), a.get("soldDate")
        if isinstance(p, (int, float)) and p > 0 and d:
            out.append((int(p), d))
    return out


def parse_live(data: dict) -> list[tuple[int, float]]:
    """[(buy_now_price, end_unix_ts)] for active listings in a prices payload."""
    from datetime import datetime
    out = []
    for a in (data.get("pricesData") or {}).get("liveAuctions") or []:
        p, end = a.get("buyNowPrice"), a.get("endDate")
        if not isinstance(p, (int, float)) or p <= 0 or not end:
            continue
        try:
            ts = datetime.fromisoformat(str(end).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        out.append((int(p), ts))
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
