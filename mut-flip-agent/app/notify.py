"""Discord webhook messages."""
import logging
import time

import requests

log = logging.getLogger(__name__)
GREEN, GOLD, RED, BLUE = 0x2ECC71, 0xF1C40F, 0xE74C3C, 0x3498DB


def coins(n):
    return f"{int(n):,}"


def _pct(x):
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def verdict(change, threshold):
    """Plain-language read of a 24h market move."""
    if change is None:
        return "Not enough data yet"
    if change <= -threshold:
        return "📉 Crash: buy window"
    if change >= threshold:
        return "📈 Bump: sell window"
    if change <= -0.03:
        return "Drifting down"
    if change >= 0.03:
        return "Drifting up"
    return "Steady"


def _movers(ms):
    return "\n".join(f"[{m.name}]({m.url}) {m.change * 100:+.0f}% → {coins(m.now_price)}"
                     for m in ms) or "None"


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
            "title": f"🎯 Snipe: {name}",
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

    # ------------------------------------------------------------- market
    def news(self, article, promo):
        embed = {
            "title": ("🆕 New promo: " if promo else "🗞️ ") + article.title,
            "url": article.url,
            "color": GOLD if promo else BLUE,
            "footer": {"text": "mut.gg news"},
        }
        if promo:
            embed["description"] = ("New cards usually pull prices down for a few hours while packs "
                                    "get opened, so expect more snipes.")
        self.send([embed])


    def drops(self, text):
        self.send([{
            "title": "🎁 Twitch drop live",
            "url": "https://twitchdrops.app/game/madden-nfl-27",
            "color": 0x9146FF,
            "description": (f"{text}\n\nWatch a Madden stream on Twitch with your EA account linked, "
                            "then claim it in your Twitch drops inventory."),
        }], content="@here")


    def market_alert(self, kind, move, platform):
        down = kind == "crash"
        self.send([{
            "title": (f"📉 Market crash: {_pct(move.change_24h)} in 24h" if down
                      else f"📈 Market bump: {_pct(move.change_24h)} in 24h"),
            "color": RED if down else GREEN,
            "description": (f"Median price change across {move.cards_24h} cards. "
                            + ("Crashes are usually the best time to buy cards you want to hold; "
                               "snipe resale values already reflect the drop." if down else
                               "A good window to sell cards you're holding before prices settle.")),
            "fields": [{"name": "Biggest drops" if down else "Biggest gains",
                        "value": _movers(move.fallers if down else move.risers)}],
            "footer": {"text": f"{platform.upper()} • market alert"},
        }], content="@here")


    def market_report(self, move, news, drops, snipes, platform, threshold=0.08):
        promos = [a for a in news]
        promo_text = "\n".join(f"[{a.title}]({a.url})" for a in promos[:6]) or "None in the last 24h"
        if snipes:
            best = max(snipes, key=lambda r: r["profit"])
            snipe_text = (f"{len(snipes)} found, {coins(sum(r['profit'] for r in snipes))} total profit\n"
                          f"Best: [{best['name']}]({best['url']}) +{coins(best['profit'])}")
        else:
            snipe_text = "None in the last 24h"
        self.send([{
            "title": "📊 Daily MUT market report",
            "color": BLUE,
            "description": f"**{verdict(move.change_24h, threshold)}**",
            "fields": [
                {"name": "Market 24h", "value": _pct(move.change_24h), "inline": True},
                {"name": "Market 7d", "value": _pct(move.change_7d), "inline": True},
                {"name": "Cards measured", "value": str(move.cards_24h), "inline": True},
                {"name": "Biggest drops", "value": _movers(move.fallers)},
                {"name": "Biggest gains", "value": _movers(move.risers)},
                {"name": "New on mut.gg", "value": promo_text[:1024]},
                {"name": "Twitch drops", "value": drops or "None live"},
                {"name": "Snipes", "value": snipe_text},
            ],
            "footer": {"text": f"{platform.upper()} • daily report"},
        }])
