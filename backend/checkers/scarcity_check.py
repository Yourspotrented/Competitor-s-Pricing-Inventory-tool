"""
Scarcity checker — given one of our ReachPro events, finds the matching event/lot
on SpotHero and ParkWhiz and reads off how close to sold out they are.

SpotHero returns an exact remaining count (`availability.available_spaces`) plus
total capacity (`facility.common.inventory.default_quantity`), so we can compute
an exact percent-remaining.

ParkWhiz only returns a 3-state status per lot (`available` / `limited` /
`sold_out`) — no exact count is exposed via their API — so scarcity there is
just that status.

Usage:
    from checkers.scarcity_check import check_scarcity
    result = check_scarcity(
        event_name="Harry Styles",
        event_date="2026-10-10",
        event_venue="Madison Square Garden Parking Lots",
        our_section="359 9th Ave.",
    )
"""
from __future__ import annotations

import concurrent.futures
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_SPOTHERO_API = "https://api.spothero.com/v2"
_SPOTHERO_HEADERS = {
    "accept": "application/json",
    "user-agent": "Mozilla/5.0",
    "origin": "https://spothero.com",
    "referer": "https://spothero.com/",
}
_PARKWHIZ_API = "https://api.parkwhiz.com/v4"
_PARKWHIZ_V31 = "https://api.parkwhiz.com/v3_1"
_NOMINATIM = "https://nominatim.openstreetmap.org/search"

# ---------------------------------------------------------------------------
# Per-domain request throttling.
#
# Nominatim's usage policy caps free requests at ~1/sec, and hitting that
# limit under bulk-scan load previously caused silent geocode failures
# (HTTP 429s that looked identical to "venue not found"). SpotHero and
# ParkWhiz are undocumented public endpoints with no published rate limit,
# but bulk scans hit them just as hard (once per event, in parallel across
# threads), so the same protection is applied to all three as a precaution
# — we'd rather scan slightly slower than risk getting rate-limited or
# temporarily blocked by any of them.
# ---------------------------------------------------------------------------

class _DomainThrottle:
    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> None:
        with self._lock:
            wait = self._last_call + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()


_nominatim_throttle_obj = _DomainThrottle(1.1)   # Nominatim's documented ~1 req/sec policy
_spothero_throttle_obj = _DomainThrottle(0.3)    # undocumented — conservative precaution
_parkwhiz_throttle_obj = _DomainThrottle(0.3)    # undocumented — conservative precaution


def _throttled_get(throttle: "_DomainThrottle", url: str, *, retries: int = 3, **kwargs) -> Optional[requests.Response]:
    """GET with per-domain throttling and retry-with-backoff on HTTP 429."""
    for attempt in range(retries):
        throttle.wait()
        try:
            resp = requests.get(url, **kwargs)
        except requests.RequestException as exc:
            logger.debug("Request failed for %s: %s", url, exc)
            return None
        if resp.status_code == 429:
            wait = throttle._min_interval * (2 ** (attempt + 1))
            logger.warning("Rate-limited by %s (attempt %d/%d), backing off %.1fs",
                           url, attempt + 1, retries, wait)
            time.sleep(wait)
            continue
        return resp
    logger.warning("Gave up on %s after %d rate-limited retries", url, retries)
    return None

_PARKWHIZ_BEARER = "a1ba91391c738d5161764790e9dcbb8bf59eb13a25fedcd4218cd3dd17392536"
_PARKWHIZ_HEADERS = {
    "accept": "application/json",
    "authorization": f"Bearer {_PARKWHIZ_BEARER}",
    "origin": "https://www.parkwhiz.com",
    "referer": "https://www.parkwhiz.com/",
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "x-language-locale": "en-us",
}

_MSG_LAT, _MSG_LON = 40.75046, -73.99346


# ---------------------------------------------------------------------------
# Matching / geocoding helpers (same approach as listing_availability_checker)
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _normalize_addr(text: str) -> str:
    t = text.lower()
    t = re.sub(r"\bnorth\b", "n", t)
    t = re.sub(r"\bsouth\b", "s", t)
    t = re.sub(r"\beast\b", "e", t)
    t = re.sub(r"\bwest\b", "w", t)
    t = re.sub(r"\bstreet\b", "st", t)
    t = re.sub(r"\bavenue\b", "ave", t)
    t = re.sub(r"\bboulevard\b", "blvd", t)
    t = re.sub(r"\bdrive\b", "dr", t)
    t = re.sub(r"\broad\b", "rd", t)
    t = re.sub(r"\blane\b", "ln", t)
    return re.sub(r"[^a-z0-9]", "", t)


def _extract_addr(section: str) -> str:
    cleaned = re.split(r"\s*[-–]\s*(?:spot\s*#|\d+\.?\d*\s*(?:mi\b|mile|block)|\.?\d+\s*mi\b|[\d.]+\s*miles|\d+\s+only\b|[\d.]+\s*away)", section, flags=re.I)[0]
    cleaned = re.sub(r"\s*([-–].*)?$", "", cleaned.split(" -")[0]).strip() if " -" in cleaned else cleaned.strip()
    return cleaned


def _section_matches(our: str, candidate: str) -> bool:
    o = _normalize(our)
    c = _normalize(candidate)
    if o and (o in c or c in o):
        return True
    oa = _normalize_addr(_extract_addr(our))
    ca = _normalize_addr(candidate)
    return bool(oa) and len(oa) >= 4 and (oa in ca or ca in oa)


def _clean_venue(venue: str) -> str:
    return re.sub(r"(?i)\s+parking(?:\s+lots?)?$", "", venue).strip()


def _strip_brand(section: str) -> str:
    cleaned = re.sub(r"^\([^)]+\)\s*-\s*", "", section).strip()
    cleaned = re.sub(r"^[\w\s]+(?:parking|ipark|impark|mpg|laz)[^-]*-\s*", "", cleaned, flags=re.I).strip()
    cleaned = re.sub(r"\s*[-–].*?(mi|away|walk|venue).*$", "", cleaned, flags=re.I).strip()
    cleaned = re.sub(r"\s+(parking\s+corp\.?|llc\s+garage|garage|parking\s+llc|lot)$", "", cleaned, flags=re.I).strip()
    return cleaned or section


def _parse_event_dt(event_date: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(event_date[:19], fmt[:len(event_date[:19])])
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(event_date.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _geocode_cache_get(query: str) -> Optional[tuple[Optional[float], Optional[float]]]:
    """Returns (lat, lon) if cached (lat/lon may be None for a cached "not found"), or None if never cached."""
    try:
        from database import GeocodeCache, get_session
        db = get_session()
        try:
            row = db.query(GeocodeCache).filter(GeocodeCache.query == query).first()
            return (row.lat, row.lon) if row is not None else None
        finally:
            db.close()
    except Exception as exc:
        logger.debug("Geocode cache read failed for %r: %s", query, exc)
        return None


def _geocode_cache_put(query: str, coords: Optional[tuple[float, float]]) -> None:
    try:
        from database import GeocodeCache, utcnow, get_session
        db = get_session()
        try:
            row = GeocodeCache(
                query=query,
                lat=coords[0] if coords else None,
                lon=coords[1] if coords else None,
                cached_at=utcnow(),
            )
            db.merge(row)  # upsert on the query primary key
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.debug("Geocode cache write failed for %r: %s", query, exc)


def _geocode(query: str) -> Optional[tuple[float, float]]:
    # Venue locations never change, so a cached result — including a cached
    # "not found" — is reused forever instead of re-asking Nominatim every scan.
    cached = _geocode_cache_get(query)
    if cached is not None:
        lat, lon = cached
        return (lat, lon) if lat is not None and lon is not None else None

    resp = _throttled_get(
        _nominatim_throttle_obj, _NOMINATIM,
        params={"q": query, "format": "json", "limit": 1},
        headers={"User-Agent": "competitors-data-finder/1.0"},
        timeout=10,
    )
    if resp is None:
        return None  # transient/network/rate-limit failure — don't cache, worth retrying next scan
    try:
        resp.raise_for_status()
        results = resp.json()
    except Exception as exc:
        logger.debug("Geocode failed for %r: %s", query, exc)
        return None  # don't cache — not a confirmed "not found"

    coords = (float(results[0]["lat"]), float(results[0]["lon"])) if results else None
    _geocode_cache_put(query, coords)
    return coords


def _geocode_venue(event_venue: str, our_section: str) -> Optional[tuple[float, float]]:
    clean_venue = _clean_venue(event_venue) if event_venue else ""
    is_generic = _normalize(our_section) in ("generaladmission", "generalparking", "")

    if not is_generic and our_section:
        cleaned = _strip_brand(our_section)
        q = f"{cleaned}, near {clean_venue}" if clean_venue else cleaned
        coords = _geocode(q)
        if coords:
            return coords

    if clean_venue:
        coords = _geocode(clean_venue)
        if coords:
            return coords

    return None


# ---------------------------------------------------------------------------
# SpotHero — exact spots_left + capacity
# ---------------------------------------------------------------------------

def _find_spothero_event(event_name: str, event_date: str, event_venue: str) -> Optional[dict]:
    clean_venue = _clean_venue(event_venue)
    clean_name = re.sub(r"(?i)^parking passes only\s+", "", event_name).strip()
    query = f"{clean_name} {clean_venue}"
    resp = _throttled_get(
        _spothero_throttle_obj, f"{_SPOTHERO_API}/events/search",
        params={"search_query": query},
        headers=_SPOTHERO_HEADERS,
        timeout=10,
    )
    try:
        results = resp.json().get("results", []) if resp is not None and resp.ok else []
    except Exception:
        return None

    target_dt = _parse_event_dt(event_date) if event_date else None
    norm_venue = _normalize(clean_venue)

    for r in results:
        dest = _normalize(r.get("destination_title", ""))
        if not (norm_venue in dest or dest in norm_venue):
            continue
        if target_dt:
            try:
                ev_dt = datetime.fromisoformat(r["starts"].replace("Z", "+00:00")).replace(tzinfo=None)
                if abs((ev_dt - target_dt).days) > 1:
                    continue
            except Exception:
                pass
        dest_info = r.get("destination", {})
        return {
            "event_id": r["event_id"],
            "lat": dest_info.get("latitude"),
            "lon": dest_info.get("longitude"),
            "starts": r.get("parking_window", {}).get("starts", r["starts"])[:19],
            "ends": r.get("parking_window", {}).get("ends", "")[:19],
        }
    return None


def _spot_price(r: dict) -> Optional[float]:
    for rate in r.get("rates", []):
        val = rate.get("quote", {}).get("total_price", {}).get("value")
        if val:
            return val / 100
    return None


def _spot_addr(r: dict) -> str:
    addrs = r.get("facility", {}).get("common", {}).get("addresses", [])
    return next((a.get("street_address", "") for a in addrs if a.get("street_address")), "")


def _spot_coords(r: dict) -> Optional[tuple[float, float]]:
    """(lat, lon) of a SpotHero result's address entry, if present."""
    addrs = r.get("facility", {}).get("common", {}).get("addresses", [])
    for a in addrs:
        lat, lon = a.get("latitude"), a.get("longitude")
        if lat is not None and lon is not None:
            return (lat, lon)
    return None


def _spot_name(r: dict) -> str:
    return r.get("facility", {}).get("common", {}).get("title", "")


def _spot_facility_id(r: dict) -> str:
    """
    SpotHero's own facility id for a search result. This is the same id space
    as the "Facility Id"/"ID" column in the team's sheets — our facilities are
    SpotHero listings, so their sheet ids match SpotHero's ids exactly. That
    makes an id comparison an exact answer to "is this lot ours?", where
    address text matching is only a guess (see clustering._is_ours).
    """
    return str(r.get("facility", {}).get("common", {}).get("id") or "")


def geocode_by_spothero_facility_id(facility_id: str) -> Optional[tuple[float, float]]:
    """
    Exact coordinates for one of OUR facilities, read from SpotHero's own
    record for that facility id.

    Preferred over geocoding the address parsed out of a spot name: it
    resolves the parenthetical/garbled addresses Nominatim can't handle
    ("105 Peterborough St. (Private Alley 932)"), and returns the lot's
    actual location rather than a street-level approximation — which matters
    when the search radius is as tight as the Events team's 0.1mi.

    Cached forever in the same GeocodeCache table as Nominatim lookups (a
    facility doesn't move), including a cached "no such facility".
    """
    cache_key = f"spothero-facility:{facility_id}"
    cached = _geocode_cache_get(cache_key)
    if cached is not None:
        lat, lon = cached
        return (lat, lon) if lat is not None and lon is not None else None

    resp = _throttled_get(_spothero_throttle_obj, f"{_SPOTHERO_API}/facilities/{facility_id}",
                           headers=_SPOTHERO_HEADERS, timeout=15)
    if resp is None:
        return None  # transient/network/rate-limit failure — don't cache, retry next scan
    if resp.status_code == 404:
        _geocode_cache_put(cache_key, None)  # confirmed: not a SpotHero facility id
        return None
    try:
        resp.raise_for_status()
        addrs = (resp.json().get("common", {}) or {}).get("addresses") or []
    except Exception as exc:
        logger.debug("SpotHero facility lookup failed for %s: %s", facility_id, exc)
        return None  # don't cache — not a confirmed miss

    coords = next(
        ((a["latitude"], a["longitude"]) for a in addrs
         if a.get("latitude") is not None and a.get("longitude") is not None),
        None,
    )
    _geocode_cache_put(cache_key, coords)
    return coords


def _spot_scarcity(r: dict) -> dict:
    """Extract exact spots-left / capacity from a SpotHero result."""
    avail = r.get("availability", {}) or {}
    inventory = r.get("facility", {}).get("common", {}).get("inventory", {}) or {}
    spots_left = avail.get("available_spaces")
    capacity = inventory.get("default_quantity")
    pct = None
    if isinstance(spots_left, (int, float)) and isinstance(capacity, (int, float)) and capacity > 0:
        pct = round(spots_left / capacity * 100, 1)

    if spots_left is None:
        level = "unknown"
    elif spots_left == 0:
        level = "sold_out"
    elif pct is not None and pct <= 15:
        level = "limited"
    else:
        level = "ok"

    return {
        "spots_left": spots_left,
        "capacity": capacity,
        "percent_remaining": pct,
        "scarcity_level": level,
    }


def _search_spothero_transient(lat: float, lon: float, starts_str: str, ends_str: str,
                                event_id_param: Optional[str] = None,
                                include_unavailable: bool = False) -> tuple[list, Optional[str]]:
    """
    Raw SpotHero transient search by coordinates + time window. Returns (results, error).

    include_unavailable keeps sold-out lots in the response (they come back
    with available_spaces=0 and unavailable_reasons=["Sold Out"]). Off by
    default to preserve the event-scarcity path's behavior; the facility
    portfolio scan turns it on, since a sold-out lot is the strongest
    demand signal there is and is exactly what that scan looks for.
    """
    params = {
        "lat": lat, "lon": lon,
        "starts": starts_str,
        "ends": ends_str,
        "show_unavailable": "true" if include_unavailable else "false",
        "initial_search": "true",
        "sort_by": "relevance",
        "include_walking_distance": "true",
    }
    if event_id_param:
        params["event_id"] = event_id_param

    resp = _throttled_get(_spothero_throttle_obj, f"{_SPOTHERO_API}/search/transient",
                           params=params, headers=_SPOTHERO_HEADERS, timeout=15)
    if resp is None:
        return [], "rate-limited or request failed"
    try:
        return (resp.json().get("results", []) if resp.ok else []), None
    except Exception as exc:
        return [], str(exc)


def _fetch_spothero_lots(event_name: str, event_venue: str, event_date: str, our_section: str) -> tuple[list, Optional[str]]:
    """Fetch all SpotHero lots near the venue/time. Returns (results, error)."""
    if not event_venue and not our_section:
        return [], "no location data"

    target_dt = _parse_event_dt(event_date) if event_date else None
    sh_event = _find_spothero_event(event_name, event_date, event_venue)

    if sh_event:
        lat, lon = sh_event["lat"], sh_event["lon"]
        starts_str = sh_event["starts"]
        ends_str = sh_event["ends"]
        event_id_param = sh_event["event_id"]
    else:
        coords = _geocode_venue(event_venue, our_section)
        if not coords:
            return [], "could not geocode venue"
        lat, lon = coords
        starts = target_dt or datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)
        ends = starts + timedelta(hours=5)
        starts_str = starts.strftime("%Y-%m-%dT%H:%M:%S")
        ends_str = ends.strftime("%Y-%m-%dT%H:%M:%S")
        event_id_param = None

    return _search_spothero_transient(lat, lon, starts_str, ends_str, event_id_param)


def fetch_spothero_lots_near(lat: float, lon: float, start_hour: int = 10, end_hour: int = 22) -> tuple[list, Optional[str]]:
    """
    Fetch all SpotHero lots near a static address (lat/lon), independent of
    any event. Used for facility-portfolio competitor discovery, where "our"
    listing is a permanent garage/lot rather than an event-tied section.

    Defaults to today 10am-10pm — the team's own standard booking window
    (noted directly in their facility sheet) — rather than a generic 24h
    window. A 24h window returns different (usually pricier, sometimes
    different-availability) inventory than a normal shopper's few-hour
    booking, which doesn't match what manually checking SpotHero shows.
    If the window has already passed today, it rolls to tomorrow.

    Sold-out lots are included. Excluding them (SpotHero's default) meant a
    competitor selling out — the single clearest "raise our price" signal
    this tool exists to catch — silently vanished from results instead of
    being reported, so the sold_out scarcity level never once fired across
    ~12k readings. It also lets our own sold-out facilities report 0 spots
    left rather than going missing from the scan entirely.
    """
    now = datetime.now()
    starts = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    ends = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if ends <= now:
        starts += timedelta(days=1)
        ends += timedelta(days=1)
    return _search_spothero_transient(
        lat, lon,
        starts.strftime("%Y-%m-%dT%H:%M:%S"),
        ends.strftime("%Y-%m-%dT%H:%M:%S"),
        include_unavailable=True,
    )


def _check_spothero(event_name: str, event_venue: str, event_date: str, our_section: str) -> dict:
    base = {"platform": "spothero", "is_available": None, "price": None,
            "spots_left": None, "capacity": None, "percent_remaining": None,
            "availability_status": None, "scarcity_level": "unknown", "error": None}

    results, err = _fetch_spothero_lots(event_name, event_venue, event_date, our_section)
    if err:
        return {**base, "error": err}

    if not results:
        # No lots at all near this venue/time on SpotHero — could mean genuinely
        # sold out, but could also mean the venue/event wasn't found there.
        # Without a positive match we can't tell the difference, so don't claim sold_out.
        return {**base, "is_available": False, "scarcity_level": "not_found",
                "error": "no results near venue"}

    is_generic = _normalize(our_section) in ("generaladmission", "generalparking", "")

    if not is_generic:
        matched = [
            r for r in results
            if _section_matches(our_section, _spot_addr(r)) or _section_matches(our_section, _spot_name(r))
        ]
        if matched:
            r = matched[0]
            scarcity = _spot_scarcity(r)
            return {**base, "is_available": True, "price": _spot_price(r), **scarcity}
        # Other lots exist nearby but not our specific lot — likely not listed
        # there (or our matching missed it), not evidence it's sold out.
        return {**base, "is_available": False, "scarcity_level": "not_found",
                "error": "our lot not found among nearby results"}

    # Generic section — report the tightest (lowest spots_left) nearby lot
    scored = [(r, _spot_scarcity(r)) for r in results]
    scored.sort(key=lambda pair: (pair[1]["spots_left"] if pair[1]["spots_left"] is not None else 10**9))
    r, scarcity = scored[0]
    return {**base, "is_available": True, "price": _spot_price(r), **scarcity}


def list_spothero_lots(event_name: str, event_venue: str, event_date: str, our_section: str = "") -> list[dict]:
    """
    Return every SpotHero lot found near the venue/time, each with its own
    spots_left/capacity/price — for events with multiple competing lots.
    """
    results, err = _fetch_spothero_lots(event_name, event_venue, event_date, our_section)
    if err or not results:
        return []

    lots = []
    for r in results:
        scarcity = _spot_scarcity(r)
        lots.append({
            "platform": "spothero",
            "lot_name": _spot_name(r),
            "lot_address": _spot_addr(r),
            "price": _spot_price(r),
            "is_our_lot": bool(our_section) and (
                _section_matches(our_section, _spot_addr(r)) or _section_matches(our_section, _spot_name(r))
            ),
            **scarcity,
        })
    lots.sort(key=lambda l: (l["spots_left"] if l["spots_left"] is not None else 10**9))
    return lots


# ---------------------------------------------------------------------------
# ParkWhiz — status only (available / limited / sold_out), no exact count
# ---------------------------------------------------------------------------

_PW_VENUE_CACHE: list = []


def _get_pw_venues() -> list:
    global _PW_VENUE_CACHE
    if _PW_VENUE_CACHE:
        return _PW_VENUE_CACHE
    venues = []
    for page in range(1, 25):
        resp = _throttled_get(_parkwhiz_throttle_obj, f"{_PARKWHIZ_API}/venues/",
                               params={"per_page": 100, "page": page},
                               headers=_PARKWHIZ_HEADERS, timeout=10)
        if resp is None:
            break
        try:
            batch = resp.json() if resp.ok and isinstance(resp.json(), list) else []
            if not batch:
                break
            venues.extend(batch)
        except Exception:
            break
    _PW_VENUE_CACHE = venues
    return venues


def _find_parkwhiz_venue_id(event_venue: str) -> Optional[int]:
    clean_v = _clean_venue(event_venue)
    norm_v = _normalize(clean_v)
    venues = _get_pw_venues()
    best_id, best_score = None, 0
    for v in venues:
        vn = _normalize(v.get("name", ""))
        if not vn:
            continue
        if vn in norm_v or norm_v in vn:
            score = min(len(vn), len(norm_v))
            if score > best_score:
                best_score, best_id = score, v["id"]
    return best_id


def _find_parkwhiz_event_id(event_name: str, event_date: str, event_venue: str,
                             coords: Optional[tuple[float, float]] = None) -> Optional[int]:
    target_dt = _parse_event_dt(event_date) if event_date else None
    clean_name = re.sub(r"(?i)^parking passes only\s+", "", event_name).strip()
    norm_name = _normalize(clean_name[:20])

    venue_id = _find_parkwhiz_venue_id(event_venue)
    if venue_id is not None:
        ev_resp = _throttled_get(
            _parkwhiz_throttle_obj, f"{_PARKWHIZ_V31}/venues/{venue_id}/events",
            params={"page": 1, "per_page": 100, "fields": "event::default", "sort": "start_time"},
            headers=_PARKWHIZ_HEADERS, timeout=15,
        )
        try:
            events = ev_resp.json() if ev_resp is not None and ev_resp.ok and isinstance(ev_resp.json(), list) else []
        except Exception:
            events = []

        for ev in events:
            if norm_name not in _normalize(ev.get("name", "")):
                continue
            if target_dt:
                try:
                    ev_dt = datetime.fromisoformat(ev["start_time"].replace("Z", "+00:00")).replace(tzinfo=None)
                    if abs((ev_dt - target_dt).days) > 1:
                        continue
                except Exception:
                    pass
            return ev["id"]

    if coords is None:
        coords = _geocode_venue(event_venue, "")
    if coords is None:
        return None

    lat, lon = coords
    ev_resp = _throttled_get(
        _parkwhiz_throttle_obj, f"{_PARKWHIZ_V31}/events/",
        params={"lat": lat, "lon": lon, "radius": 2, "per_page": 100, "sort": "start_time"},
        headers=_PARKWHIZ_HEADERS, timeout=15,
    )
    if ev_resp is None:
        return None
    try:
        events = ev_resp.json() if ev_resp.ok and isinstance(ev_resp.json(), list) else []
    except Exception:
        return None

    for ev in events:
        if norm_name not in _normalize(ev.get("name", "")):
            continue
        if target_dt:
            try:
                ev_dt = datetime.fromisoformat(ev["start_time"].replace("Z", "+00:00")).replace(tzinfo=None)
                if abs((ev_dt - target_dt).days) > 1:
                    continue
            except Exception:
                pass
        return ev["id"]
    return None


def _query_parkwhiz_quotes(lat: float, lon: float, start_time: str, end_time: str,
                            event_id: Optional[int] = None) -> tuple[list, Optional[str]]:
    """Raw ParkWhiz /quotes/ call for a lat/lon + time window, optionally scoped to an event_id."""
    delta = 0.02
    bounds = f"{lat+delta},{lon-delta},{lat-delta},{lon+delta}"
    q_param = f"anchor_coordinates:{lat},{lon} search_type:transient bounds:{bounds}"
    if event_id is not None:
        q_param += f" event_id:{event_id}"

    resp = _throttled_get(
        _parkwhiz_throttle_obj, f"{_PARKWHIZ_API}/quotes/",
        params={
            "start_time": start_time, "end_time": end_time, "email": "",
            "fields": ("quote::default,quote:shuttle_times,location::default,location:timezone,"
                       "location:site_url,location:address2,location:description,location:msa,"
                       "location:rating_summary"),
            "option_types": "all",
            "returns": "curated offstreet_bookable_sold_out offstreet_bookable",
            "q": q_param, "routing_style": "parkwhiz",
            "capabilities": "capture_plate:always",
        },
        headers=_PARKWHIZ_HEADERS, timeout=20,
    )
    if resp is None:
        return [], "rate-limited or request failed"
    try:
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return [], str(exc)

    raw_lots = data.get("data", [])
    lots = []
    for q in raw_lots:
        loc = q.get("_embedded", {}).get("pw:location", {})
        name = loc.get("name", "")
        addr = loc.get("address1", "")
        opts = q.get("purchase_options", [])
        if not opts:
            continue
        price_raw = opts[0].get("price", {}).get("USD")
        price = float(price_raw) if price_raw else None
        status = (opts[0].get("space_availability") or {}).get("status", "unknown")

        coords = None
        entrances = loc.get("entrances") or []
        if entrances:
            c = entrances[0].get("coordinates")
            if c and len(c) == 2:
                coords = (c[0], c[1])

        if name or addr:
            lots.append({"name": name, "address": addr, "price": price, "status": status, "coords": coords})
    return lots, None


def _fetch_parkwhiz_lots(event_name: str, event_date: str, event_venue: str, our_section: str) -> tuple[list, Optional[str]]:
    """Fetch all ParkWhiz lot quotes for this event. Returns (lots, error)."""
    if not event_name or not event_venue:
        return [], "missing event info"

    coords = _geocode_venue(event_venue, our_section)
    pw_event_id = _find_parkwhiz_event_id(event_name, event_date, event_venue, coords)
    if pw_event_id is None:
        return [], "event not found on ParkWhiz"

    if coords is None:
        coords = (_MSG_LAT, _MSG_LON)
    lat, lon = coords

    target_dt = _parse_event_dt(event_date) if event_date else None
    if target_dt:
        start_time = target_dt.strftime("%Y-%m-%dT19:00:00")
        end_time = (target_dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    else:
        start_time = end_time = ""

    return _query_parkwhiz_quotes(lat, lon, start_time, end_time, event_id=pw_event_id)


def fetch_parkwhiz_lots_near(lat: float, lon: float, start_hour: int = 10, end_hour: int = 22) -> tuple[list, Optional[str]]:
    """
    Fetch all ParkWhiz lots near a static address (lat/lon), independent of
    any event — the facility-portfolio equivalent of fetch_spothero_lots_near.
    No event_id is passed, since there's no event to scope to; ParkWhiz's own
    bounds param does the geo filtering.

    Defaults to today start_hour-end_hour (10am-10pm), matching the SpotHero
    facility search window, rolling to tomorrow if already past end_hour.

    ParkWhiz lots only carry a name/address/price/status (available/limited/
    sold_out) — no exact spots_left/capacity like SpotHero, so these are
    reported separately, not merged into the SpotHero-based inventory sums.
    """
    now = datetime.now()
    starts = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    ends = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if ends <= now:
        starts += timedelta(days=1)
        ends += timedelta(days=1)
    return _query_parkwhiz_quotes(
        lat, lon,
        starts.strftime("%Y-%m-%dT%H:%M:%S"),
        ends.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def _check_parkwhiz(event_name: str, event_date: str, event_venue: str, our_section: str) -> dict:
    base = {"platform": "parkwhiz", "is_available": None, "price": None,
            "spots_left": None, "capacity": None, "percent_remaining": None,
            "availability_status": None, "scarcity_level": "unknown", "error": None}

    lots, err = _fetch_parkwhiz_lots(event_name, event_date, event_venue, our_section)
    if err == "event not found on ParkWhiz":
        # ParkWhiz genuinely has no matching event for this venue/date — a
        # confirmed absence, not a failed check. Same category as the other
        # not_found cases below, not "unknown".
        return {**base, "is_available": False, "scarcity_level": "not_found", "error": err}
    if err:
        return {**base, "error": err}

    if not lots:
        # ParkWhiz recognized the event but returned zero lot quotes for it —
        # could be genuinely sold out event-wide, but could also mean no
        # inventory was ever listed. Treat as not_found rather than sold_out.
        return {**base, "is_available": False, "scarcity_level": "not_found",
                "error": "no lot quotes returned"}

    def _level(status: str) -> str:
        return {"available": "ok", "limited": "limited", "sold_out": "sold_out"}.get(status, "unknown")

    is_generic = _normalize(our_section) in ("generaladmission", "generalparking", "")
    if not is_generic:
        matched = [l for l in lots if _section_matches(our_section, l["name"]) or _section_matches(our_section, l["address"])]
        if matched:
            l = matched[0]
            return {**base, "is_available": l["status"] != "sold_out", "price": l["price"],
                    "availability_status": l["status"], "scarcity_level": _level(l["status"])}
        # Other lots exist for this event but not our specific lot.
        return {**base, "is_available": False, "scarcity_level": "not_found",
                "error": "our lot not found among ParkWhiz quotes"}

    # Generic section — report the tightest lot (sold_out > limited > available)
    priority = {"sold_out": 0, "limited": 1, "available": 2, "unknown": 3}
    lots.sort(key=lambda l: priority.get(l["status"], 3))
    l = lots[0]
    return {**base, "is_available": l["status"] != "sold_out", "price": l["price"],
            "availability_status": l["status"], "scarcity_level": _level(l["status"])}


def list_parkwhiz_lots(event_name: str, event_date: str, event_venue: str, our_section: str = "") -> list[dict]:
    """
    Return every ParkWhiz lot found for this event, each with its own
    availability status — for events with multiple competing lots.
    """
    lots, err = _fetch_parkwhiz_lots(event_name, event_date, event_venue, our_section)
    if err or not lots:
        return []

    def _level(status: str) -> str:
        return {"available": "ok", "limited": "limited", "sold_out": "sold_out"}.get(status, "unknown")

    result = []
    for l in lots:
        result.append({
            "platform": "parkwhiz",
            "lot_name": l["name"],
            "lot_address": l["address"],
            "price": l["price"],
            "spots_left": None,
            "capacity": None,
            "percent_remaining": None,
            "availability_status": l["status"],
            "scarcity_level": _level(l["status"]),
            "is_our_lot": bool(our_section) and (
                _section_matches(our_section, l["name"]) or _section_matches(our_section, l["address"])
            ),
        })
    priority = {"sold_out": 0, "limited": 1, "ok": 2, "unknown": 3}
    result.sort(key=lambda l: priority.get(l["scarcity_level"], 3))
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def check_scarcity(event_name: str, event_date: str, event_venue: str, our_section: str) -> dict:
    """Check SpotHero and ParkWhiz scarcity in parallel for one of our events."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_spothero = pool.submit(_check_spothero, event_name, event_venue, event_date, our_section)
        f_parkwhiz = pool.submit(_check_parkwhiz, event_name, event_date, event_venue, our_section)
        return {
            "spothero": f_spothero.result(),
            "parkwhiz": f_parkwhiz.result(),
        }


def list_event_lots(event_name: str, event_date: str, event_venue: str, our_section: str = "") -> list[dict]:
    """
    Return every competing lot (SpotHero + ParkWhiz combined) found for this
    event, each with its own spots_left/status — powers the per-event
    multi-lot breakdown in the dashboard.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_spothero = pool.submit(list_spothero_lots, event_name, event_venue, event_date, our_section)
        f_parkwhiz = pool.submit(list_parkwhiz_lots, event_name, event_date, event_venue, our_section)
        return f_spothero.result() + f_parkwhiz.result()
