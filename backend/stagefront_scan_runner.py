"""
The Stagefront flow, end to end: sync listings from Stagefront's own API,
check them against the buying platforms (orchestrator.run_scan with
source="stagefront"), and alert on anything that has JUST gone sold-out at
source — reusing the Lysted flow's crossing logic (lysted_scan_runner, which
already takes a source), same way reachpro_scan_runner.py does.

Two things lysted_scan_runner's functions get wrong for a second source, and
how this works around them (same pattern reachpro_scan_runner.py already
uses, not a lysted_scan_runner.py change):
  * detect_sold_out_crossings always stamps the alert dict with its own
    module-level SOURCE ("lysted"), not the `source` argument passed in — so
    every alert here has its "source" overwritten below before it's shown or
    persisted, or it would silently show up as a Lysted alert.
  * It also fills passes_secured/passes_left from the Lysted flow's SharePoint
    spare-spots workbook, which has nothing to match a Stagefront event
    against. Stagefront already knows natively which passes we hold
    (Item.InHand, carried through as passes_in_hand) — same idea as
    ReachPro's unsold_qty — so that overrides the spare-spots result here too.

Deactivation (added 2026-10-14)
--------------------------------
Stagefront's decision rule is deliberately stricter than Lysted/ReachPro's:
a listing is only treated as sold out at source once EVERY checked platform
(today: SpotHero and ParkWhiz — listing_actions.STAGEFRONT_SOLD_OUT_PLATFORMS,
extend that tuple for a future platform, nothing else needs to change) reads
sold_out. A platform that never found the lot doesn't count as agreement —
see listing_actions.buyable_when_all_sold_out. The underlying keep-vs-
deactivate call (don't pull a listing we can still cover from our own
passes_in_hand) is unchanged — same decide(), just given this stricter
buyability rule instead of the default one.

Actually carrying out a deactivation ("Unshare Everywhere") needs its own
browser-session cookie, separate from the Bearer token the read side uses
(stagefront_api.py), and stays off until both STAGEFRONT_ALLOW_WRITES is set
and that cookie is pasted — same dry-run-by-default posture every platform
here launched with.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from database import LystedSoldOutAlert, get_session
import listing_actions
from lysted_scan_runner import (
    collect_price_spikes, detect_low_inventory_crossings, detect_sold_out_crossings, scan_stats,
)
from stagefront_listings import SOURCE, get_latest_sync, load_active_listings
from teams_notify import notify_deactivations, notify_lysted_summary

logger = logging.getLogger(__name__)

STAGEFRONT_DEFAULT_INTERVAL_HOURS = 1.0   # same cadence as Lysted (moved to hourly 2026-10-19)

_scan_lock = threading.Lock()


def run_and_persist_stagefront_scan(notify: bool = True, event_limit: Optional[int] = None) -> Dict[str, Any]:
    with _scan_lock:
        return _run_locked(notify, event_limit)


def _run_locked(notify: bool, event_limit: Optional[int]) -> Dict[str, Any]:
    from orchestrator import run_scan   # lazy: orchestrator configures logging at import
    from stagefront_listings import sync_now

    # Refresh the inventory from Stagefront's API before checking it. If the
    # sync is skipped or fails, the latest stored snapshot is used instead —
    # same "a failed sync never stops a scan" rule as the Lysted flow.
    sync = sync_now()
    if sync.get("status") != "synced":
        logger.info("Stagefront scan using the last stored snapshot (sync %s: %s)",
                    sync.get("status"), sync.get("reason"))

    latest = get_latest_sync()
    if latest is None:
        logger.info("Stagefront scan skipped — no sync has succeeded yet")
        return {"status": "skipped", "reason": "no_sync", "checked": 0,
                "sold_out_alerts_detected": 0, "notified": False, "sync": sync}

    listings = load_active_listings()
    if event_limit:
        listings = listings[:event_limit]

    # Rows this run writes all carry checked_at >= scan_started; anything
    # older is "what we knew before this scan" — that split is the crossing check.
    scan_started = datetime.now(timezone.utc)
    summary = run_scan(source=SOURCE, event_limit=event_limit)

    db = get_session()
    notified = False
    pricing_notified = False
    spikes: list = []
    low_inventory: list = []
    carried: Dict[str, Any] = {"enabled": False, "done": [], "skipped": [], "failed": []}
    try:
        alerts = detect_sold_out_crossings(db, listings, scan_started, source=SOURCE)
        by_key = {l["reachpro_listing_id"]: l for l in listings}
        for a in alerts:
            listing = by_key.get(a["listing_key"], {})
            in_hand = listing.get("passes_in_hand")
            a["source"] = SOURCE
            # Unlike Lysted's listing_key (a synthetic event|date|section
            # string), Stagefront's listing_key already IS the real
            # Listing_ID — carry_out needs it under this name to act on it.
            a["stagefront_listing_id"] = a["listing_key"]
            a["passes_secured"] = listing.get("quantity_total")
            a["passes_left"] = in_hand
            a["decision"] = listing_actions.decide(
                listing.get("quantity") or a.get("quantity"), in_hand, a.get("platform_detail") or {},
                buyable_fn=listing_actions.buyable_when_all_sold_out,
            )
            # Stored directly rather than left to be recomputed later:
            # /api/actions/pending's fallback logic only knows the
            # Lysted/ReachPro passes_left-based rule, not this one.
            a["action"] = a["decision"]["action"]
        spikes = collect_price_spikes(db, listings, scan_started, source=SOURCE)
        low_inventory = detect_low_inventory_crossings(db, listings, scan_started, source=SOURCE)

        # Act on the decisions, if writes are on — always false today, since
        # no deactivation endpoint has been captured for Stagefront yet.
        carried = listing_actions.carry_out(alerts, SOURCE, "stagefront_listing_id")
        if carried["enabled"]:
            logger.info("Stagefront deactivations: %d done, %d skipped, %d failed",
                        len(carried["done"]), len(carried["skipped"]), len(carried["failed"]))
            if notify:
                # Its own dedicated group, not the Lysted/ReachPro shared
                # DEACTIVATION_TEAMS_WEBHOOK_URL one (2026-10-14 call).
                notify_deactivations(carried["done"], carried["failed"], carried["skipped"], source="Stagefront",
                                     webhook_env_var="STAGEFRONT_DEACTIVATION_TEAMS_WEBHOOK_URL")

        if notify:
            sent = notify_lysted_summary(scan_stats(db, listings, scan_started, source=SOURCE), alerts,
                                         spikes, low_inventory,
                                         webhook_env_var="STAGEFRONT_TEAMS_WEBHOOK_URL",
                                         title="📊 Stagefront Scan Summary")
            notified = sent and bool(alerts)
            pricing_notified = sent and bool(spikes or low_inventory)

        # The card carries more per-alert detail than the table stores
        # (platform_detail), so keep only real columns.
        columns = {c.name for c in LystedSoldOutAlert.__table__.columns}
        for a in alerts:
            db.add(LystedSoldOutAlert(**{k: v for k, v in a.items() if k in columns},
                                      notified=notified))
        db.commit()
    finally:
        db.close()

    out = {
        **summary,
        "sync_id": latest.id,
        "listings": len(listings),
        "sold_out_alerts_detected": len(alerts),
        "notified": notified if alerts else False,
        "sync": sync,
        "price_spikes_detected": len(spikes),
        "low_inventory_alerts_detected": len(low_inventory),
        "pricing_notified": pricing_notified,
        "deactivated": len(carried["done"]),
        "deactivation_skipped": len(carried["skipped"]),
        "deactivation_failed": len(carried["failed"]),
        "writes_enabled": carried["enabled"],
    }
    logger.info("Stagefront scan + sold-out detection complete: %s", out)
    return out
