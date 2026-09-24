from app.client import normalize_watch, parse_sales


def test_normalize():
    assert normalize_watch("https://www.mut.gg/players/12562-tj-watt/27-162004004/")[0] == "27-162004004"
    assert normalize_watch("27-162004004") == ("27-162004004", "")
    assert normalize_watch("tj watt") is None


def test_parse_sales_real_shape():
    data = {"externalId": 162004004, "pricesData": {"completedAuctions": [
        {"soldPrice": 440000, "soldDate": "2026-09-18T02:23:49.034133+00:00"},
        {"soldPrice": None, "soldDate": "x"}]}}
    assert parse_sales(data) == [(440000, "2026-09-18T02:23:49.034133+00:00")]


def test_parse_player_list():
    from app.client import parse_player_list
    html = open("/tmp/f.html").read() if __import__("os").path.exists("/tmp/f.html") else ""
    page = ('<div class="player-list-item"><a href="/players/27227-reggie-bush/27-109027227/" '
            'class="player-list-item__link"><div>OVR</div><div>89</div><div>Reggie</div><div>Bush</div>'
            '<div>SPD</div><div>89</div></a></div>')
    assert parse_player_list(page) == [
        ("27-109027227", "https://www.mut.gg/players/27227-reggie-bush/27-109027227/", 89, "Reggie Bush 89 OVR")]
    if html:
        items = parse_player_list(html)
        assert len(items) == 15 and all(o >= 83 and n for _, _, o, n in items)


def test_parse_volume():
    from app.client import parse_volume
    assert parse_volume({"pricesData": {"volume": {"day": {"sales": 13}}}}) == 13
    assert parse_volume({"pricesData": {}}) is None


def test_parse_price():
    from app.client import parse_price
    assert parse_price({"pricesData": {"summary": {"price": 135100}}}) == 135100
    assert parse_price({"pricesData": {"summary": {}}}) is None


def test_listing_message_shows_both_prices(monkeypatch):
    import time
    from app import notify
    from app.analysis import Listing
    sent = []
    d = notify.Discord("x")
    monkeypatch.setattr(d, "send", lambda embeds=None, content=None: sent.append(embeds[0]))
    d.listing("Xavier Watts", "", Listing(125_100, time.time() + 3000, 158_100, 131_750, 17_190,
              0.137, 0.2, False, "sales", "good", 14, 0.137, [159_000, 100_000], 135_100, -3_510),
              "pc")
    text = sent[0]["description"]
    assert "Sell at **158,100** (last 5 sales) → **+17,190**" in text
    assert "Sell at **135,100** (mut.gg price) → **−3,510**" in text
