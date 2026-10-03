# MUT Flip Agent

Watches Madden Ultimate Team prices on [mut.gg](https://www.mut.gg) (PC market) and posts a **🎯 Snipe** to Discord the moment a card is listed for Buy Now far enough under its resale value to flip for a profit after tax.

It also keeps you on top of the market as a whole:
- **📉 / 📈 Crash and bump alerts** (instant, `@here`) when the median card price moves `market.alert_pct` (default 8%), comparing each card's latest sales (last 6h) with the same card yesterday, so a crash that starts mid-day shows in full. One card can't trigger it.
- **Program alerts** when one program (Legends, Team of the Week, Team Builders…) moves `market.program_alert_pct` (15%), even if the overall market doesn't.
- **📊 Daily report** at `market.report_hour` (default 9 AM): market change over 24h and 7d, biggest drops and gains, new mut.gg promos, live Twitch drops, and yesterday's snipes.
- **🆕 New promos** posted as mut.gg publishes them (checked every 30 min).
- **📺 MUT YouTubers:** follows `youtube_channels`, by default [GutFoxx](https://www.youtube.com/@GutFoxx) (market strategy), [Popular Stranger](https://www.youtube.com/@iampopularstranger) (leaks and content schedules), [Moshi](https://www.youtube.com/@MoshiMadden) (daily updates and leaks) and [Swift](https://www.youtube.com/@SwiftMadden) (state of MUT, coin missions). Add any `@handle`. 💰 market/coin videos post instantly; 🔮 leak/upcoming-content videos post at most once per 8h per channel; everything else waits for the daily report.
- **What to do today**, learned from this season's own sales instead of old guides:
  - **Promo schedule:** release times read from mut.gg articles (backfilled 5 weeks on first start), so it warns the day before, e.g. "Tomorrow (Wed): Team of the Week ~10:48 AM".
  - **How promos actually move prices on PC:** for each release, how far existing cards moved in the first 12h and where they were the next day ("after the last 5 promos: -9% in 12h, -3% next day, bounced 4 of 5 times").
  - **Cheapest / priciest weekday**, once it has 2+ weeks of data.
  - The one outside rule kept (promos dip, then recover within a day or two) is from a current [MUT 27 auction house guide](https://timesaver.gg/blog/madden-nfl-27-auction-house-guide-make-coins-flipping). Sales history is kept 35 days for this.
- **🎁 Twitch drop reminders** (`@here`) when a Madden drop campaign goes live, from [twitchdrops.app](https://twitchdrops.app/game/madden-nfl-27) (checked every 3 h).

Optional, off by default: alerts on cheap *completed* sales (`flip.sale_alerts`; someone already bought those, so there's nothing to snipe) and a daily long-term investment digest (`invest.enabled`).

Runs 24/7 as a Home Assistant add-on or a plain Docker container. Data access is used with mut.gg's permission.

## How it decides

**Snipes**
1. Resale value is what you could realistically sell for *right now*, not a slow average (PC has little volume). It's the **lower** of:
   - the **median of the last 5 sales** (what buyers are actually paying), and
   - just under the **cheapest competing Buy Now listing**, which you'd have to undercut to sell, so the estimate follows listings down immediately.

   It's never above recent sales, so one optimistic listing can't make a flip look profitable. Each alert lists those last 5 sale prices, and also shows the profit if it only sells at **mut.gg's price** (the median of its last ~25 sales), so a short spike is easy to spot.
2. A card needs only **3 sales in the last 7 days** to be priced.
3. An alert fires only if, after the 10% auction tax, it clears **both** `min_profit` coins and `min_roi`, and it's at least `min_discount` under resale value.
4. Each alert shows the **max price to buy at**, **what to list it for**, and whether that resale price came from listings or sales.
5. A listing that's alerted and then sells doesn't alert again as a cheap sale.
6. **Only cards you can resell fast:** at least `min_sales_24h` (3) sales in the last 24h (mut.gg's own count).
7. **No falling knives:** skipped if the card's latest sales are down `max_drop` (15%) or more from the same time yesterday.
8. **Graded:** 🟢 **SAFE** = sells 8+/day, flat or rising, 15%+ ROI, and still clears `min_profit` at mut.gg's price. 🟡 **GOOD** = passes the rules above. Set `only_safe: true` to get only green ones.
9. **Confirm before alerting:** candidates must still qualify in a second completed snapshot at least 65 seconds later (within 180 seconds), with the same price and auction end within 15 seconds. The extra check uses the normal queue and request budget. No alert is sent if the auction disappears, expires, has under 30 seconds left, or its price matches one of the latest five completed sales. That last rule deliberately skips ambiguous same-price copies, including some valid listings. Entries carrying `soldDate` or `soldPrice` in the live collection are ignored. Pending confirmations reset after a server restart. This adds at least 65 seconds to alerts; it cannot guarantee the auction remains available afterward.
10. Alerts show the absolute scheduled auction end and the last check time. Historical Discord alerts remain historical snapshots, not current availability. Authenticated `/health` exposes pending/confirmed checks and ambiguous candidates skipped.

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
| new release | Cards that just came out: first 48 h (`fresh_hours`). `fresh_long_hours` (7 days) applies to cards whose name contains a word in `fresh_long_names` (LTD, Limited, Champion), but mut.gg card names usually don't include the program, so in practice most new cards get `fresh_hours` | 3 min |
| hot | Worth ≥ 25k and ≥ 5 sales/day | 10 min |
| cold | Everything else | 24 h |

Cards at `min_ovr` (default 85) and up are discovered from mut.gg's player list (about 300 cards at 85+) every 6 hours, and every 30 minutes for 3 hours after a promo is announced, because mistake listings way under value are most common right after a drop. New cards get a 🆕 Discord message and go straight into the new-release lane. With a huge drop, each new card is checked a little less often (they get up to 60% of the budget) so the rest of the market isn't starved. Set `min_ovr: 0` to track everything.

**Card scheduling budget (`requests_per_minute`, default 20):** the agent keeps 15% headroom, so 20/min is about 24,500 checks/day. The budget is shared in this order: watchlist, then new releases (capped at 60% of what's left), then cold cards (each once per `cold_hours`), and whatever remains decides how many cards fit in the hot tier (`hot cap`, logged every hour as `Poll plan`). If more cards qualify as hot than fit, the lowest-value ones drop to cold. To treat every card the same, set `hot_min_value: 0` and `hot_min_daily_sales: 0` so every card with recent sales qualifies.

**HTTP request budget (`http_requests_per_minute`, default 40):** this separate ceiling covers every price request, every `updating` re-check, discovery, name lookup, and MUT.GG news request made through one agent. It starts at 32 HTTP requests/minute (or the configured ceiling if lower), spaces requests evenly with no accumulated burst allowance, and raises the pace by 1/minute after at least 20 successful responses and 60 seconds. On 403/429/503 it halves the pace and pauses for 1–15 minutes, or longer when `Retry-After` says so. After a refusal, recovery stays at least 10% below the refused pace rather than repeatedly climbing back into it. This learned ceiling is conservative and can remain below the maximum a connection would later support. Both seconds and HTTP-date headers are supported. Rate, learned ceiling, pacing, and cooldown persist across agent/browser restarts. Concurrent refusals count as one slowdown incident.

These defaults are based on the project's earlier observation of roughly 30–40 HTTP requests/minute; they are not a published allowance or a guarantee against throttling. The scheduling budget is an estimate of completed card checks, not an HTTP limiter. Actual throughput depends on refresh retries and other traffic. Unrelated browsers, other agents, and the feeder page's own requests are outside this budget. Do not raise the HTTP ceiling beyond what MUT.GG permits.

## How it gets prices

**Default: `fetch_mode: extension`.** mut.gg only serves price data to real browsers, so prices come from your own Chrome:

1. The **MUT Flip Feeder** extension (in `extension/`, v1.3.0) keeps exactly one pinned mut.gg tab open (`mut.gg` or `www.mut.gg`) and closes any extras.
2. It asks the agent which cards are due (`GET /queue`), following the tiers above.
3. That tab requests each card's prices exactly like mut.gg's own page does (`/api/mutdb/prices/<id>/pc/`). If mut.gg's copy is older than ~60 s it refreshes from EA in about a second and reports `updating`; the feeder re-checks after 1.5 s. Up to 4 cards are in flight at once. Each worker leases its next card as soon as it finishes, so a slow card does not hold up a batch. Every HTTP attempt first obtains a shared request permit from the agent; retries use the same budget. A response still marked `updating` after the retries is not ingested or counted as a fresh check. The server also rejects these snapshots from older feeders before storing sales or sending alerts, and schedules the card for another check after 60 seconds. `/health` reports `refreshes_rejected` since startup. Even a finished snapshot reports MUT.GG's view of availability; an auction can sell before you open the game.
4. Each result goes straight to the agent (`POST /ingest`). A **live Buy Now listing** under your max-buy price triggers a Discord alert (with `@here`) within about a second.

**Self-recovery:** every request has a timeout (agent 15 s, mut.gg 20 s, tab messages 25 s). A mut.gg timeout reloads the tab and retries after 5 s without reporting a block. If the loop makes no progress for 5 minutes, the extension reloads itself. On the desktop, the keeper script also asks the agent's `GET /health` every minute and restarts the feeder browser if no prices have arrived for 10 minutes (not while mut.gg is blocking, and at most every 15 min); it logs to `%LOCALAPPDATA%\MUTFlipFeeder\watchdog.log`.

If mut.gg refuses in your browser (403/429), the feeder pauses (backing off from 1 up to 15 min, or longer for `Retry-After`) and the agent shows **blocked**. Note that a request that gets **no response at all** (your internet or DNS is down) is currently also reported as a refusal, with status 0, so a "mut.gg is refusing requests" message with `0` in it usually means a network outage. Prices only flow while the feeder PC is on and signed in. The other mode, `direct`, uses plain HTTP from the server; mut.gg blocks it.

**Feeder API** (port 8099, every call needs the `X-Feeder-Token` header): `GET /config`, `GET /queue?n=`, `POST /ingest`, `POST /status`, `GET /health` (seconds since the last price, feeder state, current HTTP pace, cooldown, and server-lifetime request/refusal counters), `POST /request-permit`, `POST /request-result`. The extension popup separates completed checks from browser HTTP attempts and shows the current shared pace.

**Upgrade together:** install agent 1.9.0 and feeder 1.3.0. Stop the old feeder while upgrading; it does not request permits and cannot participate in the shared budget. The new feeder refuses to start against an older agent. The agent upgrade alone does not change how an old extension sends requests.

### Install the feeder on Windows (one line)

On the Windows PC or VM that will stay on, open **PowerShell** (no admin needed) and run:

```powershell
irm https://raw.githubusercontent.com/JacobBratcher/mut-flip-agent/main/extension/install.ps1 | iex
```

It asks for the agent URL (`http://<HA IP>:8099`) and the add-on's `feeder_token`, checks that it can reach the agent, and then:
- installs Chromium (regular Chrome no longer allows auto-loading a local extension),
- downloads and pre-configures the extension,
- runs it **hidden** (not in the taskbar) and restarts it if it closes, starts it at every sign-in, and turns off sleep while plugged in.

It's a normal Chromium window, just hidden: headless Chrome identifies itself as HeadlessChrome and mut.gg blocks it. Double-click **MUT Flip Feeder** on the desktop to show the window, and again to hide it.

Files live in `%LOCALAPPDATA%\MUTFlipFeeder`: `extension\` (with `config.json` holding the agent URL and token), `profile\` (the Chromium profile), `feeder.ps1` / `feeder.vbs` (the keeper, started from the Startup folder), `toggle.ps1` / `toggle.vbs` (show/hide), and `watchdog.log`. The keeper uses VBScript (`wscript`) to start PowerShell with no window.

Re-run it any time to update (it remembers the URL and token). Over RDP, **disconnect** when you leave, don't sign out.

Manual install instead: `chrome://extensions` → Developer mode → **Load unpacked** → `extension/` → Settings → enter the URL and token → Start.

**Tip:** in Discord, set the alert channel's notifications to *All Messages* so alerts buzz your phone instantly.

## Current live setup (Oct 2, 2026)

What the author's instance actually runs, set in the add-on's Configuration tab (code defaults are described above):

| Setting | Live value | Default |
|---|---|---|
| Add-on / feeder | 1.8.1 / extension 1.2.0 | |
| `requests_per_minute` | 16 | 20 |
| `min_ovr` | 86 (~238 cards) | 85 |
| `tiers.hot_min_value` / `hot_min_daily_sales` | 0 / 0 (no hot filter: every card with sales qualifies) | 25,000 / 5 |
| `tiers.hot_minutes` | 20 | 10 |
| `tiers.cold_hours` | 1 | 24 |
| `tiers.fresh_minutes` | 2 | 3 |
| `tiers.fresh_hours` / `fresh_long_hours` | 168 / 336 | 48 / 168 |

With these, new releases get up to 60% of checks for their first week, and every other card is checked every 20 minutes when the budget allows (hourly otherwise). Home Assistant also has an automation that notifies the phone (and Discord, once its `rest_command` is added to `configuration.yaml`) when `sensor.mut_flip_agent_status` is `waiting for feeder` / `blocked` for 15 minutes, and a dashboard badge on the MUT Market button that counts snipes since it was last tapped (`input_datetime.mut_market_last_seen`, `script.mut_market_mark_seen`). Those live in Home Assistant, not in this repo.

## Home Assistant sensors

Via MQTT discovery (Mosquitto add-on), under a **MUT Flip Agent** device:
`sensor.mut_flip_agent_status`, `_cards_tracked`, `_hot_cards`, `_new_cards` (new releases being watched, in the `cards` attribute), `_checks_today`, `_flips_24h` (recent flips in the `flips` attribute), `_market_24h`, `_market_7d`, `_twitch_drop`, `_promos_24h`, `_latest_video`, `_last_price_update`.

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

It never tries to bypass Cloudflare or rate limits (no proxies or IP rotation). The feeder pauses (backing off up to 15 min, or longer for `Retry-After`) and retries, and Discord gets a warning when the state changes to blocked. If it keeps happening, lower `http_requests_per_minute`, or ask mut.gg for a higher supported limit. Changing a VPN connection does not clear the agent's cooldown.

## Debugging

```bash
docker exec -it mut-flip-agent python -m app inspect 27-162004004   # raw price data for one card
cd mut-flip-agent && python -m pytest -q tests                      # tests
```
