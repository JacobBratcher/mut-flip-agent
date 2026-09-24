"""Loads settings from HA add-on options (/data/options.json) or a standalone config.yaml."""
import copy
import json
import os
from pathlib import Path

import yaml

DEFAULTS = {
    "discord_webhook_url": "",
    "platform": "pc",
    "game": "27",
    "fetch_mode": "extension",
    "feeder_token": "",
    "feeder_port": 8099,
    "api_token": "",
    "api_token_header": "Authorization",
    "requests_per_minute": 20,
    "watchlist": [],
    "discover_all_players": True,
    "min_ovr": 83,
    "flip": {
        "min_profit": 10000, "min_roi": 0.08, "min_discount": 0.12,
        "lookback_hours": 168, "min_sales": 3, "alert_cooldown_hours": 6, "sale_alerts": False,
        "max_buy_budget": 0,
    },
    "invest": {
        "enabled": False, "digest_hour": 9, "window_days": 30,
        "min_days_history": 5, "min_drawdown": 0.25, "min_daily_sales": 3,
        "recovery_target": 0.5, "top_n": 10,
    },
    "tiers": {
        "watch_minutes": 2, "hot_minutes": 10, "cold_hours": 24,
        "hot_min_value": 25000, "hot_min_daily_sales": 5,
    },
    "tax_rate": 0.10,
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def data_dir() -> Path:
    d = Path(os.environ.get("DATA_DIR", "/data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def load() -> dict:
    options = data_dir() / "options.json"          # written by Home Assistant
    standalone = Path(os.environ.get("CONFIG_PATH", "/config/config.yaml"))
    if options.exists():
        user = json.loads(options.read_text())
    elif standalone.exists():
        user = yaml.safe_load(standalone.read_text()) or {}
    else:
        user = {}
    cfg = _merge(DEFAULTS, user)
    # Environment variables win for secrets (standalone Docker).
    cfg["discord_webhook_url"] = os.environ.get("DISCORD_WEBHOOK_URL", cfg["discord_webhook_url"])
    cfg["api_token"] = os.environ.get("MUTGG_API_TOKEN", cfg["api_token"])
    if not cfg["discord_webhook_url"]:
        raise SystemExit("discord_webhook_url is not set")
    return cfg
