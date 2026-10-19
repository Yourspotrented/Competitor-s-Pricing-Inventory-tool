"""
Our active listings read from Stagefront's own API — the Stagefront
equivalent of lysted_listings.py. There is no CSV step here: fetch_events()
+ fetch_all_inventory() (stagefront_api.py) already return clean JSON, so
this module's job is just flattening and cleaning that into the shape
orchestrator.run_scan expects.

Identity
--------
Stagefront gives real, stable ids for both the event (Event_ID) and the
listing (Listing_ID) — unlike Lysted's export, nothing here is synthesized.

"Quantity" and "is this listing still live"
--------------------------------------------
QueryEventInventoryAll returns one row per physical pass ("Item"); several
share one Listing_ID. There is no confirmed field yet for "this listing is
actively broadcasting" (Item_Status_ID's legend hasn't been captured — see
docs/PROJECT_KNOWLEDGE.md). Until it has, a pass counts as still-open using
two fields that ARE unambiguous on every sample seen so far: Completed_Date
is empty and On_Hold is false. A listing with zero open passes is treated
the same way Lysted treats a non-broadcasting row — stored, but not scanned
or shown as live.

"Passes in hand"
-----------------
Item.InHand mirrors what ReachPro's own unsold_qty gives the ReachPro flow:
a native "do we already hold this one" signal, read directly from Stagefront
rather than via the Lysted flow's SharePoint spare-spots lookup (which has
nothing to match a Stagefront event against anyway). See
stagefront_scan_runner.py for where this overrides the spare-spots result.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from database import StagefrontListing, StagefrontSync, create_tables, get_session

logger = logging.getLogger(__name__)

SOURCE = "stagefront"

_DATE_FORMATS = (
    "%Y-%m-%d %I:%M%p", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
    "%m/%d/%Y %I:%M%p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y",
)


def clean_event_name(name: str) -> str:
    """'Def Leppard and Goo Goo Dolls Parking' -> 'Def Leppard and Goo Goo Dolls' — same idea as Lysted's clean_event_name."""
    return re.sub(r"(?i)\s+parking(?:\s+pass(?:es)?)?(?:\s+only)?\s*$", "", name or "").strip()


def clean_section(section: Optional[str]) -> str:
    """'323 N Ada St (1.0 miles)' -> '323 N Ada St' — Stagefront's own distance-suffix format."""
    return re.sub(r"\s*\(\s*[\d.]+\s*miles?\s*\)\s*$", "", section or "", flags=re.I).strip()


def _combine_date_time(date_raw: Optional[str], time_raw: Optional[str]) -> Optional[str]:
    """
    '2026-11-12T00:00:00' + '6:00 PM' -> '2026-11-12T18:00:00'.

    Event_Date carries no real time (Stagefront always sends midnight) and
    Event_Time is a separate "6:00 PM"-style string. The scarcity check needs
    one combined timestamp to build its SpotHero/ParkWhiz search window
    around the actual event time, not midnight.
    """
    if not date_raw:
        return date_raw
    day = date_raw[:10]
    if not time_raw:
        return date_raw
    for fmt in ("%I:%M %p", "%I:%M%p"):
        try:
            t = datetime.strptime(time_raw.strip().upper(), fmt).time()
            return f"{day}T{t.strftime('%H:%M:%S')}"
        except ValueError:
            continue
    return date_raw


def _is_open(item: Dict[str, Any]) -> bool:
    return not item.get("Completed_Date") and not item.get("On_Hold")


def _group_by_listing(items: List[Dict[str, Any]]) -> Dict[Any, List[Dict[str, Any]]]:
    groups: Dict[Any, List[Dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(item.get("Listing_ID"), []).append(item)
    return groups


def flatten(events: List[Dict[str, Any]],
            inventory_by_event: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """One row per (event, listing), in the shape save_sync persists."""
    by_event_id = {str(e.get("Event_ID")): e for e in events}
    rows: List[Dict[str, Any]] = []
    for event_id, items in inventory_by_event.items():
        event = by_event_id.get(str(event_id)) or {}
        for listing_id, group in _group_by_listing(items).items():
            if listing_id is None:
                continue
            open_items = [i for i in group if _is_open(i)]
            in_hand_items = [i for i in open_items if i.get("InHand")]
            sample = open_items[0] if open_items else group[0]
            rows.append({
                "stagefront_listing_id": str(listing_id),
                "stagefront_event_id": str(event_id),
                "is_active": len(open_items) > 0,
                "quantity": len(open_items),
                "quantity_total": len(group),
                "passes_in_hand": len(in_hand_items),
                "event_name": clean_event_name(event.get("Event_Name") or ""),
                "event_name_raw": event.get("Event_Name"),
                "event_date": _combine_date_time(event.get("Event_Date"), event.get("Event_Time")),
                "event_time": event.get("Event_Time"),
                "venue": event.get("Venue_Name"),
                "city": event.get("Venue_City"),
                "state": event.get("Venue_State"),
                "cat_desc": event.get("Cat_Desc"),
                "section": clean_section(sample.get("Section")),
                "section_raw": sample.get("Section"),
                "row": sample.get("Row"),
                "item_status_id": sample.get("Item_Status_ID"),
                "price": sample.get("Item_Price"),
            })
    return rows


def save_sync(rows: List[Dict[str, Any]], events_seen: int) -> Dict[str, Any]:
    """Persist one full snapshot as a new sync, which becomes the active set."""
    create_tables()
    db = get_session()
    try:
        sync = StagefrontSync(
            events_seen=events_seen,
            listing_count=len(rows),
            active_count=sum(1 for r in rows if r["is_active"]),
        )
        db.add(sync)
        db.flush()  # get sync.id
        for r in rows:
            db.add(StagefrontListing(sync_id=sync.id, **r))
        db.commit()
        summary = {
            "sync_id": sync.id,
            "synced_at": sync.synced_at.isoformat() if sync.synced_at else None,
            "events_seen": sync.events_seen,
            "listing_count": sync.listing_count,
            "active_count": sync.active_count,
        }
    finally:
        db.close()
    logger.info("Stagefront sync saved: %s", summary)
    return summary


def sync_now() -> Dict[str, Any]:
    """Pull events + inventory from Stagefront's API right now and persist a fresh snapshot. Never raises."""
    import stagefront_api

    try:
        events = stagefront_api.fetch_events()
    except stagefront_api.StagefrontApiUnavailable as exc:
        logger.warning("Stagefront sync skipped: %s", exc)
        return {"status": "unavailable", "reason": str(exc)}

    inventory_by_event = stagefront_api.fetch_all_inventory(events)
    rows = flatten(events, inventory_by_event)
    summary = save_sync(rows, len(events))
    return {"status": "synced", **summary}


def get_latest_sync(db=None) -> Optional[StagefrontSync]:
    own = db is None
    if own:
        db = get_session()
    try:
        return db.query(StagefrontSync).order_by(StagefrontSync.synced_at.desc(), StagefrontSync.id.desc()).first()
    finally:
        if own:
            db.close()


# Same reasoning as lysted_listings.PAST_EVENT_GRACE_HOURS: event dates carry
# no timezone, so a day's grace covers the worst offset without checking
# genuinely past events forever.
PAST_EVENT_GRACE_HOURS = 24


def _is_upcoming(event_date: Optional[str], now_utc: datetime) -> bool:
    raw = (event_date or "")[:19]
    if not raw:
        return True
    cutoff = (now_utc - timedelta(hours=PAST_EVENT_GRACE_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
    return raw >= cutoff


def load_active_listings(db=None) -> List[Dict[str, Any]]:
    """
    The latest sync's open listings, in fetch_all_active_listings' shape —
    see lysted_listings.load_active_listings for the field contract. The
    "reachpro_*" keys are the orchestrator's field names, holding
    Stagefront's own ids here, same convention Lysted uses.
    """
    own = db is None
    if own:
        db = get_session()
    try:
        sync = get_latest_sync(db)
        if sync is None:
            return []
        rows = (
            db.query(StagefrontListing)
            .filter(StagefrontListing.sync_id == sync.id, StagefrontListing.is_active == True)  # noqa: E712
            .order_by(StagefrontListing.event_date, StagefrontListing.event_name)
            .all()
        )
        now_utc = datetime.now(timezone.utc)
        rows = [r for r in rows if _is_upcoming(r.event_date, now_utc)]
        return [
            {
                "reachpro_listing_id": r.stagefront_listing_id,
                "stagefront_listing_id": r.stagefront_listing_id,
                "reachpro_event_id": r.stagefront_event_id,
                "event_name": r.event_name or r.event_name_raw or "",
                "event_venue": r.venue or "",
                "event_date": r.event_date or "",
                "section": r.section or "",
                "our_price": r.price,
                "region": r.state,
                "city": r.city,
                "state": r.state,
                "quantity": r.quantity,
                "quantity_total": r.quantity_total,
                "passes_in_hand": r.passes_in_hand,
                "source": SOURCE,
            }
            for r in rows
        ]
    finally:
        if own:
            db.close()


def _parse_csv_date(raw: Optional[str]) -> Optional[str]:
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
        return s[:19] if len(s) >= 10 else None


def _to_float(v: Any) -> Optional[float]:
    try:
        s = str(v).replace("$", "").replace(",", "").strip()
        return float(s) if s and s.upper() != "N/A" else None
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None else None


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _stable_id(prefix: str, *parts: str) -> str:
    """
    A deterministic id for a CSV row with no Listing/Event ID column of its
    own — the same (event, date, section) always produces the same id.

    This replaces an earlier version that used Python's built-in hash():
    str/tuple hashing is randomized per-process by default (a security
    feature since Python 3.3), so the exact same row produced a different
    "stable" id on every server restart — confirmed by running the same
    input through three separate processes and getting three different
    numbers (2026-10-14). That silently broke identity for any CSV-uploaded
    listing without its own id: the sold-out-crossing detector compares a
    listing's reading against its PREVIOUS reading by this key, so a changed
    key after every restart makes every listing look brand new, the same
    failure lysted_listings.make_keys was written to avoid.

    SHA-256 of the normalized, pipe-joined parts is used instead — stable
    across restarts and processes. Truncated to 16 hex chars so the id still
    fits comfortably inside stagefront_listing_id's VARCHAR(50) alongside
    the prefix, not because 16 chars of SHA-256 needs to be cryptographically
    unique here (collisions are not a security concern for this use).
    """
    key = "|".join(_norm(p) for p in parts)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _extract_val(row: Dict[str, Any], *aliases: str) -> str:
    for a in aliases:
        if a in row and row[a] is not None:
            v = str(row[a]).strip()
            if v:
                return v
    lower_map = {str(k).strip().lower(): k for k in row.keys() if k is not None}
    for a in aliases:
        lk = a.strip().lower()
        if lk in lower_map and row[lower_map[lk]] is not None:
            v = str(row[lower_map[lk]]).strip()
            if v:
                return v
    return ""


def parse_stagefront_csv(content: bytes | str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Parse a Stagefront inventory export (CSV) into normalized row dicts ready for StagefrontListing.
    Accepts both this system's exported Stagefront CSV and Lysted-format CSVs.
    """
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = [str(h).strip() for h in (reader.fieldnames or [])]
    if not fieldnames:
        raise ValueError("Uploaded CSV is empty or has no header.")

    rows: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for i, raw in enumerate(reader, start=2):
        event_name_raw = _extract_val(raw, "Event Name", "Event", "Event Name Raw", "Event_Name", "event_name")
        section_raw = _extract_val(raw, "Section", "Lot", "Section / Lot", "Lot / Section", "section", "section_raw")
        if not event_name_raw and not section_raw:
            continue
        if not event_name_raw or not section_raw:
            warnings.append(f"Line {i}: Missing event name or section/lot — skipped.")
            continue

        date_raw = _extract_val(raw, "Event Date", "Date", "Event_Date", "event_date")
        time_raw = _extract_val(raw, "Event Time", "Time", "Event_Time", "event_time")
        date_iso = _parse_csv_date(date_raw)
        if time_raw and date_iso and "T00:00:00" in date_iso:
            date_iso = _combine_date_time(date_iso, time_raw) or date_iso

        venue = _extract_val(raw, "Venue", "Venue Name", "Venue_Name", "venue")
        city = _extract_val(raw, "City", "Venue City", "Venue_City", "city")
        state = _extract_val(raw, "State", "Venue State", "Venue_State", "state", "Region", "region")
        row_str = _extract_val(raw, "Row", "row")

        qty_val = _to_int(_extract_val(raw, "Quantity", "Qty", "Open Passes", "open_passes", "quantity"))
        total_qty_val = _to_int(_extract_val(raw, "Total Quantity", "quantity_total", "Total Qty", "Total"))
        in_hand_val = _to_int(_extract_val(raw, "Passes In Hand", "passes_in_hand", "InHand", "In Hand", "Inhand"))
        price_val = _to_float(_extract_val(raw, "List Price", "List", "Price", "Item_Price", "Item Price", "price"))

        listing_id = _extract_val(raw, "Listing ID", "stagefront_listing_id", "Listing_ID", "listing_id", "Item_ID")
        event_id = _extract_val(raw, "Event ID", "stagefront_event_id", "Event_ID", "event_id")

        if not listing_id:
            listing_id = _stable_id("sfcsv", event_name_raw, date_iso or date_raw, section_raw)
        if not event_id:
            event_id = _stable_id("sfcsvev", event_name_raw, date_iso or date_raw, venue)

        status_str = _extract_val(raw, "Status", "status").upper()
        broadcast_str = _extract_val(raw, "Broadcast", "broadcast").upper()
        
        is_active = True
        if status_str in ("READY", "INACTIVE", "COMPLETED", "DELETED"):
            is_active = False
        elif broadcast_str in ("N", "NO", "FALSE", "0"):
            is_active = False
        elif qty_val is not None and qty_val <= 0:
            is_active = False

        rows.append({
            "stagefront_listing_id": listing_id,
            "stagefront_event_id": event_id,
            "is_active": is_active,
            "quantity": qty_val if qty_val is not None else 1,
            "quantity_total": total_qty_val if total_qty_val is not None else (qty_val if qty_val is not None else 1),
            "passes_in_hand": in_hand_val if in_hand_val is not None else 0,
            "event_name": clean_event_name(event_name_raw),
            "event_name_raw": event_name_raw,
            "event_date": date_iso or date_raw,
            "event_time": time_raw or None,
            "venue": venue or None,
            "city": city or None,
            "state": state or None,
            "cat_desc": None,
            "section": clean_section(section_raw),
            "section_raw": section_raw,
            "row": row_str or None,
            "item_status_id": None,
            "price": price_val,
        })

    if not rows and not warnings:
        raise ValueError("Not a recognizable inventory CSV. Required columns: Event Name, Section/Lot, Event Date.")

    return rows, warnings


def save_upload(filename: str, content: bytes | str) -> Dict[str, Any]:
    """Parse and persist one Stagefront CSV export. Returns summary for UI."""
    rows, warnings = parse_stagefront_csv(content)
    if not rows:
        raise ValueError("No valid inventory rows found in CSV.")
    unique_events = {r["stagefront_event_id"] for r in rows}
    summary = save_sync(rows, len(unique_events))
    return {
        "status": "uploaded",
        "filename": filename,
        "sync_id": summary["sync_id"],
        "synced_at": summary["synced_at"],
        "listing_count": summary["listing_count"],
        "active_count": summary["active_count"],
        "skipped_count": summary["listing_count"] - summary["active_count"],
        "warnings": warnings,
    }
