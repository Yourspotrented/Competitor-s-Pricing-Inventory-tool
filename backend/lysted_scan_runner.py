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

from database import LystedSoldOutAlert, ScarcityCheck, get_session
from lysted_listings import SOURCE, get_latest_upload, load_active_listings
from teams_notify import notify_sold_out_listings

logger = logging.getLogger(__name__)

LYSTED_DEFAULT_INTERVAL_HOURS = 3.0   # Chibuikem: "every three hours or every six" — start at the tighter one
PLATFORMS = ("spothero", "parkwhiz")

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


def _run_locked(notify: bool, event_limit: Optional[int]) -> Dict[str, Any]:
    from orchestrator import run_scan  # lazy: orchestrator configures logging at import

    upload = get_latest_upload()
    if upload is None:
        logger.info("Lysted scan skipped — no export has been uploaded yet")
        return {"status": "skipped", "reason": "no_upload", "checked": 0,
                "sold_out_alerts_detected": 0, "notified": False}

    listings = load_active_listings()
    if event_limit:
        listings = listings[:event_limit]

    # Rows this run writes all carry checked_at >= scan_started; anything older
    # is "what we knew before this scan" — that split is the crossing check.
    scan_started = datetime.now(timezone.utc)
    summary = run_scan(source=SOURCE, event_limit=event_limit)

    db = get_session()
    notified = False
    try:
        alerts = detect_sold_out_crossings(db, listings, scan_started)

        if alerts and notify:
            notified = notify_sold_out_listings(alerts, webhook_env_var="LYSTED_TEAMS_WEBHOOK_URL")

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
    }
    logger.info("Lysted scan + sold-out detection complete: %s", out)
    return out
