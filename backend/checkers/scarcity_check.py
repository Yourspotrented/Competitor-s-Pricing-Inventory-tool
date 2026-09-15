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
import math
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
    # Compound directionals first. SpotHero's street_address spells them out
    # ("105 Northeast 3rd Avenue") where our sheets abbreviate ("105 NE 3RD
    # AVE"), and the single-word rules below can't help: \bnorth\b does not
    # match inside "northeast". Without these the two normalize to
    # "105northeast3rdave" vs "105ne3rdave" and a real lot is missed.
    t = re.sub(r"\bnortheast\b", "ne", t)
    t = re.sub(r"\bnorthwest\b", "nw", t)
    t = re.sub(r"\bsoutheast\b", "se", t)
    t = re.sub(r"\bsouthwest\b", "sw", t)
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
    t = re.sub(r"\bcourt\b", "ct", t)
    t = re.sub(r"\bplace\b", "pl", t)
    t = re.sub(r"\bparkway\b", "pkwy", t)
    t = re.sub(r"\bhighway\b", "hwy", t)
    return re.sub(r"[^a-z0-9]", "", t)


def _extract_addr(section: str) -> str:
    cleaned = re.split(r"\s*[-–]\s*(?:spot\s*#|\d+\.?\d*\s*(?:mi\b|mile|block)|\.?\d+\s*mi\b|[\d.]+\s*miles|\d+\s+only\b|[\d.]+\s*away)", section, flags=re.I)[0]
    cleaned = re.sub(r"\s*([-–].*)?$", "", cleaned.split(" -")[0]).strip() if " -" in cleaned else cleaned.strip()
    return cleaned


def _section_matches(our: str, candidate: str) -> bool:
    # A blank candidate must never match. "" is a substring of every string,
    # so the old `c in o` test returned True for any result whose address
    # field was empty — and _check_spothero takes the FIRST match, so one
    # address-less lot could be reported as ours and its scarcity alerted on.
    o = _normalize(our)
    c = _normalize(candidate)
    if not o or not c:
        return False
    if o in c or c in o:
        return True
    oa = _normalize_addr(_extract_addr(our))
    ca = _normalize_addr(candidate)
    if len(oa) < 4 or len(ca) < 4:
        return False
    return oa in ca or ca in oa


_LOT_STOPWORDS = {
    "lot", "lots", "garage", "garages", "parking", "park", "self", "valet",
    "covered", "uncovered", "north", "south", "east", "west", "street", "avenue",
    "center", "centre", "only", "spot", "spots", "from", "venue", "away", "miles",
    "mile", "the", "and", "inn", "hotel", "suites", "plaza", "tower", "shops",
}


def _lot_tokens(text: str) -> set:
    """Distinctive words in a lot name — the ones that actually identify it."""
    words = re.findall(r"[a-z]{4,}", text.lower())
    return {w for w in words if w not in _LOT_STOPWORDS}


def _lot_name_matches(our: str, candidate: str) -> bool:
    """
    Last-resort name match for lots that share a name but not a string.

    Substring comparison misses real matches whenever either side adds words:
    our "TANGER OUTLET LOT" against SpotHero's "6800 N 95th Ave - Tanger
    Outlets - Glendale Lot" normalizes to "tangeroutletlot" vs
    "...tangeroutletsglendalelot", which is neither a prefix nor a suffix.
    Comparing distinctive words instead catches it.

    Deliberately strict: generic parking vocabulary is excluded, and a single
    shared word has to be at least six characters, so "Plaza Garage" cannot
    claim "Plaza Tower and Courtyard Shops Garage".
    """
    ours, theirs = _lot_tokens(our), _lot_tokens(candidate)
    if not ours or not theirs:
        return False
    shared = {o for o in ours for t in theirs
              if o == t or (len(o) >= 5 and len(t) >= 5 and (o.startswith(t) or t.startswith(o)))}
    if len(shared) >= 2:
        return True
    return any(len(w) >= 6 for w in shared)


def _stated_miles(section: str) -> Optional[float]:
    """The distance from the venue our own lot string claims, if it states one."""
    m = re.search(r"(?i)([\d.]+)\s*(?:mi\b|mile|miles)", section)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _anchor_is_plausible(our_coords: Optional[tuple], venue_coords: Optional[tuple],
                         our_section: str) -> bool:
    """
    Sanity-check a geocoded lot against the distance its own name claims.

    Street names repeat within a city as well as between cities. "1308 CLAY
    ST" for a Toyota Center listing marked "0.1 MILES AWAY" geocoded ~5km from
    the venue — same street name, wrong end of town. Believing it would anchor
    the search in the wrong neighbourhood.
    """
    if our_coords is None or venue_coords is None:
        return True                      # nothing to check against
    stated = _stated_miles(our_section)
    allowed_km = (stated * 1.609 * 3 + 1.0) if stated is not None else 8.0
    return _haversine_m(our_coords, venue_coords) / 1000.0 <= allowed_km


def _clean_event_name(event_name: str) -> str:
    """
    The event as the buying platforms name it.

    Our sources sometimes carry a "Parking" suffix or a "Parking passes only"
    prefix where SpotHero and ParkWhiz list the event itself. Lysted rows
    arrive already cleaned by lysted_listings.clean_event_name, so this is a
    no-op for them; it guards the ReachPro and events flows, which do not.
    """
    n = re.sub(r"(?i)^parking passes only\s+", "", event_name).strip()
    n = re.sub(r"(?i)\s*[-\u2013]?\s*parking(\s+pass(es)?)?\s*$", "", n).strip()
    return n or event_name


def _event_name_matches(ours: str, theirs: str) -> bool:
    """
    Do these two names refer to the same event?

    Tested both ways round on purpose. Our listings are frequently the more
    verbose side — "James Taylor and His All-Star Band" where ParkWhiz simply
    lists "James Taylor" — so requiring our name to sit inside theirs missed
    real events. Either name may be the abbreviation of the other; the venue
    and the +/-1 day window are what keep this from over-matching.
    """
    a = _normalize(ours)
    b = _normalize(theirs)
    if not a or not b:
        return False
    if a[:20] in b:
        return True
    return len(b) >= 8 and b[:20] in a


def _clean_venue(venue: str) -> str:
    return re.sub(r"(?i)\s+parking(?:\s+lots?)?$", "", venue).strip()


def _venue_key(name: str) -> str:
    """
    A venue name reduced to something comparable across platforms.

    Beyond _normalize: drops a trailing "Parking", parenthetical qualifiers
    ("(formerly Charleston Civic Center)"), and the word "and" together with
    "&". That last one matters because _normalize deletes punctuation, so our
    "Thomas and Mack Center" became "thomasandmackcenter" while SpotHero's
    "Thomas & Mack Center" became "thomasmackcenter" — neither contains the
    other, and a real event was discarded on every comparison.
    """
    n = _clean_venue(name or "")
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"(?i)\band\b|&", " ", n)
    return _normalize(n)


def _venue_matches(ours: str, theirs: str) -> bool:
    """Do these two venue names refer to the same place?"""
    a, b = _venue_key(ours), _venue_key(theirs)
    if not a or not b:
        return False
    if a == b:
        return True
    if (a in b or b in a) and min(len(a), len(b)) >= _PW_VENUE_MIN_OVERLAP:
        return True
    # "Highland Festival Grounds at Kentucky Exposition Center" is listed by
    # the platforms as the parent complex it sits inside.
    for x, y in ((ours, b), (theirs, a)):
        if " at " in (x or "").lower():
            tail = _venue_key(re.split(r"(?i)\s+at\s+", x, maxsplit=1)[-1])
            if tail and len(tail) >= _PW_VENUE_MIN_OVERLAP and (tail == y or tail in y or y in tail):
                return True
    return False


def _strip_brand(section: str) -> str:
    cleaned = re.sub(r"^\([^)]+\)\s*-\s*", "", section).strip()
    cleaned = re.sub(r"^[\w\s]+(?:parking|ipark|impark|mpg|laz)[^-]*-\s*", "", cleaned, flags=re.I).strip()
    cleaned = re.sub(r"\s*[-–].*?(mi|away|walk|venue).*$", "", cleaned, flags=re.I).strip()
    cleaned = re.sub(r"\s+(parking\s+corp\.?|llc\s+garage|garage|parking\s+llc|lot)$", "", cleaned, flags=re.I).strip()
    return cleaned or section


def _addr_for_geocode(section: str) -> str:
    """
    Our lot string reduced to something a geocoder can resolve.

    Deliberately separate from _extract_addr, which the facility/events flows
    also use: this strips more aggressively and only the Lysted path wants
    that. Lysted lot names carry trailing noise that defeats Nominatim —
    "1308 CLAY ST GARAGE (0.1 MILES AWAY)" and "810 WASHINGTON ST. LOT" both
    fail, while "1308 Clay St" and "810 Washington St" resolve.
    """
    s = _extract_addr(section)
    s = re.sub(r"\([^)]*\)", " ", s)                       # "(0.1 MILES AWAY)", "(LOT 8013)"
    s = re.sub(r"(?i)\b\d+(\.\d+)?\s*miles?\b.*$", " ", s)  # a distance the split missed
    s = re.sub(r"(?i)[\s,.-]+(lot|garage|parking(\s+lot)?)\s*$", " ", s)
    s = re.sub(r"[\s,.-]+$", "", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def _addr_fragment(text: str) -> str:
    """One candidate cleaned up, or "" if it is not a street address."""
    t = text.strip()
    if re.match(r"(?i)^[\d.]+\s*(mi|mile|miles)\b", t):
        return ""                                        # "0.7 MILES AWAY"
    t = re.sub(r"(?i)\b[\d.]+\s*miles?\b.*$", " ", t)
    t = re.sub(r"(?i)[\s,.-]+(lot|garage|parking(\s+lot)?)[\s.]*$", " ", t)
    t = re.sub(r"[\s,.-]+$", "", t).strip()
    if len(t) < 5 or not re.match(r"^\d", t):
        return ""                                        # must start with a street number
    return re.sub(r"\s{2,}", " ", t)


def _addr_candidates(section: str) -> list:
    """
    Every street address hiding in one lot string, best first.

    The address is often not at the front. Our lot strings are frequently
    "NAME - ADDRESS" or "NAME (ADDRESS)" — "ATHENS HOTEL & SUITES - 1308 CLAY
    ST GARAGE", "LEELAND (1515 SAN JACINTO STREET)", "THAXTON LOT - 615 SCOTT
    ST." — and taking only the head threw the address away, leaving a hotel
    name no geocoder could place.
    """
    out = []
    for inner in re.findall(r"\(([^)]*)\)", section):
        c = _addr_fragment(inner)
        if c:
            out.append(c)
    body = re.sub(r"\([^)]*\)", " ", section)
    for part in re.split(r"\s+[-\u2013]\s*|\s*[-\u2013]\s+", body):
        c = _addr_fragment(part)
        if c:
            out.append(c)
    head = _addr_for_geocode(section)
    if head and re.match(r"^\d", head) and len(head) >= 5:
        out.append(head)
    seen, uniq = set(), []
    for c in out:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Metres between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(h)))


# How close a competitor's pin must be to our geocoded address to call it the
# same physical lot.
#
# These are tight on purpose. A downtown block is only 80-120m, so a loose
# radius silently matches the garage on the NEXT block: at 150m this matched
# our "510 S Presa St" to "609 S Alamo St" and our "323 NW 4th St" to "310 NW
# 5th St". A wrong match is worse than no match — it would tell the listing
# team to deactivate a listing because someone else's lot sold out.
#
# Inside _EXACT_SPOT_METRES, position alone is enough. Between that and
# _SAME_LOT_METRES it must also be on the same street, which is what lets
# "810 Washington St" match a pin geocoded to 820, or "180 N Franklin St"
# match the "Lake-Franklin Garage" on the corner.
_EXACT_SPOT_METRES = 30.0
_SAME_LOT_METRES = 60.0

_STREET_NOISE = re.compile(
    r"(?i)\b(n|s|e|w|ne|nw|se|sw|north|south|east|west|"
    r"st|street|ave|avenue|blvd|boulevard|dr|drive|rd|road|ln|lane|"
    r"ct|court|pl|place|pkwy|parkway|hwy|highway|lot|garage|parking)\b")


def _street_token(section: str) -> str:
    """The bare street name from our lot string: "500 LEE ST. E." -> "lee"."""
    s = _addr_for_geocode(section) or section
    s = re.sub(r"^\s*\d+[a-z]?\s+", " ", s, flags=re.I)   # leading house number
    return _normalize(_STREET_NOISE.sub(" ", s))


def _geocode_our_lot(our_section: str, city: str = "", state: str = "") -> Optional[tuple[float, float]]:
    """
    Locate OUR lot directly, rather than the venue it happens to sit near.

    This mirrors what the facility flow gets for free from
    geocode_by_spothero_facility_id(): an anchor on our own lot. Lysted rows
    carry no facility id, but they do carry City/State alongside a lot string
    that is usually a street address, and "500 LEE ST E, Charleston, WV"
    resolves where the bare address does not.
    """
    where = ", ".join(p for p in (city, state) if p)
    for addr in _addr_candidates(our_section):
        # With a city/state to hand, never fall back to the bare address: a
        # street name repeats across the country and the unqualified query
        # silently lands in the wrong one. "4811-US 301 N" for a Tampa, FL
        # listing resolved to Fayetteville, NC — a confident anchor 600 miles
        # from the lot, which is worse than having no anchor at all.
        queries = [f"{addr}, {where}"] if where else [addr]
        for q in queries:
            coords = _geocode(q)
            if coords:
                return coords
    return None


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


def _geocode_venue(event_venue: str, our_section: str,
                   city: str = "", state: str = "") -> Optional[tuple[float, float]]:
    clean_venue = _clean_venue(event_venue) if event_venue else ""
    is_generic = _normalize(our_section) in ("generaladmission", "generalparking", "")
    where = ", ".join(p for p in (city, state) if p)

    if not is_generic and our_section:
        cleaned = _strip_brand(our_section)
        q = f"{cleaned}, near {clean_venue}" if clean_venue else cleaned
        coords = _geocode(q)
        if coords:
            return coords

    if clean_venue:
        # Progressively looser forms. OpenStreetMap often knows a venue under
        # its parent complex or an older name: "Highland Festival Grounds at
        # Kentucky Exposition Center" is unknown but "Kentucky Exposition
        # Center" resolves, and city/state disambiguates common venue names.
        candidates = [clean_venue]
        if where:
            candidates.insert(0, f"{clean_venue}, {where}")
        if " at " in clean_venue.lower():
            tail = re.split(r"(?i)\s+at\s+", clean_venue, maxsplit=1)[-1].strip()
            if tail:
                candidates += ([f"{tail}, {where}"] if where else []) + [tail]
        for q in candidates:
            coords = _geocode(q)
            if coords:
                return coords

    return None


# ---------------------------------------------------------------------------
# SpotHero — exact spots_left + capacity
# ---------------------------------------------------------------------------

def _spothero_event_search(query: str) -> list:
    resp = _throttled_get(
        _spothero_throttle_obj, f"{_SPOTHERO_API}/events/search",
        params={"search_query": query},
        headers=_SPOTHERO_HEADERS,
        timeout=10,
    )
    try:
        return resp.json().get("results", []) if resp is not None and resp.ok else []
    except Exception:
        return []


def _find_spothero_event(event_name: str, event_date: str, event_venue: str) -> Optional[dict]:
    clean_venue = _clean_venue(event_venue)
    clean_name = _clean_event_name(event_name)
    target_dt = _parse_event_dt(event_date) if event_date else None

    def _pick(results: list) -> Optional[dict]:
        for r in results:
            if not _venue_matches(clean_venue, r.get("destination_title", "")):
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

    # Name plus venue first. When SpotHero titles the event differently from
    # our sheet the combined query returns nothing useful, so fall back to
    # searching the venue alone and matching on name and date within it —
    # previously a single miss here dropped the event scoping entirely, and
    # an unscoped search returns markedly less inventory.
    queries = [f"{clean_name} {clean_venue}".strip()]
    if clean_venue:
        queries.append(clean_venue)
    if clean_name:
        queries.append(clean_name)

    seen = set()
    for q in queries:
        if not q or q in seen:
            continue
        seen.add(q)
        hit = _pick(_spothero_event_search(q))
        if hit:
            return hit
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


def _spothero_window(event_date: str) -> tuple[str, str]:
    """The search window for an event: its start, or tonight, plus five hours."""
    target_dt = _parse_event_dt(event_date) if event_date else None
    starts = target_dt or datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)
    ends = starts + timedelta(hours=5)
    return (starts.strftime("%Y-%m-%dT%H:%M:%S"), ends.strftime("%Y-%m-%dT%H:%M:%S"))


def _pick_our_lot(results: list, our_section: str,
                  our_coords: Optional[tuple[float, float]]) -> Optional[dict]:
    """
    Which of these results is our lot? Position first, text second.

    Coordinates are the stronger evidence: two listings of the same garage
    routinely disagree on wording and even street number, but not on where
    they are. This is the closest the Lysted flow can get to the facility
    flow's exact facility-id comparison (clustering._is_ours), which needs an
    id these rows don't carry.
    """
    if our_coords:
        near = sorted(
            ((_haversine_m(our_coords, c), r)
             for r in results
             for c in [_spot_coords(r)] if c),
            key=lambda pair: pair[0],   # never fall through to comparing the dicts
        )
        token = _street_token(our_section)
        for dist, r in near:
            if dist > _SAME_LOT_METRES:
                break
            if dist <= _EXACT_SPOT_METRES:
                return r
            if len(token) >= 3 and token in _normalize(f"{_spot_addr(r)} {_spot_name(r)}"):
                return r

    for r in results:
        if _section_matches(our_section, _spot_addr(r)) or _section_matches(our_section, _spot_name(r)):
            return r
    for r in results:
        if _lot_name_matches(our_section, _spot_name(r)):
            return r
    return None


def _ring_points(centre: tuple, miles: float, n: int = 6) -> list:
    """n points evenly spaced on a circle of the given radius around centre."""
    lat, lon = centre
    km = max(0.3, miles * 1.609)
    dlat = km / 111.32
    dlon = km / (111.32 * max(0.2, math.cos(math.radians(lat))))
    return [(lat + dlat * math.sin(2 * math.pi * i / n),
             lon + dlon * math.cos(2 * math.pi * i / n)) for i in range(n)]


def _sweep_spothero(centre: tuple, miles: float, starts: str, ends: str,
                    event_id: Optional[str]) -> list:
    """
    Widen a search that SpotHero refuses to widen itself.

    Its transient search returns at most 15 lots however you ask — per_page,
    limit, max_results and radius are all ignored — and they are the 15
    nearest the anchor. A lot three quarters of a mile out is simply never in
    them. Sampling a ring at the distance our own lot string claims brings
    that band into view: around American Airlines Center this turns 15
    reachable lots into 69.
    """
    seen, out = set(), []
    for lat, lon in _ring_points(centre, miles):
        res, err = _search_spothero_transient(lat, lon, starts, ends, event_id,
                                              include_unavailable=True)
        if err:
            continue
        for r in res:
            key = _spot_facility_id(r) or _spot_name(r)
            if key and key not in seen:
                seen.add(key)
                out.append(r)
    return out


def _fetch_spothero_lots(event_name: str, event_venue: str, event_date: str, our_section: str,
                         city: str = "", state: str = "") -> tuple[list, Optional[str]]:
    """Fetch all SpotHero lots near the venue/time. Returns (results, error)."""
    if not event_venue and not our_section:
        return [], "no location data"

    sh_event = _find_spothero_event(event_name, event_date, event_venue)

    if sh_event:
        lat, lon = sh_event["lat"], sh_event["lon"]
        starts_str = sh_event["starts"]
        ends_str = sh_event["ends"]
        event_id_param = sh_event["event_id"]
    else:
        # Venue first, then our own lot. A lot we can place is a usable anchor
        # even when the venue name means nothing to OpenStreetMap.
        coords = _geocode_venue(event_venue, our_section, city, state) \
            or _geocode_our_lot(our_section, city, state)
        if not coords:
            return [], "could not geocode venue"
        lat, lon = coords
        starts_str, ends_str = _spothero_window(event_date)
        event_id_param = None

    # Keep sold-out lots. SpotHero hides them by default, which is fatal here:
    # a lot that has sold out is exactly what this scan looks for, and hiding
    # it makes our own listing look absent instead ("our lot not found among
    # nearby results"). Our "105 NE 3RD AVE" in Miami was reported missing for
    # this reason while SpotHero was listing it, sold out, 11m away. The
    # facility flow already searches this way (fetch_spothero_lots_near).
    return _search_spothero_transient(lat, lon, starts_str, ends_str, event_id_param,
                                      include_unavailable=True)


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


def _check_spothero(event_name: str, event_venue: str, event_date: str, our_section: str,
                    city: str = "", state: str = "") -> dict:
    base = {"platform": "spothero", "is_available": None, "price": None,
            "spots_left": None, "capacity": None, "percent_remaining": None,
            "availability_status": None, "scarcity_level": "unknown", "error": None}

    results, err = _fetch_spothero_lots(event_name, event_venue, event_date, our_section, city, state)
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
        our_coords = _geocode_our_lot(our_section, city, state)
        if not _anchor_is_plausible(our_coords, _geocode_venue(event_venue, "", city, state), our_section):
            logger.debug("Discarding implausible anchor for %r", our_section)
            our_coords = None
        r = _pick_our_lot(results, our_section, our_coords)

        if r is None and our_coords is None:
            # No address to anchor on, so we cannot re-centre the search.
            # Sweep the band our lot says it sits in instead and match on name.
            venue_coords = _geocode_venue(event_venue, our_section, city, state)
            if venue_coords:
                sh_event = _find_spothero_event(event_name, event_date, event_venue)
                if sh_event:
                    starts_str, ends_str = sh_event["starts"], sh_event["ends"]
                    sweep_event_id = sh_event["event_id"]
                else:
                    starts_str, ends_str = _spothero_window(event_date)
                    sweep_event_id = None
                wide = _sweep_spothero(venue_coords, _stated_miles(our_section) or 0.8,
                                       starts_str, ends_str, sweep_event_id)
                if wide:
                    r = _pick_our_lot(wide, our_section, None)

        if r is None and our_coords:
            # The venue-anchored search may simply not reach our lot — it is
            # centred on the venue, and these listings sit up to a mile out.
            # Re-run centred on the lot itself before concluding it is absent.
            # Reuse the event's own window and id when SpotHero knows the
            # event: scoping the search to it returns markedly more inventory
            # (24 lots vs 15 on one Wilmington listing) because event-only
            # lots are otherwise absent.
            sh_event = _find_spothero_event(event_name, event_date, event_venue)
            if sh_event:
                starts_str, ends_str = sh_event["starts"], sh_event["ends"]
                retry_event_id = sh_event["event_id"]
            else:
                starts_str, ends_str = _spothero_window(event_date)
                retry_event_id = None
            nearby, retry_err = _search_spothero_transient(
                our_coords[0], our_coords[1], starts_str, ends_str,
                retry_event_id, include_unavailable=True)
            if not retry_err and nearby:
                r = _pick_our_lot(nearby, our_section, our_coords)

        if r is not None:
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


def list_spothero_lots(event_name: str, event_venue: str, event_date: str, our_section: str = "",
                       city: str = "", state: str = "") -> list[dict]:
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
            # SpotHero's own facility id. Two distinct lots can share a name
            # AND an address (103 Centennial Olympic Park Dr. lists two at
            # different prices), which made "the previous price for this lot"
            # ambiguous and manufactured a permanent phantom spike.
            "lot_id": _spot_facility_id(r),
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
_PW_VENUE_PAGES_LOADED = 0
_PW_VENUE_EXHAUSTED = False
_PW_VENUE_LOCK = threading.Lock()

# ParkWhiz's /venues/ has no search parameter — every one we tried (q, search,
# name, query) is ignored and returns the same default page — so the only way
# to resolve a venue name to an id is to page through the list. It is huge
# (still returning rows at 40,000) and unsorted by relevance: Red Hat
# Amphitheater is on page 57, Kia Forum on page 84, Levi's Stadium on page
# 105. The old fixed 24-page stop saw 2,400 of them, so most venues resolved
# to nothing and every event there failed as "event not found on ParkWhiz".
#
# Pages are fetched lazily and kept for the life of the process: common
# venues are found in the first few pages and cost nothing extra, and only a
# miss pays to extend the cache.
_PW_VENUE_FIRST_PAGES = 24
_PW_VENUE_MAX_PAGES = 400


def _load_pw_venue_pages(target_pages: int) -> list:
    """Extend the cached venue list to at least target_pages, then return it."""
    global _PW_VENUE_PAGES_LOADED, _PW_VENUE_EXHAUSTED
    target_pages = min(target_pages, _PW_VENUE_MAX_PAGES)
    with _PW_VENUE_LOCK:
        while not _PW_VENUE_EXHAUSTED and _PW_VENUE_PAGES_LOADED < target_pages:
            page = _PW_VENUE_PAGES_LOADED + 1
            resp = _throttled_get(_parkwhiz_throttle_obj, f"{_PARKWHIZ_API}/venues/",
                                  params={"per_page": 100, "page": page},
                                  headers=_PARKWHIZ_HEADERS, timeout=10)
            if resp is None:
                break  # transient — stop here, retry on the next scan
            try:
                batch = resp.json() if resp.ok and isinstance(resp.json(), list) else []
            except Exception:
                break
            if not batch:
                _PW_VENUE_EXHAUSTED = True
                break
            _PW_VENUE_CACHE.extend(batch)
            _PW_VENUE_PAGES_LOADED = page
        return _PW_VENUE_CACHE


def _get_pw_venues() -> list:
    return _load_pw_venue_pages(_PW_VENUE_FIRST_PAGES)


# Shortest normalized overlap that counts as naming the same venue. Without a
# floor, ParkWhiz's short entries swallow everything by substring: "Masa"
# matched "Thomas and Mack Center" ("tho-masa-ndmackcenter"), "Arena" matched
# "Simmons Bank Arena", "Heat" matched "Acrisure Amphitheater". An exact name
# is always accepted, which keeps genuinely short ones like "Stage AE".
_PW_VENUE_MIN_OVERLAP = 8


def _venue_in_place(v: dict, city: str, state: str) -> bool:
    """Is this catalogue entry in the city/state our listing names?"""
    if not state:
        return False
    if _normalize(v.get("state") or "") != _normalize(state):
        return False
    if city and _normalize(v.get("city") or "") != _normalize(city):
        return False
    return True


def _best_pw_venue(norm_v: str, venues: list) -> Optional[int]:
    best_id, best_score = None, 0
    for v in venues:
        vn = _venue_key(v.get("name", ""))
        if not vn:
            continue
        if vn == norm_v:
            return v["id"]
        if vn in norm_v or norm_v in vn:
            score = min(len(vn), len(norm_v))
            if score >= _PW_VENUE_MIN_OVERLAP and score > best_score:
                best_score, best_id = score, v["id"]
    return best_id


def _best_pw_venue_in_place(our_venue: str, norm_v: str, venues: list,
                            city: str, state: str) -> Optional[int]:
    """
    Match within the listing's own city, where loose matching is safe.

    Nationally, a short or generic name has to be rejected — "Arena" would
    otherwise swallow "Simmons Bank Arena", and a "Performing Arts Center" in
    the wrong state would answer for ours. Inside one city there are only a
    handful of venues, so word overlap is enough to identify the right one and
    catches the names the strict pass cannot: differing punctuation, a
    sponsor's name added or dropped, "Arena" vs "Arena at X".
    """
    local = [v for v in venues if _venue_in_place(v, city, state)]
    if not local:
        return None
    best_id, best_score = None, 0
    for v in local:
        vn = _venue_key(v.get("name", ""))
        if not vn:
            continue
        if vn == norm_v:
            return v["id"]
        if (vn in norm_v or norm_v in vn) and min(len(vn), len(norm_v)) >= 5:
            score = min(len(vn), len(norm_v))
            if score > best_score:
                best_score, best_id = score, v["id"]
    if best_id is not None:
        return best_id
    for v in local:
        if _lot_name_matches(our_venue, v.get("name", "")):
            return v["id"]
    return None


def _find_parkwhiz_venue_id(event_venue: str, city: str = "", state: str = "") -> Optional[int]:
    norm_v = _venue_key(event_venue)
    if not norm_v:
        return None
    for venues in (_get_pw_venues(), _load_pw_venue_pages(_PW_VENUE_MAX_PAGES)):
        vid = (_best_pw_venue(norm_v, venues)
               or _best_pw_venue_in_place(event_venue, norm_v, venues, city, state))
        if vid is not None:
            return vid
    return None


def _find_parkwhiz_event_id(event_name: str, event_date: str, event_venue: str,
                             coords: Optional[tuple[float, float]] = None,
                             city: str = "", state: str = "") -> Optional[int]:
    target_dt = _parse_event_dt(event_date) if event_date else None
    clean_name = _clean_event_name(event_name)

    venue_id = _find_parkwhiz_venue_id(event_venue, city, state)
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
            if not _event_name_matches(clean_name, ev.get("name", "")):
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
        if not _event_name_matches(clean_name, ev.get("name", "")):
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


def _pick_our_pw_lot(lots: list, our_section: str,
                     our_coords: Optional[tuple[float, float]]) -> Optional[dict]:
    """_pick_our_lot's counterpart for ParkWhiz quotes, which carry their own
    entrance coordinates. Same rule: position first, then text."""
    if our_coords:
        near = sorted(((_haversine_m(our_coords, l["coords"]), l)
                       for l in lots if l.get("coords")),
                      key=lambda pair: pair[0])
        token = _street_token(our_section)
        for dist, l in near:
            if dist > _SAME_LOT_METRES:
                break
            if dist <= _EXACT_SPOT_METRES:
                return l
            if len(token) >= 3 and token in _normalize(f"{l.get('address','')} {l.get('name','')}"):
                return l
    for l in lots:
        if _section_matches(our_section, l["name"]) or _section_matches(our_section, l["address"]):
            return l
    for l in lots:
        if _lot_name_matches(our_section, f'{l.get("name","")} {l.get("address","")}'):
            return l
    return None


def _fetch_parkwhiz_lots(event_name: str, event_date: str, event_venue: str, our_section: str,
                         city: str = "", state: str = "",
                         anchor_coords: Optional[tuple[float, float]] = None) -> tuple[list, Optional[str]]:
    """Fetch all ParkWhiz lot quotes for this event. Returns (lots, error)."""
    if not event_name or not event_venue:
        return [], "missing event info"

    coords = _geocode_venue(event_venue, our_section, city, state) \
        or _geocode_our_lot(our_section, city, state)
    pw_event_id = _find_parkwhiz_event_id(event_name, event_date, event_venue, coords, city, state)
    if pw_event_id is None:
        return [], "event not found on ParkWhiz"

    # The event_id already pins the event; the coordinates only set the search
    # bounds (~2km). Centring them on our own lot is what brings a lot a mile
    # out from the venue inside the box.
    if anchor_coords is not None:
        coords = anchor_coords
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


def _check_parkwhiz(event_name: str, event_date: str, event_venue: str, our_section: str,
                    city: str = "", state: str = "") -> dict:
    base = {"platform": "parkwhiz", "is_available": None, "price": None,
            "spots_left": None, "capacity": None, "percent_remaining": None,
            "availability_status": None, "scarcity_level": "unknown", "error": None}

    lots, err = _fetch_parkwhiz_lots(event_name, event_date, event_venue, our_section, city, state)
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
        our_coords = _geocode_our_lot(our_section, city, state)
        if not _anchor_is_plausible(our_coords, _geocode_venue(event_venue, "", city, state), our_section):
            our_coords = None
        l = _pick_our_pw_lot(lots, our_section, our_coords)

        if l is None and our_coords:
            # The quote bounds are ~2km around the venue; our lot can sit
            # outside them. Re-ask centred on the lot, still scoped to the
            # same event.
            retry, retry_err = _fetch_parkwhiz_lots(event_name, event_date, event_venue,
                                                    our_section, city, state,
                                                    anchor_coords=our_coords)
            if not retry_err and retry:
                l = _pick_our_pw_lot(retry, our_section, our_coords)

        if l is not None:
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


def list_parkwhiz_lots(event_name: str, event_date: str, event_venue: str, our_section: str = "",
                       city: str = "", state: str = "") -> list[dict]:
    """
    Return every ParkWhiz lot found for this event, each with its own
    availability status — for events with multiple competing lots.
    """
    lots, err = _fetch_parkwhiz_lots(event_name, event_date, event_venue, our_section, city, state)
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

def check_scarcity(event_name: str, event_date: str, event_venue: str, our_section: str,
                   city: str = "", state: str = "") -> dict:
    """Check SpotHero and ParkWhiz scarcity in parallel for one of our events."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_spothero = pool.submit(_check_spothero, event_name, event_venue, event_date, our_section, city, state)
        f_parkwhiz = pool.submit(_check_parkwhiz, event_name, event_date, event_venue, our_section, city, state)
        return {
            "spothero": f_spothero.result(),
            "parkwhiz": f_parkwhiz.result(),
        }


def list_event_lots(event_name: str, event_date: str, event_venue: str, our_section: str = "",
                    city: str = "", state: str = "") -> list[dict]:
    """
    Return every competing lot (SpotHero + ParkWhiz combined) found for this
    event, each with its own spots_left/status — powers the per-event
    multi-lot breakdown in the dashboard.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_spothero = pool.submit(list_spothero_lots, event_name, event_venue, event_date, our_section, city, state)
        f_parkwhiz = pool.submit(list_parkwhiz_lots, event_name, event_date, event_venue, our_section, city, state)
        return f_spothero.result() + f_parkwhiz.result()
