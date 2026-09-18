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
