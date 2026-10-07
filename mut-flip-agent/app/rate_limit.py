"""One paced HTTP budget shared by discovery and every browser price request."""
import json
import math
import threading
import time
from email.utils import parsedate_to_datetime


def retry_seconds(value, now=None):
    """Retry-After accepts either seconds or an HTTP date."""
    now = time.time() if now is None else now
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0


class RequestBudget:
    def __init__(self, ceiling, path=None, clock=time.time, worker_ceiling=16):
        self.ceiling = max(1, float(ceiling))
        self.target = self.ceiling
        self.floor = 1
        self.clock, self.path = clock, path
        self.lock = threading.Lock()
        self.worker_ceiling = max(1, float(worker_ceiling))
        self.worker_next = {}
        self.transient_failures = []
        self.network_slowdowns = 0
        self.rate = min(32, self.ceiling)
        self.next_at = self.blocked_until = self.backoff = 0
        self.changed_at = clock()
        self.successes = self.requests = self.refusals = 0
        if path and path.exists():
            saved = json.loads(path.read_text())
            for key in ("rate", "target", "next_at", "blocked_until", "backoff", "changed_at"):
                value = float(saved.get(key, self.ceiling) if key == "target" else saved[key])
                if not math.isfinite(value):
                    raise ValueError(f"Invalid persisted request budget: {key}")
                setattr(self, key, value)
            for key, value in saved.get("worker_next", {}).items():
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError("Invalid persisted worker pacing")
                if value > clock():
                    self.worker_next[key] = value
            # Before any refusal, target is only the old configured ceiling, not
            # a learned limit. Allow a raised configuration to take effect while
            # retaining the current pace and its gradual recovery.
            if self.blocked_until == 0:
                self.target = self.ceiling
            self.target = min(self.ceiling, max(self.floor, self.target))
            self.rate = min(self.target, max(self.floor, self.rate))

    def _save(self):
        if self.path:
            saved = {k: getattr(self, k) for k in
                     ("rate", "target", "next_at", "blocked_until", "backoff", "changed_at")}
            saved["worker_next"] = self.worker_next
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(saved))
            temp.replace(self.path)

    def acquire(self, worker_id=None):
        """Nonblocking: callers wait and ask again; no burst tokens accumulate."""
        with self.lock:
            now = self.clock()
            self.worker_next = {k: v for k, v in self.worker_next.items() if v > now}
            wait = max(self.next_at, self.blocked_until,
                       self.worker_next.get(worker_id, 0)) - now
            result = {"allowed": wait <= 0, "wait_ms": max(0, math.ceil(wait * 1000)),
                      "blocked": self.blocked_until > now, "rpm": self.rate}
            if wait <= 0:
                self.next_at = now + 60 / self.rate
                if worker_id is not None:
                    self.worker_next[worker_id] = now + 60 / self.worker_ceiling
                self.requests += 1
                self._save()
            return result

    def record(self, status, retry_after=None, outcome="http"):
        with self.lock:
            now = self.clock()
            if status in (403, 429, 503):
                self.refusals += 1
                # Concurrent refusals belong to the same incident.
                if now >= self.blocked_until:
                    # Keep 10% below a refused pace instead of repeatedly climbing
                    # back into the same limit. This learned target also persists.
                    self.target = max(self.floor, min(self.target, self.rate * 0.9))
                    self.rate = max(self.floor, self.rate / 2)
                    self.backoff = min(900, max(60, self.backoff * 2))
                    self.blocked_until = now + self.backoff
                self.blocked_until = max(self.blocked_until, now + retry_seconds(retry_after, now))
                self.successes = 0
                self.changed_at = now
                self._save()
            elif outcome in ("timeout", "network_error") or status in (0, 500, 502, 504):
                # A single slow request need not stop healthy workers. Repeated
                # failures must not let unattended traffic keep ramping upward.
                self.successes = 0
                self.changed_at = now
                self.transient_failures = [t for t in self.transient_failures if t >= now - 60]
                self.transient_failures.append(now)
                if len(self.transient_failures) >= 3 and now >= self.blocked_until:
                    self.network_slowdowns += 1
                    self.target = max(self.floor, min(self.target, self.rate * 0.9))
                    self.rate = max(self.floor, self.rate * 0.75)
                    self.blocked_until = now + 60
                    self.transient_failures.clear()
                self._save()
            elif 200 <= status < 300 and now >= self.blocked_until:
                self.successes += 1
                # Recover slowly, only with sustained successful traffic.
                if self.successes >= 20 and now - self.changed_at >= 60:
                    self.rate = min(self.target, self.rate + 1)
                    self.successes = 0
                    self.changed_at = now
                    self._save()
                if self.rate == self.target:
                    self.backoff = 0
            return {"wait_ms": max(0, math.ceil((self.blocked_until - now) * 1000)),
                    "rpm": self.rate}

    def relearn(self):
        """Operator recovery after a repair; never reset pace, spacing or cooldown."""
        with self.lock:
            self.target = self.ceiling
            self.successes = 0
            self.changed_at = self.clock()
            self._save()

    def snapshot(self):
        with self.lock:
            return {"rpm": self.rate, "ceiling": self.ceiling, "learned_ceiling": self.target,
                    "requests": self.requests,
                    "refusals": self.refusals,
                    "worker_ceiling": self.worker_ceiling,
                    "network_slowdowns": self.network_slowdowns,
                    "cooldown_seconds": max(0, math.ceil(self.blocked_until - self.clock()))}
