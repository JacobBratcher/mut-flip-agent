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
