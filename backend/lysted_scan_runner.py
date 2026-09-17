"""
The Lysted flow, end to end: check the latest upload's live listings against
the buying platforms (orchestrator.run_scan with source="lysted" — the same
scan the ReachPro flow runs), then find listings that have JUST gone sold-out
at source and tell the listing team to deactivate them.

Why "just": a listing that was already sold out last scan was already
alerted. Re-alerting every few hours would bury the new ones, so an alert
fires on the crossing — sold out now on a platform where it wasn't sold out
before (or that had never been checked).
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from database import LystedSoldOutAlert, PriceSpike, ScarcityCheck, get_session
from lysted_listings import SOURCE, get_latest_upload, load_active_listings
from teams_notify import notify_lysted_summary

logger = logging.getLogger(__name__)

LYSTED_DEFAULT_INTERVAL_HOURS = 3.0   # Chibuikem: "every three hours or every six" — start at the tighter one
PLATFORMS = ("spothero", "parkwhiz")

# Same threshold the facility flow uses (facility_scan_runner), so "running
# low" means the same thing in both Teams channels.
LOW_INVENTORY_THRESHOLD_PERCENT = 20.0

_scan_lock = threading.Lock()


def run_and_persist_lysted_scan(notify: bool = True, event_limit: Optional[int] = None) -> Dict[str, Any]:
    with _scan_lock:
        return _run_locked(notify, event_limit)


def _latest_check(db, listing_key: str, platform: str, *, since: Optional[datetime] = None,
                  before: Optional[datetime] = None) -> Optional[ScarcityCheck]:
    q = db.query(ScarcityCheck).filter(
        ScarcityCheck.source == SOURCE,
        ScarcityCheck.reachpro_listing_id == listing_key,
        ScarcityCheck.platform == platform,
    )
    if since is not None:
        q = q.filter(ScarcityCheck.checked_at >= since)
    if before is not None:
        q = q.filter(ScarcityCheck.checked_at < before)
    return q.order_by(ScarcityCheck.checked_at.desc()).first()


def detect_sold_out_crossings(db, listings: List[Dict[str, Any]], scan_started: datetime) -> List[Dict[str, Any]]:
    """
    Which of these listings have JUST gone sold-out at source?

    For each listing and platform, compare the reading this scan wrote
    (checked_at >= scan_started) with the latest one before it. A listing
    alerts when it is sold out now on a platform where it was not sold out
    before — including a platform never checked before, since a first check
    that finds it sold out is still news. Sold out last time and still sold
    out is not a crossing and does not re-alert.
    """
    alerts: List[Dict[str, Any]] = []
    for listing in listings:
        key = listing["reachpro_listing_id"]
        now_sold_out, prev_levels = [], {}
        for platform in PLATFORMS:
            current = _latest_check(db, key, platform, since=scan_started)
            prev = _latest_check(db, key, platform, before=scan_started)
            prev_levels[platform] = prev.scarcity_level if prev else None
            if current is not None and current.scarcity_level == "sold_out":
                now_sold_out.append(platform)
        newly = [p for p in now_sold_out if prev_levels[p] != "sold_out"]
        if not newly:
            continue
        alerts.append({
            "listing_key": key,
            "event_key": listing["reachpro_event_id"],
            "event_name": listing.get("event_name"),
            "event_date": listing.get("event_date"),
            "venue": listing.get("event_venue"),
            "city": None,
            "state": listing.get("region"),
            "section": listing.get("section"),
            "platforms": ", ".join(now_sold_out),
            "quantity": listing.get("quantity"),
            "list_price": listing.get("our_price"),
            "previous_levels": ", ".join(f"{p}: {prev_levels[p] or 'unchecked'}" for p in PLATFORMS),
            "detected_at": scan_started,
        })
    return alerts


def _is_low(check) -> bool:
    """Is this reading 'running low' but not yet gone?"""
    if check is None or check.scarcity_level == "sold_out":
        return False
    if check.percent_remaining is not None:
        return check.percent_remaining < LOW_INVENTORY_THRESHOLD_PERCENT
    # ParkWhiz exposes no count, only a 3-state status — "limited" is all we get.
    return check.scarcity_level == "limited"


def detect_low_inventory_crossings(db, listings: List[Dict[str, Any]],
                                   scan_started: datetime) -> List[Dict[str, Any]]:
    """
    Which listings' backing lots have JUST started running low at source?

    Crossing-based for the same reason as detect_sold_out_crossings: a lot
    that was already low last scan is not news, and re-alerting every three
    hours would bury the ones that just turned. Sold-out is excluded — that
    is the other alert's job, and reporting both would double-count.
    """
    alerts: List[Dict[str, Any]] = []
    for listing in listings:
        key = listing["reachpro_listing_id"]
        for platform in PLATFORMS:
            current = _latest_check(db, key, platform, since=scan_started)
            prev = _latest_check(db, key, platform, before=scan_started)
            if not _is_low(current) or _is_low(prev):
                continue
            alerts.append({
                "listing_key": key,
                "platform": platform,
                "lot_name": listing.get("section"),
                "lot_address": listing.get("section"),
                "spots_left": current.spots_left,
                "capacity": current.capacity,
                "percent_remaining": current.percent_remaining if current.percent_remaining is not None else 0.0,
                "previous_percent_remaining": prev.percent_remaining if prev else None,
                # The facility flow's "near <facility> (<cluster>)" line means
                # nothing here; a Lysted row's context is its event and venue.
                "context_subtitle": " · ".join(
                    x for x in (listing.get("event_name"), listing.get("event_venue")) if x),
                "detected_at": scan_started,
            })
    return alerts


def collect_price_spikes(db, listings: List[Dict[str, Any]],
                         scan_started: datetime) -> List[Dict[str, Any]]:
    """
    The spikes this scan recorded, shaped for the Teams card.

    run_scan already detects and persists them (orchestrator); they were just
    never sent anywhere for Lysted.
    """
    event_keys = {l["reachpro_event_id"] for l in listings}
    if not event_keys:
        return []
    by_event = {}
    for l in listings:
        by_event.setdefault(l["reachpro_event_id"], l)
    rows = (
        db.query(PriceSpike)
        .filter(PriceSpike.detected_at >= scan_started,
                PriceSpike.reachpro_event_id.in_(event_keys))
        .order_by(PriceSpike.percent_increase.desc())
        .all()
    )
    out = []
    for r in rows:
        listing = by_event.get(r.reachpro_event_id, {})
        out.append({
            "platform": r.platform,
            "lot_name": r.lot_name,
            "lot_address": r.lot_address,
            "previous_price": r.previous_price,
            "current_price": r.current_price,
            "percent_increase": r.percent_increase,
            "context_subtitle": " · ".join(
                x for x in (r.event_name or listing.get("event_name"), listing.get("event_venue")) if x),
            "detected_at": r.detected_at,
        })
    return out


def scan_stats(db, listings: List[Dict[str, Any]], scan_started: datetime) -> Dict[str, int]:
    """
    Per-listing totals for the summary card: how many were checked, and how
    many were found on at least one platform (any result but not_found /
    unknown) in this scan.
    """
    keys = {l["reachpro_listing_id"] for l in listings}
    found = set()
    if keys:
        rows = (db.query(ScarcityCheck.reachpro_listing_id, ScarcityCheck.scarcity_level)
                .filter(ScarcityCheck.checked_at >= scan_started,
                        ScarcityCheck.reachpro_listing_id.in_(keys))
                .all())
        found = {k for k, level in rows if level not in (None, "not_found", "unknown")}
    return {"listings": len(listings), "found": len(found), "not_found": len(keys) - len(found)}


def _run_locked(notify: bool, event_limit: Optional[int]) -> Dict[str, Any]:
    from orchestrator import run_scan  # lazy: orchestrator configures logging at import
    from lysted_api import sync_from_api

    # Refresh the inventory from Lysted's API before checking it. Whatever
    # happens here the scan goes ahead: if the sync is skipped or fails, the
    # latest snapshot — an earlier sync or a CSV uploaded by hand — is used.
    api_sync = sync_from_api()
    if api_sync.get("status") != "synced":
        logger.info("Lysted scan using latest stored snapshot (API sync %s: %s)",
                    api_sync.get("status"), api_sync.get("reason"))

    upload = get_latest_upload()
    if upload is None:
        logger.info("Lysted scan skipped — no export has been uploaded yet")
        return {"status": "skipped", "reason": "no_upload", "checked": 0,
                "sold_out_alerts_detected": 0, "notified": False, "api_sync": api_sync}

    listings = load_active_listings()
    if event_limit:
        listings = listings[:event_limit]

    # Rows this run writes all carry checked_at >= scan_started; anything older
    # is "what we knew before this scan" — that split is the crossing check.
    scan_started = datetime.now(timezone.utc)
    summary = run_scan(source=SOURCE, event_limit=event_limit)

    db = get_session()
    notified = False
    pricing_notified = False
    spikes: List[Dict[str, Any]] = []
    low_inventory: List[Dict[str, Any]] = []
    try:
        alerts = detect_sold_out_crossings(db, listings, scan_started)
        spikes = collect_price_spikes(db, listings, scan_started)
        low_inventory = detect_low_inventory_crossings(db, listings, scan_started)

        # One summary card per scan, like the team's Daily Sold Summary:
        # totals, then what to deactivate, then the pricing signal.
        if notify:
            sent = notify_lysted_summary(scan_stats(db, listings, scan_started), alerts,
                                         spikes, low_inventory,
                                         webhook_env_var="LYSTED_TEAMS_WEBHOOK_URL")
            notified = sent and bool(alerts)
            pricing_notified = sent and bool(spikes or low_inventory)

        for a in alerts:
            db.add(LystedSoldOutAlert(**a, notified=notified))
        db.commit()
    finally:
        db.close()

    out = {
        **summary,
        "upload_id": upload.id,
        "listings": len(listings),
        "sold_out_alerts_detected": len(alerts),
        "notified": notified if alerts else False,
        "api_sync": api_sync,
        "price_spikes_detected": len(spikes),
        "low_inventory_alerts_detected": len(low_inventory),
        "pricing_notified": pricing_notified,
    }
    logger.info("Lysted scan + sold-out detection complete: %s", out)
    return out
