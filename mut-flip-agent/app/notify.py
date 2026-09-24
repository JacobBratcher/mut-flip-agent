"""Discord webhook messages."""
import logging
import time

import requests

log = logging.getLogger(__name__)
GREEN, GOLD, RED, BLUE = 0x2ECC71, 0xF1C40F, 0xE74C3C, 0x3498DB
BASIS = {"sales": "last 5 sales", "listings": "under cheapest rival"}


def coins(n):
    return f"{int(n):,}"


def signed(n):
    return f"{int(n):+,}".replace("-", "−")


def _pct(x):
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def _icon(title):
    from .youtube import ICON, classify
    return ICON[classify(title)]


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

    def new_cards(self, names, every_seconds, tiers):
        shown = "\n".join(f"• {n}" for n in names[:15]) + (f"\n…and {len(names) - 15} more" if len(names) > 15 else "")
        self.send([{
            "title": f"🆕 {len(names)} new card{'s' if len(names) != 1 else ''} just dropped",
            "color": GOLD,
            "description": (f"Checking {'them' if len(names) != 1 else 'it'} every "
                            f"{max(1, every_seconds // 60)} min for the next {tiers.get('fresh_hours', 48)} h "
                            f"(LTD / Champions: {tiers.get('fresh_long_hours', 168) // 24} days). Mistake "
                            f"listings way under value are most common right after a drop.\n{shown}"),
        }])

    def listing(self, name, url, d, platform, promo_today=False, fresh=False):
        safe = getattr(d, "grade", "good") == "safe"
        badge = "🟢 SAFE" if safe else "🟡 GOOD"
        trend = getattr(d, "trend", None)
        trend_txt = "n/a" if trend is None else f"{trend * 100:+.0f}% vs yesterday"
        tips = []
        if fresh:
            tips.append("🆕 New release: prices are still settling. Buy fast, list right away.")
        if d.falling:
            tips.append("⚠️ Sellers are undercutting: list right away.")
        if promo_today:
            tips.append("Promo day: if it doesn't sell at the list price today, hold it for "
                        "tomorrow's bounce instead of dumping it.")
        lines = [f"**Buy Now ≤ {coins(d.max_buy)}** (listed at {coins(d.bin_price)}). "
                 f"Ends <t:{int(d.ends)}:R>.",
                 f"Sell at **{coins(d.market)}** ({BASIS.get(d.basis, d.basis)}) → "
                 f"**{signed(d.profit)}** after tax ({d.roi:.0%} ROI)"]
        typical = getattr(d, "typical", None)
        if typical and getattr(d, "typical_profit", None) is not None:
            lines.append(f"Sell at **{coins(typical)}** (mut.gg price) → "
                         f"**{signed(d.typical_profit)}** after tax")
        embed = {
            "title": f"🎯 {badge}: {name}",
            "color": GREEN if safe else GOLD,
            "description": "\n".join(lines + tips),
            "fields": [
                {"name": "Sold last 24h", "value": str(getattr(d, "sales_24h", 0)), "inline": True},
                {"name": "Trend", "value": trend_txt, "inline": True},
            ],
            "footer": {"text": f"{platform.upper()} • live listing"},
        }
        recent = getattr(d, "recent", None)
        if recent:
            embed["fields"].append({"name": "Last sales (newest first)",
                                    "value": ", ".join(coins(p) for p in recent), "inline": False})
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


    def market_report(self, move, news, drops, snipes, platform, threshold=0.08,
                      tips=(), videos=()):
        promo_text = "\n".join(f"[{a.title}]({a.url})" for a in news[:6]) or "None in the last 24h"
        if snipes:
            best = max(snipes, key=lambda r: r["profit"])
            safe = sum(1 for r in snipes if (r["grade"] or "good") == "safe")
            snipe_text = (f"{len(snipes)} found ({safe} safe), "
                          f"{coins(sum(r['profit'] for r in snipes))} total profit\n"
                          f"Best: [{best['name']}]({best['url']}) +{coins(best['profit'])}")
        else:
            snipe_text = "None in the last 24h"
        progs = move.programs
        prog_text = "\n".join(f"{p.program}: {p.change * 100:+.0f}% ({p.cards} cards)"
                               for p in (progs[:3] + [p for p in progs[-3:] if p not in progs[:3]])
                               ) or "Not enough data yet"
        breadth = ("" if move.breadth_down is None
                   else f" · {move.breadth_down:.0%} of cards down 10%+")
        fields = [
            {"name": "Market 24h", "value": _pct(move.change_24h) + breadth, "inline": True},
            {"name": "Market 7d", "value": _pct(move.change_7d), "inline": True},
            {"name": "By program", "value": prog_text[:1024]},
            {"name": "Biggest drops", "value": _movers(move.fallers)[:1024]},
            {"name": "Biggest gains", "value": _movers(move.risers)[:1024]},
            {"name": "What to do today", "value": ("\n".join(f"• {t}" for t in tips) or "Nothing special")[:1024]},
            {"name": "New on mut.gg", "value": promo_text[:1024]},
        ]
        if videos:
            fields.append({"name": "From MUT YouTubers (💰 market · 🔮 leaks)", "value": "\n".join(
                f"{_icon(v.title)} [{v.channel}: {v.title}]({v.url})"
                for v in videos)[:1024]})
        fields += [
            {"name": "Twitch drops", "value": drops or "None live"},
            {"name": "Snipes", "value": snipe_text},
        ]
        self.send([{
            "title": "📊 Daily MUT market report",
            "color": BLUE,
            "description": f"**{verdict(move.change_24h, threshold)}**",
            "fields": fields,
            "footer": {"text": f"{platform.upper()} • daily report"},
        }])

    def program_alert(self, kind, prog, move, platform):
        down = kind == "crash"
        cards = [m for m in (move.fallers if down else move.risers)]
        self.send([{
            "title": (f"📉 {prog.program} crashing: {prog.change * 100:+.0f}%" if down
                      else f"📈 {prog.program} spiking: {prog.change * 100:+.0f}%"),
            "color": RED if down else GREEN,
            "description": (f"Median change across {prog.cards} {prog.program} cards since yesterday "
                            f"(whole market {_pct(move.change_24h)}). "
                            + ("Buy the dip only if you'll hold a day or two; don't snipe-and-relist into a slide."
                               if down else "Good time to sell any of these you own.")),
            "footer": {"text": f"{platform.upper()} • program alert"},
        }], content="@here")

    def video(self, v, kind):
        from .youtube import ICON
        self.send([{
            "title": f"{ICON.get(kind, '📺')} {v.channel}: {v.title}",
            "url": v.url,
            "color": 0xFF0000,
            "description": {"market": "Market / coins video.",
                            "leak": "Leaks or upcoming content: this is what moves prices next."}.get(kind),
            "footer": {"text": "new upload"},
        }])
