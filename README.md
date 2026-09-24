# MUT Flip Agent

Watches Madden Ultimate Team prices on [mut.gg](https://www.mut.gg) (PC market) and posts a **🎯 Snipe** to Discord the moment a card is listed for Buy Now far enough under its resale value to flip for a profit after tax.

It also keeps you on top of the market as a whole:
- **📉 / 📈 Crash and bump alerts** (instant, `@here`) when the median card price moves `market.alert_pct` (default 8%), comparing each card's latest sales (last 6h) with the same card yesterday, so a crash that starts mid-day shows in full. One card can't trigger it.
- **Program alerts** when one program (Legends, Team of the Week, Team Builders…) moves `market.program_alert_pct` (15%), even if the overall market doesn't.
- **📊 Daily report** at `market.report_hour` (default 9 AM): market change over 24h and 7d, biggest drops and gains, new mut.gg promos, live Twitch drops, and yesterday's snipes.
- **🆕 New promos** posted as mut.gg publishes them (checked every 30 min).
- **📺 MUT YouTubers:** new uploads from `youtube_channels` (default [GutFoxx](https://www.youtube.com/@GutFoxx); add any `@handle`), with market/coin videos marked 💰.
- **What to do today** in the daily report: promo-day dip and next-day bounce, midweek buying / weekend selling, and the big seasonal crashes (Road to the Playoffs, Team of the Year + Super Bowl, NFL Draft), based on [GutFoxx's market guides](https://gutfoxx.com/tag/madden-market/) and the mut.gg community.
- **🎁 Twitch drop reminders** (`@here`) when a Madden drop campaign goes live, from [twitchdrops.app](https://twitchdrops.app/game/madden-nfl-27) (checked every 3 h).

Optional, off by default: alerts on cheap *completed* sales (`flip.sale_alerts`; someone already bought those, so there's nothing to snipe) and a daily long-term investment digest (`invest.enabled`).

Runs 24/7 as a Home Assistant add-on or a plain Docker container. Data access is used with mut.gg's permission.

## How it decides

**Snipes**
1. Resale value is what you could realistically sell for *right now*, not a slow average (PC has little volume):
   - the **cheapest competing Buy Now listing**, which you'd have to undercut to sell, so the estimate follows listings down immediately;
   - capped at **+10% over the last 3 sales**, so one optimistic seller can't inflate a flip.
2. A card needs only **3 sales in the last 7 days** to be priced.
3. An alert fires only if, after the 10% auction tax, it clears **both** `min_profit` coins and `min_roi`, and it's at least `min_discount` under resale value.
4. Each alert shows the **max price to buy at**, **what to list it for**, and whether that resale price came from listings or sales.
5. A listing that's alerted and then sells doesn't alert again as a cheap sale.
6. **Only cards you can resell fast:** at least `min_sales_24h` (3) sales in the last 24h.
7. **No falling knives:** skipped if the card's latest sales are down `max_drop` (15%) or more from the same time yesterday.
8. **Graded:** 🟢 **SAFE** = sells 8+/day, flat or rising, 15%+ ROI. 🟡 **GOOD** = passes the rules above. Set `only_safe: true` to get only green ones.

**Investments** (off by default; sent once a day at `digest_hour` when `invest.enabled` is on)
- At least `min_days_history` days of data and `min_daily_sales` sales/day (so you can actually sell later).
- Down at least `min_drawdown` from its high in the window.
- Not still dropping (yesterday wasn't more than 3% below the day before).
- Sell target assumes it recovers `recovery_target` of the drop (0.5 = half). Ranked by ROI × liquidity.
- History builds up from the day you install it, so the digest gets useful after about a week.

**What gets checked how often**
| Tier | Which cards | Default interval |
|---|---|---|
| watch | Your `watchlist` | 2 min |
| hot | Worth ≥ 25k and ≥ 5 sales/day | 10 min |
| cold | Everything else | 24 h |

Cards at `min_ovr` (default 83) and up are discovered every 6 hours from mut.gg's player list (about 420 cards). Set `min_ovr: 0` to track everything.

**Poll budget (default 20 requests/min):** about 24,000 requests/day after 15% headroom. With ~420 cards at 83+, every card gets checked at least daily, and the top 168 by coins traded per day get checked every 10 minutes. Weaker hot cards drop to cold automatically. The agent honors `Retry-After` and backs off on refusals.

## How it gets prices

**Default: `fetch_mode: extension`.** mut.gg only serves price data to real browsers, so prices come from your own Chrome:

1. The **MUT Flip Feeder** extension (in `extension/`) keeps one pinned mut.gg tab open.
2. It asks the agent which cards are due (watchlist every 2 min, hot cards every 10 min, the rest daily).
3. That tab requests each card's prices exactly like mut.gg's own page does, including its "still updating" re-checks, at `requests_per_minute` (default 20).
4. Each result goes straight to the agent. A **live Buy Now listing** under your max-buy price triggers a Discord alert (with `@here`) within about a second.

If mut.gg refuses in your browser, the feeder pauses (backing off up to 15 min) and the agent shows **blocked**. Prices only flow while Chrome is running on that PC. The other mode, `direct`, uses plain HTTP from the server; mut.gg blocks it.

### Install the feeder on Windows (one line)

On the Windows PC or VM that will stay on, open **PowerShell** (no admin needed) and run:

```powershell
irm https://raw.githubusercontent.com/JacobBratcher/mut-flip-agent/main/extension/install.ps1 | iex
```

It asks for the agent URL (`http://<HA IP>:8099`) and the add-on's `feeder_token`, checks that it can reach the agent, and then:
- installs Chromium (regular Chrome no longer allows auto-loading a local extension),
- downloads and pre-configures the extension,
- adds a startup shortcut, turns off sleep while plugged in, and launches it.

Re-run it any time to update. Over RDP, **disconnect** when you leave, don't sign out.

Manual install instead: `chrome://extensions` → Developer mode → **Load unpacked** → `extension/` → Settings → enter the URL and token → Start.

**Tip:** in Discord, set the alert channel's notifications to *All Messages* so alerts buzz your phone instantly.

## Home Assistant sensors

Via MQTT discovery (Mosquitto add-on), under a **MUT Flip Agent** device:
`sensor.mut_flip_agent_status`, `_cards_tracked`, `_hot_cards`, `_checks_today`, `_flips_24h` (recent flips in the `flips` attribute), `_invest_picks` (picks in the `picks` attribute), `_last_price_update`.

## Install: Home Assistant add-on (recommended)

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add `https://github.com/JacobBratcher/mut-flip-agent`.
2. Install **MUT Flip Agent**.
3. **Configuration** tab: paste your Discord webhook URL (Discord channel → Edit → Integrations → Webhooks), and the token/header mut.gg gave you if any.
4. Start it. You'll get a "Started" message in Discord.

## Install: standalone Docker

```bash
cp .env.example .env              # add webhook + token
cp example-config.yaml config.yaml
docker compose up -d --build
```

## If mut.gg blocks requests

It never tries to bypass Cloudflare. It pauses (backing off up to 1h), keeps retrying, and posts a Discord warning at most every 6h. If that keeps happening, confirm your token/header, or ask mut.gg to allowlist your server's IP.

## Debugging

```bash
docker exec -it mut-flip-agent python -m app inspect 27-162004004   # raw price data for one card
cd mut-flip-agent && python -m pytest -q tests                      # tests
```
