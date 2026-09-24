"""Discord webhook messages."""
import logging
import time

import requests

log = logging.getLogger(__name__)
GREEN, GOLD, RED, BLUE = 0x2ECC71, 0xF1C40F, 0xE74C3C, 0x3498DB


def coins(n):
    return f"{int(n):,}"


class Discord:
    def __init__(self, url):
        self.url = url

    def send(self, embeds=None, content=None):
        payload = {"username": "MUT Flip Agent"}
        if content:
            payload["content"] = content
        if embeds:
            payload["embeds"] = embeds[:10]
        for _ in range(3):
            try:
                r = requests.post(self.url, json=payload, timeout=15)
                if r.status_code == 429:
                    time.sleep(float(r.json().get("retry_after", 2)))
                    continue
                r.raise_for_status()
                return True
            except requests.RequestException as e:
                log.warning("Discord send failed: %s", e)
                time.sleep(3)
        return False

    def flip(self, name, url, f, platform):
        warn = "\n⚠️ Market is sliding. Resell quickly or skip." if f.falling else ""
        embed = {
            "title": f"💸 Flip: {name}",
            "color": GOLD if f.falling else GREEN,
            "description": (f"Just sold for **{coins(f.buy_seen)}**, "
                            f"{f.discount:.0%} under market. Look for listings near this price.{warn}"),
            "fields": [
                {"name": "Buy at or under", "value": coins(f.max_buy), "inline": True},
                {"name": "Resell around", "value": coins(f.market), "inline": True},
                {"name": "Profit after tax", "value": f"{coins(f.profit)} ({f.roi:.0%})", "inline": True},
            ],
            "footer": {"text": f"{platform.upper()} • resale based on {f.basis}"},
        }
        if url:
            embed["url"] = url
        self.send([embed])

    def listing(self, name, url, d, platform):
        warn = "\n⚠️ Market is sliding. Resell quickly or skip." if d.falling else ""
        embed = {
            "title": f"🚨 Listed now: {name}",
            "color": RED,
            "description": (f"Buy Now **{coins(d.bin_price)}**, {d.discount:.0%} under market. "
                            f"Ends <t:{int(d.ends)}:R>.{warn}"),
            "fields": [
                {"name": "Buy Now", "value": coins(d.bin_price), "inline": True},
                {"name": "Resell around", "value": coins(d.market), "inline": True},
                {"name": "Profit after tax", "value": f"{coins(d.profit)} ({d.roi:.0%})", "inline": True},
            ],
            "footer": {"text": f"{platform.upper()} • live listing • resale based on {d.basis}"},
        }
        if url:
            embed["url"] = url
        self.send([embed], content="@here")

    def digest(self, picks, platform):
        if not picks:
            self.send(content="📈 Investment digest: no cards meet your rules today.")
            return
        embeds = []
        for name, url, iv in picks:
            tag = " • 🧱 at record low" if iv.record_low else ""
            embeds.append({
                "title": f"📈 {name}",
                    "color": BLUE,
                "description": f"{iv.drawdown:.0%} below its recent high, now leveling off{tag}.",
                "fields": [
                    {"name": "Buy now", "value": coins(iv.current), "inline": True},
                    {"name": "Sell target", "value": coins(iv.target), "inline": True},
                    {"name": "Profit after tax", "value": f"{coins(iv.profit)} ({iv.roi:.0%})", "inline": True},
                    {"name": "Recent high", "value": coins(iv.high), "inline": True},
                    {"name": "Sales/day", "value": f"{iv.daily_sales:.1f}", "inline": True},
                ],
                "footer": {"text": f"{platform.upper()} • long-term hold"},
            })
            if url:
                embeds[-1]["url"] = url
        for i in range(0, len(embeds), 10):
            self.send(embeds[i:i + 10])

    def status(self, text, color=RED):
        self.send([{"title": "MUT Flip Agent", "description": text, "color": color}])
