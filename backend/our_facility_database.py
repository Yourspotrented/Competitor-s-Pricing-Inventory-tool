"""
Load "our" facility master data from the team's SharePoint FACILITY DATABASE
export (manually downloaded — see README for why this isn't pulled live via
Graph API yet). Columns are read as-is, under their own names, alongside
the existing Notion "Inventory" source (our_inventory.py) — nothing is
merged or reinterpreted between sources until the team confirms which one
is authoritative.

The CSV is a raw Excel export: cp1252-encoded (not UTF-8 — Windows Excel
"Save As CSV" produces this), with two duplicate '# of stall' headers per
platform column (PARKWHIZ, WAY.COM, NEIGHBOR, SPACER each have their own
listed-status + stall-count pair) and ~130 trailing empty columns from
stray formatting in the original sheet — both handled by reading positionally
rather than via csv.DictReader (which silently collapses duplicate headers).

Usage:
    from our_facility_database import load_facility_database
    rows = load_facility_database()   # [{facility_id, facility_name, ...}, ...]
"""
from __future__ import annotations

import csv
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "FACILITY DATABASE(MAIN).csv")
_ENCODING = "cp1252"

# Column positions in the real header row (see module docstring — everything
# past index 21 is trailing empty junk from the Excel export).
_COL = {
    "state": 0, "facility_id": 1, "facility_name": 2, "facility_address": 3,
    "facility_status": 4, "facility_category": 5, "no_of_stalls": 6,
    "monthly_inventory": 7, "parkwhiz_status": 8, "parkwhiz_stalls": 9,
    "way_status": 10, "way_stalls": 11, "neighbor_status": 12, "neighbor_stalls": 13,
    "spacer_status": 14, "spacer_stalls": 15, "gps_coordinates": 16,
    "facility_notes": 17, "revenue_category": 18, "landlord": 19,
}


def _clean(val: str) -> Optional[str]:
    val = (val or "").strip()
    return val if val else None


def _clean_int(val: str) -> Optional[int]:
    val = _clean(val)
    if val is None:
        return None
    try:
        return int(float(val))
    except ValueError:
        return None  # e.g. "N/A" — not a parse error worth logging, just absent


def load_facility_database(csv_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Parse the FACILITY DATABASE CSV export. Returns a list of dicts, one per
    row with a non-empty Facility ID (rows without one — headers, blank
    trailer rows — are skipped):

      { facility_id, facility_name, facility_address, facility_status,
        facility_category, no_of_stalls, monthly_inventory,
        parkwhiz_status, parkwhiz_stalls, way_status, way_stalls,
        neighbor_status, neighbor_stalls, spacer_status, spacer_stalls }

    no_of_stalls is an int when parseable, else None (covers "N/A" cells).
    monthly_inventory is kept as the raw string ("N/A", "9", "0", ...) since
    the source column mixes text and numbers inconsistently — parse at the
    call site if a numeric comparison is needed.
    """
    path = csv_path or _DEFAULT_CSV_PATH
    rows: List[Dict[str, Any]] = []

    with open(path, encoding=_ENCODING, newline="") as f:
        reader = csv.reader(f)
        next(reader, None)  # header row — already known via _COL, not re-derived
        for raw in reader:
            if len(raw) <= _COL["facility_id"]:
                continue
            facility_id = _clean(raw[_COL["facility_id"]])
            if not facility_id:
                continue

            def get(key: str) -> str:
                idx = _COL[key]
                return raw[idx] if idx < len(raw) else ""

            rows.append({
                "facility_id": facility_id,
                "facility_name": _clean(get("facility_name")),
                "facility_address": _clean(get("facility_address")),
                "facility_status": _clean(get("facility_status")),
                "facility_category": _clean(get("facility_category")),
                "no_of_stalls": _clean_int(get("no_of_stalls")),
                "monthly_inventory": _clean(get("monthly_inventory")),
                "parkwhiz_status": _clean(get("parkwhiz_status")),
                "parkwhiz_stalls": _clean_int(get("parkwhiz_stalls")),
                "way_status": _clean(get("way_status")),
                "way_stalls": _clean_int(get("way_stalls")),
                "neighbor_status": _clean(get("neighbor_status")),
                "neighbor_stalls": _clean_int(get("neighbor_stalls")),
                "spacer_status": _clean(get("spacer_status")),
                "spacer_stalls": _clean_int(get("spacer_stalls")),
            })

    logger.info("Loaded %d facility-database rows (from %s)", len(rows), path)
    return rows
