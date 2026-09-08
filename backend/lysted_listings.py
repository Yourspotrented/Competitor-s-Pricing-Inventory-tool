"""
Our active listings from a Lysted inventory export — the CSV the listing team
exports and uploads — as the replacement for reachpro.fetch_all_active_listings.

Why (2026-09-04 call): "the same logic, the same tool that you have for
ReachPro, but the only difference now is simply that you're going to be using
an exported CSV file." Lysted is a different selling platform; we're new with
them and Chibuikem doesn't want to block on negotiating API access, so the
export is the deliberate interim.

Two halves:
  save_upload(filename, content)   parse + persist one export (the upload)
  load_active_listings()           the latest upload's live listings, in
                                   exactly the dict shape
                                   fetch_all_active_listings returns, so
                                   orchestrator.run_scan consumes them unchanged

Identity — the export has no listing id or event id (verified against a real
export: 21 columns, none is an id). Event Name + Event Date + Section was
unique across all 89 rows of that file, so it stands in as the listing key.
It breaks if someone edits the Section text between exports ("0.5 MILES" vs
"0.5 MI AWAY" both already appear), so it's a stopgap: ask Lysted for an id
column if history keeps resetting.

"Active" — Status == ACTIVE and Broadcast == Y. READY / non-broadcast rows
are stored but not checked, so nobody is told to deactivate a listing that
was never live.

Each upload is treated as the full current inventory: the latest upload IS
the active set, and older uploads stay in the table only as history.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from database import LystedListing, LystedUpload, create_tables, get_session

logger = logging.getLogger(__name__)

SOURCE = "lysted"

REQUIRED_COLUMNS = ("Event Name", "Event Date", "Venue", "Section", "Status", "Broadcast")

# Lysted writes "2026-09-12 7:01pm"; ReachPro (and scarcity_check._parse_event_dt)
# speak ISO "2026-09-12T19:01:00". Normalise on the way in, or every SpotHero /
# ParkWhiz event lookup silently fails to date-match.
_DATE_FORMATS = (
    "%Y-%m-%d %I:%M%p", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
    "%m/%d/%Y %I:%M%p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y",
)


def parse_lysted_date(raw: Optional[str]) -> Optional[str]:
    """'2026-09-12 7:01pm' -> '2026-09-12T19:01:00'; None if unparseable."""
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s.upper(), fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def clean_event_name(name: str) -> str:
    """
    'Ella Langley Parking' -> 'Ella Langley'. Lysted appends " Parking" to
    every event name; the buying platforms know the event by the act, so the
    suffix only pollutes the search query. The raw name is kept alongside.
    """
    return re.sub(r"(?i)\s+parking(?:\s+pass(?:es)?)?(?:\s+only)?\s*$", "", name or "").strip()


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _to_float(v: Any) -> Optional[float]:
    try:
        s = str(v).replace("$", "").replace(",", "").strip()
        return float(s) if s and s.upper() != "N/A" else None
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None else None


def is_live(status: Optional[str], broadcast: Optional[str]) -> bool:
    return (status or "").strip().upper() == "ACTIVE" and (broadcast or "").strip().upper() == "Y"


def parse_lysted_csv(content: bytes | str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Parse an export into normalised row dicts. Returns (rows, warnings).
    Raises ValueError if required columns are missing — that's a wrong file,
    not a partially-usable one.
    """
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    # newline='' is the csv module's documented requirement: Public Notes
    # carries embedded newlines inside quotes, and without it a quoted
    # line break is mis-split and later columns shift into each other.
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        raise ValueError(f"not a Lysted inventory export — missing column(s): {', '.join(missing)}")

    rows: List[Dict[str, Any]] = []
    warnings: List[str] = []
    seen_keys: Dict[str, int] = {}

    for i, raw in enumerate(reader, start=2):  # header is line 1
        g = lambda k: (raw.get(k) or "").strip()
        event_name_raw = g("Event Name")
        section = g("Section")
        if not event_name_raw or not section:
            warnings.append(f"line {i}: missing event name or section — skipped")
            continue

        date_raw = g("Event Date")
        date_iso = parse_lysted_date(date_raw)
        if date_raw and not date_iso:
            warnings.append(f"line {i}: could not parse event date {date_raw!r} — kept raw")

        listing_key = f"{SOURCE}:{_norm(event_name_raw)}|{date_iso or _norm(date_raw)}|{_norm(section)}"
        event_key = f"{SOURCE}:{_norm(event_name_raw)}|{date_iso or _norm(date_raw)}|{_norm(g('Venue'))}"
        if listing_key in seen_keys:
            warnings.append(f"line {i}: duplicate of line {seen_keys[listing_key]} "
                            f"(same event, date and section) — both kept, but they share one identity")
        else:
            seen_keys[listing_key] = i

        status, broadcast = g("Status"), g("Broadcast")
        rows.append({
            "listing_key": listing_key,
            "event_key": event_key,
            "is_active": is_live(status, broadcast),
            "username": g("Username") or None,
            "quantity": _to_int(g("Quantity")),
            "status": status or None,
            "broadcast": broadcast or None,
            "event_name": clean_event_name(event_name_raw),
            "event_name_raw": event_name_raw,
            "event_date": date_iso or (date_raw or None),
            "event_date_raw": date_raw or None,
            "venue": g("Venue") or None,
            "city": g("City") or None,
            "state": g("State") or None,
            "section": section,
            "row": g("Row") or None,
            "seats": g("Seats") or None,
            "public_notes": (g("Public Notes") or None),
            "total_cost": _to_float(g("Total Cost")),
            "list_price": _to_float(g("List")),
            "inhand_date": g("Inhand Date") or None,
        })

    return rows, warnings


def save_upload(filename: str, content: bytes | str) -> Dict[str, Any]:
    """Parse and persist one export. Returns a summary for the UI."""
    rows, warnings = parse_lysted_csv(content)
    create_tables()
    db = get_session()
    try:
        upload = LystedUpload(
            filename=filename,
            row_count=len(rows),
            active_count=sum(1 for r in rows if r["is_active"]),
            skipped_count=sum(1 for r in rows if not r["is_active"]),
            warnings="\n".join(warnings)[:2000] or None,
        )
        db.add(upload)
        db.flush()  # get upload.id
        for r in rows:
            db.add(LystedListing(upload_id=upload.id, **r))
        db.commit()
        summary = {
            "upload_id": upload.id,
            "filename": filename,
            "uploaded_at": upload.uploaded_at.isoformat() if upload.uploaded_at else None,
            "row_count": upload.row_count,
            "active_count": upload.active_count,
            "skipped_count": upload.skipped_count,
            "warnings": warnings,
        }
    finally:
        db.close()
    logger.info("Lysted upload saved: %s", summary)
    return summary


def get_latest_upload(db=None) -> Optional[LystedUpload]:
    own = db is None
    if own:
        db = get_session()
    try:
        return db.query(LystedUpload).order_by(LystedUpload.uploaded_at.desc(), LystedUpload.id.desc()).first()
    finally:
        if own:
            db.close()


def load_active_listings(db=None) -> List[Dict[str, Any]]:
    """
    The latest upload's live listings in fetch_all_active_listings' shape:
      reachpro_listing_id, reachpro_event_id, event_name, event_venue,
      event_date, section, our_price, region  (+ quantity, source)
    The "reachpro_*" keys are the orchestrator's field names — they're just
    the listing / event identity, and here they hold the synthetic keys.
    """
    own = db is None
    if own:
        db = get_session()
    try:
        upload = get_latest_upload(db)
        if upload is None:
            return []
        rows = (
            db.query(LystedListing)
            .filter(LystedListing.upload_id == upload.id, LystedListing.is_active == True)  # noqa: E712
            .order_by(LystedListing.event_date, LystedListing.event_name)
            .all()
        )
        return [
            {
                "reachpro_listing_id": r.listing_key,
                "reachpro_event_id": r.event_key,
                "event_name": r.event_name or r.event_name_raw or "",
                "event_venue": r.venue or "",
                "event_date": r.event_date or "",
                "section": r.section or "",
                "our_price": r.list_price,
                "region": r.state,
                "quantity": r.quantity,
                "source": SOURCE,
            }
            for r in rows
        ]
    finally:
        if own:
            db.close()
