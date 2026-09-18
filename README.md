# MUT Flip Agent

Watches Madden Ultimate Team prices on [mut.gg](https://www.mut.gg) (PC market) and posts to Discord when:

- **💸 Flip:** a card sells well under its market value, so there are likely cheap listings to snipe and resell.
- **📈 Investment (daily digest):** a liquid card has dropped hard from its recent high and has stopped falling, so it's a buy-and-hold candidate.

Runs 24/7 as a Home Assistant add-on or a plain Docker container. Data access is used with mut.gg's permission.

## How it decides

**Flips**
1. Market value = median sale price over the last 48h. If the last 12h are clearly lower, it uses that instead so a falling card doesn't look like a bargain.
2. A new sale triggers an alert only if, after the 10% auction tax, it clears **both** `min_profit` coins and `min_roi`, and it's at least `min_discount` under market.
3. The alert tells you the **max price to buy at** and **what to list it for**.
4. One alert per card per `alert_cooldown_hours`.

Note: mut.gg publishes *completed* sales, not live listings. An alert means "this card is trading under value right now," so move fast.

**Investments** (sent once a day at `digest_hour`)
- At least `min_days_history` days of data and `min_daily_sales` sales/day (so you can actually sell later).
- Down at least `min_drawdown` from its high in the window.
- Not still dropping (yesterday wasn't more than 3% below the day before).
- Sell target assumes it recovers `recovery_target` of the drop (0.5 = half). Ranked by ROI × liquidity.
- History builds up from the day you install it, so the digest gets useful after about a week.

**What gets checked how often**
| Tier | Which cards | Default interval |
|---|---|---|
| watch | Your `watchlist` | 5 min |
| hot | Worth ≥ 25k and ≥ 5 sales/day | 20 min |
| cold | Everything else | 24 h |

Cards at `min_ovr` (default 83) and up are discovered every 6 hours from mut.gg's player list (about 420 cards). Set `min_ovr: 0` to track everything.

**Poll budget (default 10 requests/min):** about 12,000 requests/day after 15% headroom. With ~420 cards at 83+, every card gets checked at least daily, and the top 166 by coins traded per day get checked every 20 minutes. Weaker hot cards drop to cold automatically. The plan is logged hourly, and you get a Discord warning if your settings go over budget. Only raise `requests_per_minute` if mut.gg approved a higher rate. The agent also honors their `Retry-After` header.

## How it gets prices

By default (`fetch_mode: browser`) it runs a headless Chromium via Playwright, loads mut.gg like a visitor, and reads prices the same way the site's own page does, with mut.gg's permission. There's no fingerprint spoofing. If Cloudflare still refuses, it backs off and tells you. `fetch_mode: direct` uses plain HTTP instead.

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
cp config.example.yaml config.yaml
docker compose up -d --build
```

## If mut.gg blocks requests

It never tries to bypass Cloudflare. It pauses (backing off up to 1h), keeps retrying, and posts a Discord warning at most every 6h. If that keeps happening, confirm your token/header, or ask mut.gg to allowlist your server's IP.

## Debugging

```bash
docker exec -it mut-flip-agent python -m app inspect 27-162004004   # raw price data for one card
cd mut-flip-agent && python -m pytest -q tests                      # tests
```
