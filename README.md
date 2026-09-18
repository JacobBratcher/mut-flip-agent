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

All cards are auto-discovered daily from mut.gg's sitemap.

**Poll budget (default 10 requests/min):** about 12,000 requests/day after 15% headroom. With 10 watchlist cards and ~4,000 cards total, that covers watch (2,880/day) and a daily pass on every card (~4,000/day). The rest goes to the top 75 hot cards, ranked by coins traded per day; weaker hot cards drop to cold automatically. The first full pass over the market takes about 7 hours. The plan is logged hourly, and you get a Discord warning if your settings go over budget. Only raise `requests_per_minute` if mut.gg approved a higher rate. The agent also honors their `Retry-After` header.

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
