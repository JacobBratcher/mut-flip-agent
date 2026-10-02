from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate

import pytest

from app.rate_limit import RequestBudget, retry_seconds


class Clock:
    def __init__(self):
        self.now = 1_800_000_000

    def __call__(self):
        return self.now


def test_concurrent_callers_share_one_slot_without_idle_bursts():
    clock = Clock()
    budget = RequestBudget(40, clock=clock)
    for _ in range(2):
        with ThreadPoolExecutor(8) as pool:
            permits = list(pool.map(lambda _: budget.acquire(), range(8)))
        assert sum(p["allowed"] for p in permits) == 1
        assert budget.acquire()["wait_ms"] == 1875
        clock.now += 3600


@pytest.mark.parametrize("header", ["7200", formatdate(1_800_007_200, usegmt=True)])
def test_long_retry_after_survives_restart_and_inflight_success(tmp_path, header):
    clock = Clock()
    path = tmp_path / "budget.json"
    budget = RequestBudget(40, path, clock)
    budget.acquire()
    budget.record(429, header)
    budget.record(200)  # another request was already in flight
    budget.record(429)  # same incident must not halve the rate twice
    restarted = RequestBudget(40, path, clock)
    assert restarted.rate == 16
    assert restarted.acquire() == {"allowed": False, "blocked": True, "wait_ms": 7200000, "rpm": 16}
    clock.now += 7200
    assert restarted.acquire()["allowed"]
    restarted.record(429)
    assert restarted.rate == 8
    assert restarted.acquire()["wait_ms"] == 120000


def test_recovery_requires_time_and_successes_and_never_exceeds_ceiling():
    clock = Clock()
    budget = RequestBudget(34, clock=clock)
    for _ in range(100):
        budget.record(200)
    assert budget.rate == 32  # no fast burst can increase the rate
    clock.now += 60
    budget.record(200)
    assert budget.rate == 33
    clock.now += 60
    for _ in range(20):
        budget.record(200)
    assert budget.rate == 34
    clock.now += 60
    for _ in range(20):
        budget.record(200)
    assert budget.rate == 34
    budget.record(429)
    assert budget.rate == 17
    clock.now += 60
    budget.record(200)
    assert budget.rate == 17


def test_persisted_spacing_prevents_a_restart_burst(tmp_path):
    clock = Clock()
    path = tmp_path / "budget.json"
    assert RequestBudget(40, path, clock).acquire()["allowed"]
    assert not RequestBudget(40, path, clock).acquire()["allowed"]


def test_raise_temporary_ceiling_without_resetting_pace_or_spacing(tmp_path):
    clock = Clock()
    path = tmp_path / "budget.json"
    RequestBudget(16, path, clock).acquire()
    budget = RequestBudget(40, path, clock)
    assert budget.rate == 16
    assert budget.target == 40
    assert not budget.acquire()["allowed"]
    clock.now += 60
    for _ in range(20):
        budget.record(200)
    assert budget.rate == 17


def test_raising_configuration_keeps_refusal_limit_and_cooldown(tmp_path):
    clock = Clock()
    path = tmp_path / "budget.json"
    budget = RequestBudget(16, path, clock)
    budget.record(429, "7200")
    budget = RequestBudget(40, path, clock)
    assert budget.rate == 8
    assert budget.target == pytest.approx(14.4)
    assert budget.acquire()["wait_ms"] == 7200000


def test_recovery_stays_below_a_previously_refused_rate_after_restart(tmp_path):
    clock = Clock()
    path = tmp_path / "budget.json"
    budget = RequestBudget(40, path, clock)
    budget.record(429)
    budget = RequestBudget(40, path, clock)
    for _ in range(60):
        clock.now += 60
        for _ in range(20):
            budget.record(200)
    assert budget.rate == pytest.approx(28.8)
    assert budget.snapshot()["learned_ceiling"] == pytest.approx(28.8)


@pytest.mark.parametrize("value", [None, "bad", "NaN", "inf", "-1", [], {}])
def test_invalid_retry_headers(value):
    assert retry_seconds(value, 1_800_000_000) == 0


def test_discovery_uses_browser_budget(monkeypatch):
    from app.client import Blocked, MutGG
    from app.config import DEFAULTS
    clock = Clock()
    budget = RequestBudget(40, clock=clock)
    client = MutGG(DEFAULTS, budget)
    budget.record(429, "600")
    monkeypatch.setattr(client.s, "get", lambda *a, **kw: pytest.fail("request during cooldown"))
    with pytest.raises(Blocked) as error:
        client.discover(85)
    assert error.value.retry_after == 600


def test_discovery_refusal_stops_browser_requests(monkeypatch):
    from app.client import Blocked, MutGG
    from app.config import DEFAULTS
    from types import SimpleNamespace
    clock = Clock()
    budget = RequestBudget(40, clock=clock)
    client = MutGG(DEFAULTS, budget)
    response = SimpleNamespace(status_code=429, text="blocked", headers={"Retry-After": "7200"})
    monkeypatch.setattr(client.s, "get", lambda *a, **kw: response)
    with pytest.raises(Blocked):
        client.get("https://www.mut.gg/sitemap-news.xml")
    assert budget.acquire()["blocked"]
    assert budget.snapshot()["cooldown_seconds"] == 7200
