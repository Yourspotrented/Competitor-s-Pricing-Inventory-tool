"""
Load "our" facility portfolio (static garages/lots — not event-tied) and
geocode each one. Facilities come from a CSV export of the team's Google
Sheet: one row per bookable spot/lot.

Two CSV shapes are supported:
  - The team's real sheet export: "Cluster,Facility Id,Name,Our total inv
    left,Competitor total Inv left,Time period" — no separate address
    column, so the street address is extracted from the "Name" field
    (e.g. "191 Kent St. - Spot #1" -> "191 Kent St.").
  - A simple "id,name,address" CSV with an explicit address column.

Usage:
    from facilities import load_facilities
    facilities = load_facilities()   # [{id, name, address, cluster_label, lat, lon}, ...]
"""
from __future__ import annotations

import csv
import logging
import os
from typing import Any, Dict, List, Optional

from checkers.scarcity_check import _extract_addr, _geocode, geocode_by_spothero_facility_id

logger = logging.getLogger(__name__)

_DEFAULT_CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "Clusters - Sheet1.csv")

# Facilities in this sheet are in the greater Boston area with no city/state
# in their address — appending one of these makes Nominatim geocoding far
# more reliable and disambiguates from same-named streets elsewhere. Most
# resolve under Boston, but a few (e.g. "Kent St.") are actually in
# Brookline, so each candidate suffix is tried in order until one resolves.
_GEOCODE_SUFFIXES = ["Boston, MA", "Brookline, MA"]


def _read_csv(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _row_to_facility(row: Dict[str, str]) -> Optional[Dict[str, str]]:
    """Normalize any supported CSV shape into {id, name, address, cluster_label}."""
    facility_id = (row.get("id") or row.get("Facility Id") or row.get("ID") or "").strip()
    if not facility_id:
        return None

    name = (row.get("name") or row.get("Name") or row.get("SPOT") or "").strip()
    address = (row.get("address") or "").strip()
    if not address:
        # Real sheet has no address column — derive it from the name, which
        # is "<street address> - <spot descriptor>" (e.g. "191 Kent St. -
        # Spot #1"). Reuses the same extraction the SpotHero matcher uses.
        address = _extract_addr(name)

    return {
        "id": facility_id,
        "name": name,
        "address": address,
        "cluster_label": (row.get("Cluster") or "").strip(),
    }


def load_facilities(csv_path: Optional[str] = None, geocode: bool = True,
                     geocode_suffixes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Load our facility portfolio from CSV and (by default) geocode each
    address. Geocoding reuses the same cached-forever Nominatim lookup the
    event scarcity checker uses, so repeat loads are cheap.

    Each candidate suffix in geocode_suffixes is tried in order until one
    resolves (see _GEOCODE_SUFFIXES) — some facilities are in a neighboring
    town, not the default city, so a single hardcoded suffix would either
    fail to geocode them or (worse) silently geocode to the wrong town if
    that town happens to share the street name.

    Returns a list of dicts:
      { id, name, address, cluster_label, lat, lon }
    lat/lon are None if geocoding was skipped or the address couldn't be resolved.
    cluster_label is the sheet's own manual "Cluster" column when present
    (informational only — the radius clustering in clustering.py is what
    actually groups facilities for competitor searches).
    """
    path = csv_path or _DEFAULT_CSV_PATH
    rows = _read_csv(path)
    suffixes = geocode_suffixes if geocode_suffixes is not None else _GEOCODE_SUFFIXES

    facilities: List[Dict[str, Any]] = []
    for row in rows:
        facility = _row_to_facility(row)
        if facility is None:
            continue
        facility["lat"] = None
        facility["lon"] = None

        if geocode:
            # Ask SpotHero for this facility's own coordinates first — our
            # sheet ids ARE SpotHero facility ids, so this is an exact lookup
            # rather than a guess at what the spot name's address resolves to.
            coords = geocode_by_spothero_facility_id(facility["id"])
            if coords:
                facility["lat"], facility["lon"] = coords
            elif facility["address"]:
                candidates = [f"{facility['address']}, {s}" for s in suffixes] if suffixes else [facility["address"]]
                for query in candidates:
                    coords = _geocode(query)
                    if coords:
                        facility["lat"], facility["lon"] = coords
                        break
                else:
                    logger.warning("Could not geocode facility %s (%s): no SpotHero record, tried %r",
                                    facility["id"], facility["name"], candidates)
        facilities.append(facility)

    logger.info("Loaded %d facilities (%d geocoded)", len(facilities),
                sum(1 for f in facilities if f["lat"] is not None))
    return facilities


_our_ids_cache: Optional[set] = None


def load_our_facility_ids(refresh: bool = False) -> set:
    """
    Every facility id we own, across BOTH the pricing sheet and the Events
    sheet — the set used to decide whether a search result is one of our own
    lots rather than a competitor.

    Deliberately spans both portfolios: whether a lot is ours doesn't depend
    on which scan happened to find it, so a facility listed only in the
    pricing sheet must not be counted as a "competitor" by the Events scan,
    and vice versa. Chibuikem's requirement was "anything that is not our
    facility", not "anything not in the sheet we're currently scanning".

    Reads ids only (no geocoding), and caches the result — the sheets don't
    change mid-run. Pass refresh=True after editing a sheet on disk.
    """
    global _our_ids_cache
    if _our_ids_cache is not None and not refresh:
        return _our_ids_cache

    # Lazy import: events_facilities imports this module at module level.
    from events_facilities import _DEFAULT_CSV_PATH as _EVENTS_CSV_PATH

    ids: set = set()
    for path in (_DEFAULT_CSV_PATH, _EVENTS_CSV_PATH):
        try:
            for row in _read_csv(path):
                facility = _row_to_facility(row)
                if facility:
                    ids.add(facility["id"])
        except FileNotFoundError:
            logger.warning("Facility sheet missing, skipped for self-exclusion: %s", path)

    logger.info("Self-exclusion id set: %d facility ids across both portfolios", len(ids))
    _our_ids_cache = ids
    return ids
