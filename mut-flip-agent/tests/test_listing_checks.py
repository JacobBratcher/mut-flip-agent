from types import SimpleNamespace

import pytest

from app.listing_checks import ListingChecks


def listing(price=111_500, ends=2000):
    return SimpleNamespace(bin_price=price, ends=ends)


def test_burrow_completed_sale_overlap_never_confirms():
    checks = ListingChecks()
    recent = [140_000, 125_500, 143_000, 111_500, 145_500]
    assert checks.check("burrow", listing(), recent, 1000) == (False, None)
    assert checks.check("burrow", listing(), recent, 1066) == (False, None)
    assert checks.confirmed == 0 and not checks.pending


def test_immediate_duplicate_is_not_confirmation():
    checks = ListingChecks()
    assert checks.check("x", listing(), [], 1000) == (False, 1065)
    assert checks.check("x", listing(), [], 1001) == (False, 1065)
    assert checks.check("x", listing(), [], 1065) == (True, None)


@pytest.mark.parametrize("second,recent", [
    (None, []),  # bought/removed before verification
    (listing(ends=1050), []),  # expired before verification
    (listing(), [111_500]),  # completed sale appeared during verification
])
def test_unavailable_candidate_never_alerts(second, recent):
    checks = ListingChecks()
    checks.check("x", listing(), [], 1000)
    assert checks.check("x", second, recent, 1066) == (False, None)
    assert checks.confirmed == 0 and not checks.pending


@pytest.mark.parametrize("changed", [listing(price=110_000), listing(ends=2600)])
def test_different_auction_requires_new_verification(changed):
    checks = ListingChecks()
    checks.check("x", listing(), [], 1000)
    assert checks.check("x", changed, [], 1066) == (False, 1131)


def test_old_candidate_and_restarted_agent_cannot_confirm():
    checks = ListingChecks()
    checks.check("x", listing(), [], 1000)
    assert checks.check("x", listing(), [], 1181) == (False, 1246)
    assert ListingChecks().check("x", listing(), [], 1181) == (False, 1246)


def test_minor_timestamp_rounding_still_confirms():
    checks = ListingChecks()
    checks.check("x", listing(), [], 1000)
    assert checks.check("x", listing(ends=2001), [], 1066) == (True, None)
