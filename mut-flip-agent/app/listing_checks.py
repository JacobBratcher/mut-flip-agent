"""Conservative confirmation of opportunities across separate price snapshots."""
from dataclasses import dataclass

# Wait beyond the source's approximately one-minute refresh interval. Immediate
# retries can return the same cached auction list and aren't confirmation.
VERIFY_AFTER = 65
VERIFY_EXPIRES = 180
END_TOLERANCE = 15


@dataclass
class Candidate:
    price: int
    ends: float
    seen: float


class ListingChecks:
    def __init__(self):
        self.pending = {}
        self.confirmed = 0
        self.ambiguous = 0

    def check(self, uid, deal, recent_sales, now):
        """Return (confirmed, next_verification_time); no snapshot means no alert.

        Prices alone cannot identify auctions. When the candidate's price also
        occurs in the last five completed sales, skip it rather than presenting
        ambiguous availability as a buy opportunity. This can hide valid copies.
        """
        for key, candidate in list(self.pending.items()):
            if now - candidate.seen > VERIFY_EXPIRES or candidate.ends <= now:
                self.pending.pop(key, None)
        if deal is None or deal.ends <= now + 30:
            self.pending.pop(uid, None)
            return False, None
        if deal.bin_price in recent_sales:
            self.pending.pop(uid, None)
            self.ambiguous += 1
            return False, None
        previous = self.pending.get(uid)
        if (previous and previous.price == deal.bin_price
                and abs(previous.ends - deal.ends) <= END_TOLERANCE):
            if now >= previous.seen + VERIFY_AFTER:
                self.pending.pop(uid, None)
                self.confirmed += 1
                return True, None
            return False, previous.seen + VERIFY_AFTER
        # Don't schedule a check after the auction's remaining useful lifetime.
        if deal.ends <= now + VERIFY_AFTER + 30:
            self.pending.pop(uid, None)
            return False, None
        self.pending[uid] = Candidate(deal.bin_price, deal.ends, now)
        return False, now + VERIFY_AFTER
