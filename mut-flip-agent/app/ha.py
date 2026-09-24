"""Publishes agent status, flips and investment picks to Home Assistant via MQTT discovery."""
import json
import logging
import os

import paho.mqtt.client as mqtt
import requests

log = logging.getLogger(__name__)
PREFIX = "mut_flip_agent"
DEVICE = {"identifiers": [PREFIX], "name": "MUT Flip Agent", "manufacturer": "mut.gg",
          "model": "Price agent", "sw_version": os.environ.get("BUILD_VERSION", "")}

SENSORS = [
    # key, name, icon, extra
    ("status", "Status", "mdi:swap-horizontal-circle", {}),
    ("cards_tracked", "Cards tracked", "mdi:cards-outline", {"state_class": "measurement"}),
    ("hot_cards", "Hot cards", "mdi:fire", {"state_class": "measurement"}),
    ("checks_today", "Price checks today", "mdi:counter", {"state_class": "total_increasing"}),
    ("flips_24h", "Flips 24h", "mdi:cash-fast", {"state_class": "measurement",
                                                   "json_attributes_topic": f"{PREFIX}/flips"}),
    ("market_24h", "Market 24h", "mdi:chart-line-variant",
     {"state_class": "measurement", "unit_of_measurement": "%",
      "json_attributes_topic": f"{PREFIX}/market"}),
    ("market_7d", "Market 7d", "mdi:chart-timeline-variant",
     {"state_class": "measurement", "unit_of_measurement": "%"}),
    ("twitch_drop", "Twitch drop", "mdi:twitch", {"json_attributes_topic": f"{PREFIX}/drops"}),
    ("promos_24h", "New on mut.gg 24h", "mdi:newspaper-variant-outline",
     {"state_class": "measurement", "json_attributes_topic": f"{PREFIX}/news"}),
    ("last_price_update", "Last price update", "mdi:clock-check-outline", {"device_class": "timestamp"}),
]


RETIRED = ["invest_picks"]


def _broker():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        try:
            r = requests.get("http://supervisor/services/mqtt",
                             headers={"Authorization": f"Bearer {token}"}, timeout=10)
            r.raise_for_status()
            return r.json()["data"]
        except Exception as e:
            log.warning("MQTT service lookup failed: %s", e)
            return None
    if os.environ.get("MQTT_HOST"):
        return {"host": os.environ["MQTT_HOST"], "port": int(os.environ.get("MQTT_PORT", 1883)),
                "username": os.environ.get("MQTT_USER"), "password": os.environ.get("MQTT_PASS")}
    return None


class HA:
    def __init__(self):
        self.client = None
        b = _broker()
        if not b:
            log.info("MQTT not configured; Home Assistant sensors disabled")
            return
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=PREFIX)
        if b.get("username"):
            c.username_pw_set(b["username"], b.get("password"))
        c.will_set(f"{PREFIX}/availability", "offline", retain=True)
        c.on_connect = self._on_connect
        c.connect_async(b["host"], int(b.get("port", 1883)))
        c.loop_start()
        self.client = c

    def _on_connect(self, client, userdata, flags, rc, props=None):
        log.info("MQTT connected (%s)", rc)
        for key, name, icon, extra in SENSORS:
            cfg = {
                "name": name, "unique_id": f"{PREFIX}_{key}",
                "default_entity_id": f"sensor.{PREFIX}_{key}",
                "state_topic": f"{PREFIX}/state",
                "value_template": "{{ value_json.%s }}" % key,
                "availability_topic": f"{PREFIX}/availability",
                "icon": icon, "device": DEVICE, **extra,
            }
            client.publish(f"homeassistant/sensor/{PREFIX}/{key}/config", json.dumps(cfg), retain=True)
        for gone in RETIRED:     # an empty retained config removes the entity from HA
            client.publish(f"homeassistant/sensor/{PREFIX}/{gone}/config", "", retain=True)
        client.publish(f"{PREFIX}/availability", "online", retain=True)

    def publish(self, state: dict, flips: list, picks: list, extra: dict | None = None):
        if not self.client:
            return
        for topic, payload in (extra or {}).items():
            self.client.publish(f"{PREFIX}/{topic}", json.dumps(payload), retain=True)
        self.client.publish(f"{PREFIX}/state", json.dumps(state), retain=True)
        self.client.publish(f"{PREFIX}/flips", json.dumps({"flips": flips}), retain=True)
        self.client.publish(f"{PREFIX}/picks", json.dumps({"picks": picks}), retain=True)
