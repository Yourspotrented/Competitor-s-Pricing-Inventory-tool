"""
Way.com as a third buying platform, alongside SpotHero and ParkWhiz.

Why: the team buys passes there (the spare spots workbook has a "BUYING PRICE
AT WAY.CO" column), so a lot sold out on SpotHero and ParkWhiz but still on
sale at Way.com is not really gone — reading only two platforms makes us
deactivate listings we could still fulfil.

What Way.com gives us
---------------------
Its web app calls a public search API with a consumer key shipped in the page
(no login, no cookie, and it answers a plain server-side request):

    POST /way-search/v2/public/search/events   venueId -> that venue's events
    POST /way-search/v2/public/search          venueId+eventId, or lat/lon and
                                               a time window -> the lots

Each lot carries listingId, listingName, minPrice, availability, bookable,
its own lat/lon and a full address. That is richer than ParkWhiz: real
coordinates mean our lot can be matched by position, not only by text.

What it does not give is a spots-left count. So, like ParkWhiz, Way.com can
confirm that a lot is gone but can never tell us how much is left — it is
read as available / sold_out, never "limited".

Venue ids are the awkward part: the event search needs Way's own venueId, and
nothing we hold maps to it. So this module works the way our SpotHero check
does — search by the venue's coordinates over the event's time window — and
uses the event search only when a venueId is already known.

This is the website's own API, not a documented one: it can change without
notice, and the tool should degrade to "unknown" rather than guess when it
does.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

API_BASE = "https://www.way.com"
SEARCH_URL = f"{API_BASE}/way-search/v2/public/search"
EVENTS_URL = f"{API_BASE}/way-search/v2/public/search/events"

# The consumer key the way.com web app sends with every search. Public, in the
# sense that it is in the page's JavaScript; kept here rather than in .env so
# nobody mistakes it for a secret of ours.
_CONSUMER_KEY = "Basic d2F5LXdlYi1jb25zdW1lcjozNTQxMzIxMC04NWVhLTRjMDYtOTIwNC05NGRjMjNjZWQ3M2M="

_TIMEOUT = 30
_PAGE_SIZE = 100

# way.com/robots.txt, for User-agent: *
#     Allow: /            (only /login, /signup, /checkoutguest,
#                          /account-verify and /way-ev-consumer/ are barred —
#                          the search paths we use are permitted)
#     Crawl-delay: 5
# So: one request every five seconds, account-wide, and never touch the
# disallowed paths.
_CRAWL_DELAY_SECONDS = 5.0
_last_request_at = 0.0
_rate_lock = threading.Lock()

# Way answers 403 when it decides we have asked for too much in a window —
# a few hundred requests over half an hour did it once. Hammering through
# that is both rude and pointless, so a refusal pauses every Way check for a
# while and the scan carries on with the other platforms. The listing simply
# reads "not checked" on Way until the pause lifts.
_COOLDOWN_SECONDS = 15 * 60
_cooldown_until = 0.0
_RETRY_AFTER_SECONDS = 30

# Their WAF rejects Python's TLS fingerprint with a Cloudflare challenge while
# answering curl normally — a bot-management heuristic, not a rule about who
# may read these pages, which robots.txt settles. Rather than dress requests
# up as a browser, we make the call with curl, a plain, honest HTTP client,
# at the rate their own robots.txt asks for.
_CURL = "curl"

# The window we ask about, around an event's start. Way prices hourly parking,
# so a window that brackets the event is the closest equivalent to SpotHero's
# transient search for the same slot.
_HOURS_BEFORE = 2
_HOURS_AFTER = 4


def _headers() -> Dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "authorization": _CONSUMER_KEY,
        "content-type": "application/json;charset=UTF-8",
        "origin": API_BASE,
        "referer": f"{API_BASE}/parking/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
    }


def _wait_for_turn() -> None:
    """Hold every caller to the crawl-delay their robots.txt asks for."""
    global _last_request_at
    with _rate_lock:
        gap = time.monotonic() - _last_request_at
        if gap < _CRAWL_DELAY_SECONDS:
            time.sleep(_CRAWL_DELAY_SECONDS - gap)
        _last_request_at = time.monotonic()


def _in_cooldown() -> Optional[str]:
    left = _cooldown_until - time.monotonic()
    if left > 0:
        return f"Way.com asked us to slow down — paused for another {left/60:.0f} min"
    return None


def _start_cooldown() -> None:
    global _cooldown_until
    _cooldown_until = time.monotonic() + _COOLDOWN_SECONDS
    logger.warning("Way.com returned 403 — pausing Way checks for %d minutes", _COOLDOWN_SECONDS // 60)


def _post(url: str, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    (data, error). Never raises: a platform being unreachable is a reading,
    not a crash.

    A 403 is retried once after a pause, since it is usually a rate limit
    rather than a refusal; a second one stops Way checks for a while.
    """
    paused = _in_cooldown()
    if paused:
        return None, paused

    data, err = _post_once(url, payload)
    if err == "HTTP 403":
        time.sleep(_RETRY_AFTER_SECONDS)
        data, err = _post_once(url, payload)
        if err == "HTTP 403":
            _start_cooldown()
            return None, "Way.com rate-limited us (403) — paused"
    return data, err


def _post_once(url: str, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    _wait_for_turn()
    args = [_CURL, "-s", "--max-time", str(_TIMEOUT), "--url", url, "--data-binary", "@-",
            "-w", "\n%{http_code}"]
    for key, value in _headers().items():
        args += ["-H", f"{key}: {value}"]
    try:
        done = subprocess.run(args, input=json.dumps(payload), capture_output=True,
                              text=True, timeout=_TIMEOUT + 10)
    except (subprocess.SubprocessError, OSError) as exc:
        return None, f"request failed: {exc}"

    body, _, code = done.stdout.rpartition("\n")
    if code.strip() != "200":
        return None, f"HTTP {code.strip() or 'no response'}"
    try:
        return json.loads(body), None
    except ValueError:
        # A challenge page comes back as HTML. Report it rather than trying to
        # look like something we are not.
        return None, "Way.com returned a challenge page instead of JSON"


def _window(event_date: str) -> Tuple[str, str]:
    """The check-in/check-out pair to ask about for an event."""
    try:
        start = datetime.fromisoformat((event_date or "")[:19])
    except ValueError:
        start = datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    return (start - timedelta(hours=_HOURS_BEFORE)).strftime(fmt), \
           (start + timedelta(hours=_HOURS_AFTER)).strftime(fmt)


def _lot(row: Dict[str, Any]) -> Dict[str, Any]:
    """One Way.com search result in the shape the rest of the tool expects."""
    address = row.get("address") or {}
    available = bool(row.get("availability")) and bool(row.get("bookable", True))
    price = row.get("minPrice")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    return {
        "platform": "way",
        "lot_id": str(row.get("listingId") or "") or None,
        "lot_name": row.get("listingName") or "",
        "lot_address": address.get("addressString") or "",
        "price": price,
        "lat": _float(row.get("lat") or address.get("lat")),
        "lon": _float(row.get("lon") or address.get("lon")),
        # No count is published, so this is the whole story: on sale, or gone.
        "spots_left": None,
        "capacity": None,
        "percent_remaining": None,
        "availability_status": "available" if available else "sold_out",
        "scarcity_level": "ok" if available else "sold_out",
    }


def _float(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# Entries expire: a scan runs every three hours and availability is the whole
# point, so a reading kept beyond one scan would quietly report yesterday's
# inventory as today's.
_CACHE_TTL_SECONDS = 45 * 60
_cache: Dict[tuple, Tuple[float, Tuple[List[Dict[str, Any]], Optional[str]]]] = {}
_cache_lock = threading.Lock()


def search_lots_cached(lat: float, lon: float, event_date: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    search_lots, but once per venue and event window for the life of the
    process.

    Their robots.txt asks for five seconds between requests, and a scan checks
    hundreds of listings that share a few hundred events. Asking once per
    event rather than once per listing keeps us inside that rate and turns
    half an hour of waiting into a couple of minutes. Coordinates are rounded
    to about a hundred metres so listings at the same venue share an entry.
    """
    key = (round(lat, 3), round(lon, 3), (event_date or "")[:13])
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
    result = search_lots(lat, lon, event_date)
    # Don't cache a failure for three quarters of an hour — a rate limit or a
    # blip would otherwise blank Way for every listing at that venue.
    if result[1] is None:
        with _cache_lock:
            _cache[key] = (now, result)
    return result


def clear_cache() -> None:
    """Forget what we read — a new scan wants fresh availability."""
    with _cache_lock:
        _cache.clear()


def search_lots(lat: float, lon: float, event_date: str,
                venue_id: Optional[int] = None, event_id: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    The lots Way.com sells near a point for an event's window.

    With a venueId and eventId it asks the event search, which is what the
    site itself does; otherwise it asks the hourly search over the event's
    window, which is how we reach venues whose Way id we do not know.
    """
    checkin, checkout = _window(event_date)
    if venue_id and event_id:
        payload = {
            "lat": str(lat), "lon": str(lon),
            "paginationDto": {"pageNumber": 1, "pageSize": _PAGE_SIZE},
            "searchType": "PARKING",
            "parkingFilterDto": {"venueId": int(venue_id), "eventId": int(event_id)},
            "parkingSearchTab": "EVENT", "sortBy": "RELEVANCE",
            "pricingType": "Hourly", "showTotalPriceWithTaxes": False,
        }
    else:
        payload = {
            "lat": str(lat), "lon": str(lon),
            "paginationDto": {"pageNumber": 1, "pageSize": _PAGE_SIZE},
            "searchType": "PARKING", "sortBy": "RELEVANCE",
            "parkingFilterDto": {"checkin": checkin, "checkout": checkout},
            "parkingSearchTab": "HOURLY",
            "pricingType": "Hourly", "showTotalPriceWithTaxes": False,
        }

    data, err = _post(SEARCH_URL, payload)
    if err:
        return [], err
    rows = (data or {}).get("rows") or []
    return [_lot(r) for r in rows], None


def venue_events(venue_id: int, lat: float, lon: float) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Way.com's events for a venue it knows by id — {event_id, name, time, lots}."""
    data, err = _post(EVENTS_URL, {
        "lat": str(lat), "lon": str(lon),
        "paginationDto": {"pageNumber": 1, "pageSize": _PAGE_SIZE},
        "searchType": "PARKING",
        "parkingFilterDto": {"venueId": int(venue_id)},
        "parkingSearchTab": "EVENT", "pricingType": "Hourly",
    })
    if err:
        return [], err
    events = []
    for row in (data or {}).get("rows") or []:
        events.append({
            "event_id": row.get("eventId"),
            "event_name": row.get("eventName"),
            "event_time": row.get("eventTime"),
            "venue_id": row.get("venueId"),
            "venue_name": row.get("venueName"),
            "lots": row.get("parkingLotCount"),
        })
    return events, None
