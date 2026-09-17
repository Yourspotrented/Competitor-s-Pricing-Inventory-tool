"""
Passes we have already secured, from the team's "SPARE SPOTS- LYSTED" workbook
on SharePoint — so a sold-out alert can say what it costs us.

Why (Leticia, 2026-09-18): when a lot sells out at source the listing has to be
deactivated, but passes already bought cannot always be refunded, so they sit
in inventory. The alert she asked for reads:

    Kenny Chesney + date + venue
    Lysted listing: 10 passes
    PW: yes/no (how many left)
    Spothero: yes/no (how many left)
    Passes secured: Yes, 6 passes left

Everything but the last line comes from the scan. This module supplies the
last line.

The workbook has one ROW PER PASS, not per event: eight rows for the Nashville
Kenny Chesney date, two of them carrying a USED/CANCELLED DATE, which is her
"we secured 8 passes and already used 2". So "secured" counts rows and "left"
counts rows with that column empty.

Both sheets are read (NEW INVENTORY is the current one, OLD INVENTORY the
history) because an event bought for months ago is still upcoming.

Read-only, and never raises: if SharePoint is unreachable or the sheet is
renamed, the alert simply omits the line rather than failing the scan.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

WORKBOOK_NAME = "SPARE SPOTS- LYSTED.xlsx"
SHEETS = ("NEW INVENTORY", "OLD INVENTORY")
CACHE_TTL_SECONDS = 15 * 60      # a scan takes ~an hour; one fetch per scan is plenty
_GRAPH = "https://graph.microsoft.com/v1.0"

# Excel serial dates count days from this epoch (the 1900 system, including
# its phantom leap day) — "46571" in EVENT DATE is 2027-07-03.
_EXCEL_EPOCH = date(1899, 12, 30)

_cache: Dict[str, Any] = {"fetched_at": None, "rows": []}
_lock = threading.Lock()


def _site_id(headers: Dict[str, str]) -> str:
    host = os.getenv("SHAREPOINT_REPORT_SITE_HOST", "yourspotrented.sharepoint.com").strip()
    path = os.getenv("SHAREPOINT_REPORT_SITE_PATH", "/sites/MaxParkArbLLC").strip()
    resp = requests.get(f"{_GRAPH}/sites/{host}:{path}", headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def _workbook_item_id(headers: Dict[str, str], site_id: str) -> str:
    configured = os.getenv("SPARE_SPOTS_ITEM_ID", "").strip()
    if configured:
        return configured
    resp = requests.get(f"{_GRAPH}/sites/{site_id}/drive/root/search(q='SPARE SPOTS')",
                        headers=headers, timeout=30)
    resp.raise_for_status()
    for item in resp.json().get("value") or []:
        if (item.get("name") or "").strip().lower() == WORKBOOK_NAME.lower():
            return item["id"]
    raise RuntimeError(f"{WORKBOOK_NAME} not found on the site")


def _excel_date(value: Any) -> Optional[str]:
    """Excel serial or a written date -> 'YYYY-MM-DD'. None if unreadable."""
    s = str(value or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{4,6}(\.\d+)?", s):
        try:
            return (_EXCEL_EPOCH + timedelta(days=float(s))).strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _fetch_rows() -> List[Dict[str, Any]]:
    from sharepoint_upload import _get_access_token

    headers = {"Authorization": f"Bearer {_get_access_token()}"}
    site_id = _site_id(headers)
    item_id = _workbook_item_id(headers, site_id)

    rows: List[Dict[str, Any]] = []
    for sheet in SHEETS:
        url = (f"{_GRAPH}/sites/{site_id}/drive/items/{item_id}"
               f"/workbook/worksheets('{sheet}')/usedRange(valuesOnly=true)")
        resp = requests.get(url, headers=headers, timeout=120)
        if not resp.ok:
            logger.warning("Spare spots: sheet %r unreadable (HTTP %s)", sheet, resp.status_code)
            continue
        values = resp.json().get("values") or []
        if not values:
            continue
        header = [str(h).strip() for h in values[0]]
        col = {name: i for i, name in enumerate(header)}
        need = ("EVENT", "EVENT DATE", "EVENT VENUE", "LOCATION", "USED/CANCELLED DATE")
        missing = [c for c in need if c not in col]
        if missing:
            logger.warning("Spare spots: %r is missing column(s) %s — skipped", sheet, missing)
            continue
        for raw in values[1:]:
            get = lambda name: str(raw[col[name]]).strip() if col[name] < len(raw) else ""
            event_date = _excel_date(get("EVENT DATE"))
            location = get("LOCATION")
            if not event_date or not location:
                continue          # blank spacer rows between events
            rows.append({
                "event": get("EVENT"),
                "event_date": event_date,
                "venue": get("EVENT VENUE"),
                "location": location,
                "used": bool(get("USED/CANCELLED DATE")),
                "sheet": sheet,
            })
    logger.info("Spare spots: read %d pass row(s) from %s", len(rows), WORKBOOK_NAME)
    return rows


def load_rows(force: bool = False) -> List[Dict[str, Any]]:
    """Every purchased pass, cached. Returns [] if the workbook can't be read."""
    with _lock:
        fetched_at = _cache["fetched_at"]
        fresh = fetched_at and (datetime.now() - fetched_at).total_seconds() < CACHE_TTL_SECONDS
        if fresh and not force:
            return _cache["rows"]
        try:
            _cache["rows"] = _fetch_rows()
            _cache["fetched_at"] = datetime.now()
        except Exception as exc:
            logger.warning("Spare spots unavailable (%s) — alerts will omit secured passes", exc)
            if _cache["rows"]:
                return _cache["rows"]      # stale beats nothing
            _cache["rows"] = []
        return _cache["rows"]


def _day(event_date: Optional[str]) -> str:
    return (event_date or "")[:10]


def passes_for(event_date: Optional[str], section: str, venue: str = "",
               rows: Optional[List[Dict[str, Any]]] = None) -> Optional[Tuple[int, int]]:
    """
    (secured, left) for one listing, or None when nothing was bought for it.

    Matched on the event's calendar date plus the lot: the sheet writes the lot
    its own way ("Public Square Garage/ 350 Deaderick St (0.5 miles)" against
    our "350 DEADERICK ST. (0.5 MILES)"), so the same address comparison the
    scan uses for SpotHero does the work here. Time of day is ignored — the
    sheet records dates only.
    """
    from checkers.scarcity_check import _section_matches

    day = _day(event_date)
    if not day or not section:
        return None
    rows = load_rows() if rows is None else rows
    matched = [r for r in rows
               if r["event_date"] == day
               and (_section_matches(section, r["location"]) or _section_matches(r["location"], section))]
    if not matched:
        return None
    return len(matched), sum(1 for r in matched if not r["used"])


def describe(event_date: Optional[str], section: str, venue: str = "",
             rows: Optional[List[Dict[str, Any]]] = None) -> str:
    """The alert's line: "Yes, 6 of 8 passes left" / "No passes purchased"."""
    counts = passes_for(event_date, section, venue, rows)
    if counts is None:
        return "no purchases recorded"
    secured, left = counts
    if left == 0:
        return f"all {secured} used or cancelled"
    return f"yes, {left} of {secured} pass{'' if secured == 1 else 'es'} left"
