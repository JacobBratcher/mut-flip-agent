"""Fetches mut.gg price data through a real Chromium session (Playwright).

The browser loads mut.gg like a visitor, then requests prices the same way the
site's own JavaScript does. No fingerprint spoofing or anti-detection tricks: if
Cloudflare still refuses, we raise Blocked and back off.
"""
import json
import logging

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)
BASE = "https://www.mut.gg"
SKIP_TYPES = {"image", "media", "font"}
SKIP_HOSTS = ("adthrive", "googlesyndication", "doubleclick", "google-analytics",
              "googletagmanager", "amazon-adsystem", "twitch.tv")
FETCH_JS = """async (path) => {
  const r = await fetch(path, {headers: {Accept: 'application/json'}, credentials: 'same-origin'});
  return [r.status, r.headers.get('content-type') || '', await r.text()];
}"""


class BrowserFetcher:
    RECYCLE_AFTER = 400          # restart Chromium periodically to keep memory flat

    def __init__(self, blocked_exc):
        self.Blocked = blocked_exc
        self._pw = self._browser = self._page = None
        self._count = 0

    def _start(self):
        self.close()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = self._browser.new_context(locale="en-US")
        ctx.route("**/*", self._route)
        self._page = ctx.new_page()
        self._count = 0
        self._warm(f"{BASE}/players/")
        log.info("Browser session ready")

    @staticmethod
    def _route(route):
        req = route.request
        if req.resource_type in SKIP_TYPES or any(h in req.url for h in SKIP_HOSTS):
            return route.abort()
        return route.continue_()

    def _warm(self, url):
        self._page.goto(url, wait_until="load", timeout=60000)
        self._page.wait_for_timeout(4000)   # let Cloudflare's page script finish

    def get_json(self, path: str, warm_url: str = "") -> dict:
        try:
            if self._page is None or self._count >= self.RECYCLE_AFTER:
                self._start()
            self._count += 1
            status, ctype, text = self._page.evaluate(FETCH_JS, path)
            if status in (403, 503) and warm_url:
                self._warm(warm_url)          # visit the card's own page, then retry once
                status, ctype, text = self._page.evaluate(FETCH_JS, path)
        except PWError as e:
            self.close()
            raise self.Blocked(f"browser error: {str(e)[:200]}") from e
        if status == 429:
            raise self.Blocked(f"429 from {path}", 300)
        if status != 200 or "json" not in ctype:
            raise self.Blocked(f"{status} from {path} (browser)")
        return json.loads(text)

    def close(self):
        for obj in (self._browser, self._pw):
            try:
                if obj is self._pw and obj:
                    obj.stop()
                elif obj:
                    obj.close()
            except Exception:
                pass
        self._pw = self._browser = self._page = None
