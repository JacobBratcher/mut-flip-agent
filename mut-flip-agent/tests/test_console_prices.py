from copy import deepcopy
import pytest
from app.config import DEFAULTS
from app.console_prices import ConsolePrices, price
from app.db import DB
from app import main, feeder
from tests.test_feeder import call

NOW = 1_800_000_000


def setup(tmp_path, count=1):
    db = DB(tmp_path / 'mut.db')
    for i in range(1, count + 1):
        db.upsert_item(f'27-{i}')
    cfg = deepcopy(DEFAULTS)
    cfg['console_comparison']['enabled'] = True
    return db, ConsolePrices(db, cfg)


def record(external_id=1, pc='500,000', xbox='300K', ps5='1M'):
    return {'externalId': external_id, 'priceDisplay': {'pc': pc, 'xbox-series-x': xbox, 'playstation-5': ps5}}


@pytest.mark.parametrize('value', [None, True, 0, -1, float('inf'), float('nan'), '--', 'BND', '5,00', '1e6', {}, '999999999999'])
def test_unpriced_or_malformed_is_not_a_deal(value):
    assert price(value) is None


def test_comparison_both_directions_and_console_identity(tmp_path):
    db, prices = setup(tmp_path)
    before = dict(db.item('27-1'))
    lease = prices.lease(NOW)
    assert prices.ingest({'lease_id': lease['lease_id'], 'records': [record()]}, NOW + 1) == 1
    report = prices.report(NOW + 2)
    assert report['fresh_cards'] == 1
    assert [(g['console'], g['coin_gap'], g['pc_vs_console_pct']) for g in report['gaps']] == [
        ('playstation-5', -500000, -50.0), ('xbox-series-x', 200000, 66.7)]
    assert prices.context('27-1', NOW + 2)['age_seconds'] == 1
    assert dict(db.item('27-1')) == before  # Preview never advances PC scan or scheduling timestamps.
    assert prices.report(NOW + 1802)['gaps'] == []
    assert prices.context('27-1', NOW + 1802) is None


def test_thresholds_missing_and_disabled(tmp_path):
    _, prices = setup(tmp_path)
    for i, rec in enumerate([record(pc=100000, xbox=60000, ps5=120000), record(pc=None), record(xbox=None, ps5=None)]):
        lease = prices.lease(NOW + i * 700)
        prices.ingest({'lease_id': lease['lease_id'], 'records': [rec]}, NOW + i * 700)
        assert prices.report(NOW + i * 700)['gaps'] == []
    prices.cfg['console_comparison']['enabled'] = False
    assert prices.lease(NOW + 3000) == {'external_ids': []}
    assert prices.context('27-1', NOW) is None


def test_global_batch_throttle_leases_persist_and_failed_work_recovers(tmp_path):
    db, prices = setup(tmp_path, 45)
    first = prices.lease(NOW)
    assert len(first['external_ids']) == 20
    assert prices.lease(NOW + 59)['external_ids'] == []
    prices = ConsolePrices(db, prices.cfg)  # restart does not reset gate or leases
    second = prices.lease(NOW + 60)
    assert not set(first['external_ids']) & set(second['external_ids'])
    assert len(prices.lease(NOW + 181)['external_ids']) == 20
    with pytest.raises(ValueError, match='expired'):
        prices.ingest({'lease_id': first['lease_id'], 'records': []}, NOW + 181)


def test_validation_atomic_and_omissions_clear_stale_prices(tmp_path):
    _, prices = setup(tmp_path, 2)
    lease = prices.lease(NOW)
    for records in [[record(3)], [record(), record()], [record(), {'externalId': 2, 'priceDisplay': []}], [record()] * 21]:
        with pytest.raises(ValueError):
            prices.ingest({'lease_id': lease['lease_id'], 'records': records}, NOW + 1)
        assert prices.context('27-1', NOW + 2) is None
    prices.ingest({'lease_id': lease['lease_id'], 'records': [record()]}, NOW + 1)
    assert prices.report(NOW + 2)['gap_count'] == 2
    lease = prices.lease(NOW + 602)
    prices.ingest({'lease_id': lease['lease_id'], 'records': []}, NOW + 603)
    assert prices.report(NOW + 604)['gap_count'] == 0
    assert prices.context('27-1', NOW + 604) is None


def test_feeder_preview_routes_are_authenticated_and_separate_from_scans(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    cfg = deepcopy(DEFAULTS)
    cfg.update(discord_webhook_url='x', feeder_token='secret')
    cfg['console_comparison']['enabled'] = True
    agent = main.Agent(cfg)
    agent.db.upsert_item('27-1')
    httpd = feeder.serve(agent, 0)
    port = httpd.server_address[1]
    try:
        for route in ['/preview-queue', '/console-report']:
            assert call(port, 'GET', route, token='wrong')[0] == 401
        assert call(port, 'POST', '/preview-ingest', {}, token='wrong')[0] == 401
        assert call(port, 'GET', '/config')[1]['console_preview_version'] == 1
        lease = call(port, 'GET', '/preview-queue')[1]
        payload = {'lease_id': lease['lease_id'], 'records': [record()]}
        assert call(port, 'POST', '/preview-ingest', payload)[1] == {'ok': True, 'items': 1}
        assert call(port, 'POST', '/preview-ingest', payload)[0] == 400  # no replay
        assert call(port, 'GET', '/console-report')[1]['gap_count'] == 2
        assert agent.last_ingest == 0
        assert agent.checks_today == 0
        assert len(call(port, 'GET', '/queue?n=1')[1]['items']) == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_alert_includes_preview_context_without_changing_pc_estimate(monkeypatch):
    from types import SimpleNamespace
    from app.notify import Discord
    messages = []
    discord = Discord('unused')
    monkeypatch.setattr(discord, 'send', lambda embeds, **kw: messages.extend(embeds))
    deal = SimpleNamespace(max_buy=300000, bin_price=290000, ends=NOW + 100,
                           market=500000, basis='sales', profit=160000, roi=.55, falling=False)
    discord.listing('Card', '', deal, 'pc', console_preview={
        'pc': 500000, 'xbox': 1000000, 'ps5': 200000, 'age_seconds': 120})
    assert 'Sell at **500,000**' in messages[0]['description']
    context = next(f for f in messages[0]['fields'] if 'Preview' in f['name'])
    assert 'Xbox Series X: 1,000,000' in context['value']
    assert 'PS5: 200,000' in context['value']
    assert 'source sale age unknown' in context['value']
