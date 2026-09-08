"""
Load the Events team's facility portfolio — a separate list from the
pricing team's Clusters sheet (see facilities.py), provided by Rex.

The sheet only has two columns, "ID" and "SPOT" (no address, no cluster
label) — reuses facilities.py's loader, which already knows how to derive
a street address from a "<address> - <spot descriptor>" name and handles
the ID/SPOT column names as one of its supported shapes.

179 facilities total; 103 overlap with the pricing team's 159, 76 are new
and weren't tracked before. Scanned as a fully separate pipeline from the
pricing flow — own radius (0.1mi default, per the Events team's own ask),
own DB tables, own Teams channel — not reusing pricing-flow scan results
even for the overlapping facilities, per team decision.

Usage:
    from events_facilities import load_events_facilities
    facilities = load_events_facilities()
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from facilities import load_facilities, _GEOCODE_SUFFIXES

_DEFAULT_CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "Events - Facilities.csv")


def load_events_facilities(csv_path: Optional[str] = None, geocode: bool = True,
                            geocode_suffixes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Same shape/behavior as facilities.load_facilities, defaulted to the Events sheet."""
    return load_facilities(
        csv_path=csv_path or _DEFAULT_CSV_PATH,
        geocode=geocode,
        geocode_suffixes=geocode_suffixes if geocode_suffixes is not None else _GEOCODE_SUFFIXES,
    )
