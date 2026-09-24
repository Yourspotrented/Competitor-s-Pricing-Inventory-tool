"""
The ReachPro flow's missing half: work out which of our ReachPro listings can
no longer be fulfilled, tell the team, and (once ReachPro grants write access)
act on it.

Why (2026-09-22 call): Chibuikem scored ReachPro "75% complete — we don't
download CSV to upload, we take the data … checking all of that and then
alerting the team", with the missing 25% being deactivation. In this codebase
the gap is wider than that: run_scan(source="reachpro") stores every reading
and the dashboard shows it, but nothing watches for a listing going sold out
and nothing tells anyone. So this module does both halves —

  * detection and alerting, reusing the Lysted flow's crossing logic
    (lysted_scan_runner, which takes a source), so "sold out" means the same
    thing on both platforms and neither re-alerts something already reported;
  * the decision, via listing_actions: a sell-out caps the listing at what we
    can still fulfil rather than always killing it (Max Lawrence: "we have 10,
    so there's literally no point in listing 15").

Acting on the decision is half built (listing_actions.apply_action):

  * DEACTIVATE works. It sends the request ReachPro's own UI sends,
    POST /api/Listing/DeleteMarketplaceListings?listingId=<inventory id> with
    an empty body (captured from the Inventory page, 2026-09-25), which takes
    the listing off its marketplaces and leaves the passes in inventory.
  * REDUCE does not. Changing a listed quantity is a different call and none
    has been captured, so "reduce 15 to 10" is still reported, not done.

Both are gated on REACHPRO_ALLOW_WRITES, off by default and dry-run when off:
this is a live sales platform, and an unlisting is not something to fire on a
guess or a stale reading.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import listing_actions
from database import LystedSoldOutAlert, get_session
from lysted_scan_runner import (collect_price_spikes, detect_low_inventory_crossings,
                                detect_sold_out_crossings, scan_stats)
from teams_notify import notify_lysted_summary

logger = logging.getLogger(__name__)

SOURCE = "reachpro"
WEBHOOK_ENV_VAR = "REACHPRO_TEAMS_WEBHOOK_URL"

_scan_lock = threading.Lock()


def run_and_persist_reachpro_scan(notify: bool = True, event_limit: Optional[int] = None,
                                  regions: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Scan our ReachPro listings, then detect, decide and alert. Never re-alerts.

    Alerting is on by default, as it is for Lysted, but it only actually sends
    where REACHPRO_TEAMS_WEBHOOK_URL is set — so a laptop with it unset scans
    and decides in silence, while the deployed service posts.
    """
    with _scan_lock:
        return _run_locked(notify, event_limit, regions)


def _run_locked(notify: bool, event_limit: Optional[int], regions: Optional[List[str]]) -> Dict[str, Any]:
    from orchestrator import run_scan          # lazy: configures logging at import
    from reachpro import fetch_all_active_listings

    try:
        listings = fetch_all_active_listings(regions=regions, event_limit=event_limit)
    except Exception as exc:
        # A stale REACHPRO_COOKIE is the usual cause, and it is a configuration
        # problem, not a scan failure worth crashing the scheduler over.
        logger.error("ReachPro scan skipped — could not read our listings: %s", exc)
        return {"status": "skipped", "reason": f"reachpro unavailable: {exc}",
                "checked": 0, "sold_out_alerts_detected": 0, "notified": False}

    if not listings:
        logger.info("ReachPro scan skipped — no active listings returned")
        return {"status": "skipped", "reason": "no_active_listings", "checked": 0,
                "sold_out_alerts_detected": 0, "notified": False}

    # Everything this run writes carries checked_at >= scan_started; older rows
    # are "what we knew before", and that split is the crossing check.
    scan_started = datetime.now(timezone.utc)
    summary = run_scan(source=SOURCE, event_limit=event_limit, regions=regions)

    db = get_session()
    notified = pricing_notified = False
    alerts: List[Dict[str, Any]] = []
    spikes: List[Dict[str, Any]] = []
    low_inventory: List[Dict[str, Any]] = []
    try:
        alerts = detect_sold_out_crossings(db, listings, scan_started, source=SOURCE)
        # Inventory in hand comes from ReachPro itself: a purchase order
        # becomes a ticket group, so unsoldQty is what we hold and have not
        # sold. No spreadsheet, and it is already net of sales — better than
        # the spare spots workbook the Lysted flow has to read.
        by_key = {l["reachpro_listing_id"]: l for l in listings}
        for a in alerts:
            listing = by_key.get(a["listing_key"], {})
            in_hand = listing.get("unsold_qty")
            a["source"] = SOURCE
            a["passes_secured"] = listing.get("ticket_count")
            a["passes_left"] = in_hand
            a["decision"] = listing_actions.decide(listing.get("quantity") or a.get("quantity"),
                                                   in_hand, a.get("platform_detail") or {})
        spikes = collect_price_spikes(db, listings, scan_started, source=SOURCE)
        low_inventory = detect_low_inventory_crossings(db, listings, scan_started, source=SOURCE)

        if notify:
            sent = notify_lysted_summary(scan_stats(db, listings, scan_started, source=SOURCE),
                                         alerts, spikes, low_inventory,
                                         webhook_env_var=WEBHOOK_ENV_VAR,
                                         title="📊 ReachPro Scan Summary")
            notified = sent and bool(alerts)
            pricing_notified = sent and bool(spikes or low_inventory)

        columns = {c.name for c in LystedSoldOutAlert.__table__.columns}
        for a in alerts:
            db.add(LystedSoldOutAlert(**{k: v for k, v in a.items() if k in columns},
                                      notified=notified))
        db.commit()
    finally:
        db.close()

    out = {
        **summary,
        "listings": len(listings),
        "sold_out_alerts_detected": len(alerts),
        "deactivate": sum(1 for a in alerts if a["decision"]["action"] == listing_actions.DEACTIVATE),
        "reduce": sum(1 for a in alerts if a["decision"]["action"] == listing_actions.REDUCE),
        "notified": notified,
        "price_spikes_detected": len(spikes),
        "low_inventory_alerts_detected": len(low_inventory),
        "pricing_notified": pricing_notified,
        "writes_enabled": listing_actions.writes_enabled(SOURCE),
    }
    logger.info("ReachPro scan + sold-out detection complete: %s", out)
    return out
