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


def test_operator_relearning_preserves_pacing_cooldown_and_backoff(tmp_path):
    clock = Clock()
    path = tmp_path / 'budget.json'
    budget = RequestBudget(120, path, clock)
    budget.acquire()
    budget.record(403, '7200')
    before = (budget.rate, budget.next_at, budget.blocked_until, budget.backoff)
    budget.relearn()
    budget = RequestBudget(120, path, clock)
    assert (budget.rate, budget.next_at, budget.blocked_until, budget.backoff) == before
    assert budget.target == 120
    assert budget.acquire()['wait_ms'] == 7200000
    clock.now += 7200
    assert budget.acquire()['allowed']
    assert not budget.acquire()['allowed']
    for _ in range(20):
        budget.record(200)
    assert budget.rate == before[0] + 1


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


def test_worker_pacing_is_additional_to_global_pacing_and_survives_restart(tmp_path):
    clock = Clock()
    path = tmp_path / 'workers.json'
    budget = RequestBudget(120, path, clock, worker_ceiling=16)
    assert budget.acquire('primary')['allowed']
    clock.now += 1.875
    assert not budget.acquire('primary')['allowed']
    assert budget.acquire('secondary')['allowed']
    restarted = RequestBudget(120, path, clock, worker_ceiling=16)
    clock.now += 1.875
    assert restarted.acquire('primary')['allowed']
    assert not restarted.acquire('secondary')['allowed']
    assert restarted.requests == 1


def test_six_workers_never_multiply_the_global_ceiling():
    clock = Clock()
    budget = RequestBudget(120, clock=clock, worker_ceiling=16)
    budget.rate = 120
    workers = ['primary', 'secondary'] + [f'worker{i}' for i in range(3, 7)]
    granted = {w: [] for w in workers}
    for _ in range(120):
        with ThreadPoolExecutor(6) as pool:
            permits = list(pool.map(budget.acquire, workers))
        assert sum(p['allowed'] for p in permits) <= 1
        for w, p in zip(workers, permits):
            if p['allowed']:
                granted[w].append(clock.now)
        clock.now += 0.5
    assert sum(map(len, granted.values())) <= 120
    for times in granted.values():
        assert 0 < len(times) <= 16
        assert all(b - a >= 3.75 for a, b in zip(times, times[1:]))


def test_repeated_timeouts_back_off_and_persist_without_claiming_http_refusals(tmp_path):
    clock = Clock()
    path = tmp_path / 'timeouts.json'
    budget = RequestBudget(120, path, clock)
    budget.record(0, outcome='timeout')
    assert budget.rate == 32 and budget.network_slowdowns == 0
    budget.record(200)  # a healthy sibling must not erase a failing session
    budget.record(0, outcome='network_error')
    budget.record(504)
    assert budget.rate == 24
    assert budget.target == pytest.approx(28.8)
    assert budget.network_slowdowns == 1 and budget.refusals == 0
    budget.record(0, outcome='timeout')  # in-flight failures cannot reduce twice
    assert budget.rate == 24
    restored = RequestBudget(120, path, clock)
    assert restored.acquire('worker3')['blocked']
    assert restored.acquire('worker3')['wait_ms'] == 60000
    assert restored.target == pytest.approx(28.8)
    clock.now += 60
    assert restored.acquire('worker3')['allowed']


def test_isolated_old_timeout_does_not_trigger_new_incident():
    clock = Clock()
    budget = RequestBudget(120, clock=clock)
    budget.record(0, outcome='timeout')
    clock.now += 61
    budget.record(0, outcome='timeout')
    budget.record(0, outcome='timeout')
    assert budget.network_slowdowns == 0
