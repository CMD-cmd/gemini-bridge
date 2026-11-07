"""Optional daemon-local, short-lived catalog cache with per-key single-flight.

Importing does not enable caching. The daemon must explicitly call enable().
Keys/catalogs stay in memory; metadata contains neither. A failed refresh removes
old data and delivers that failure to every waiter on that refresh.
"""
from datetime import datetime, timezone
import threading
import time

FRESHNESS_LIMIT_SECONDS = 15.0
_enabled = False
_lock = threading.Lock()
_values = {}
_flights = {}
_local = threading.local()


class _Flight:
    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.error = None
        self.observed_at = None
        self.observed_monotonic = None


def enable():
    """Enable caching for subsequent calls in this process; no external effects."""
    global _enabled
    with _lock:
        _enabled = True


def _meta(observed_at=None, observed_monotonic=None, hit=False):
    _local.observation = (observed_at, observed_monotonic, hit)


def metadata():
    """Describe this thread's most recent get; no catalog or cache key exposure.

    Before a successful get (and after a failed one), observation/age are None.
    Joining an in-progress fresh fetch is not reported as a cached-value hit.
    """
    observed_at, observed_monotonic, hit = getattr(_local, 'observation', (None, None, False))
    age = None if observed_monotonic is None else max(0.0, time.monotonic() - observed_monotonic)
    return {'observed_at': observed_at, 'age_seconds': age, 'cache_hit': hit,
            'freshness_limit_seconds': min(15.0, FRESHNESS_LIMIT_SECONDS)}


def _fetch(fetch):
    value = fetch()
    if not isinstance(value, str):
        raise TypeError('Catalog fetch must return a string')
    return value, datetime.now(timezone.utc).isoformat(), time.monotonic()


def get(key, fetch, force=False):
    """Return catalog text or propagate the current fetch error, never stale data.

    key must be hashable and include all caller-relevant account, executable and
    network identity. force bypasses saved values but joins an existing fetch.
    Disabled calls invoke fetch directly and have no cache/single-flight effects.
    """
    _meta()
    with _lock:
        enabled = _enabled
        if enabled:
            flight = _flights.get(key)
            owner = flight is None
            if owner:
                saved = _values.get(key)
                if not force and saved is not None and time.monotonic() - saved[2] < min(15.0, FRESHNESS_LIMIT_SECONDS):
                    _meta(saved[1], saved[2], True)
                    return saved[0]
                # Discard stale or force-bypassed data before starting a query.
                _values.pop(key, None)
                flight = _Flight()
                _flights[key] = flight
    if not enabled:
        value, observed_at, observed_monotonic = _fetch(fetch)
        _meta(observed_at, observed_monotonic)
        return value
    if owner:
        try:
            value, observed_at, observed_monotonic = _fetch(fetch)
        except BaseException as exc:
            with _lock:
                _values.pop(key, None)
                flight.error = exc
                _flights.pop(key, None)
                flight.done.set()
            raise
        with _lock:
            flight.value = value
            flight.observed_at = observed_at
            flight.observed_monotonic = observed_monotonic
            _values[key] = (value, observed_at, observed_monotonic)
            _flights.pop(key, None)
            flight.done.set()
    else:
        flight.done.wait()
        if flight.error is not None:
            raise flight.error
    _meta(flight.observed_at, flight.observed_monotonic)
    return flight.value
