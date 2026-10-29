"""
The margin check for one scan: each listing's own price against what its
matched lot costs to buy, as read by that same scan.

margin.py holds the rule; this joins it to the data. A selling flow
(stagefront_scan_runner today) calls compute_margins() right after its
run_scan(), with the same listings and scan_started it already has — so only
readings this scan wrote are used, never an older price that may have moved.

Report-only. Nothing here changes a price on any platform.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from database import MarginCheck, ScarcityCheck, get_session
import margin

logger = logging.getLogger(__name__)

# The platforms the team buys from — the client's call (2026-10-28): "start by
# checking SpotHero and ParkWhiz". Way.com is scanned too, but adding it here
# would let a Way price the team doesn't buy at set our floor.
MARGIN_PLATFORMS = ("spothero", "parkwhiz")


def _current_readings(db, listing_keys: List[str], scan_started: datetime,
                      source: str) -> Dict[str, Dict[str, ScarcityCheck]]:
    """{listing_key: {platform: latest row this scan wrote}}, in one query."""
    rows = (
        db.query(ScarcityCheck)
        .filter(ScarcityCheck.source == source,
                ScarcityCheck.checked_at >= scan_started,
                ScarcityCheck.platform.in_(MARGIN_PLATFORMS),
                ScarcityCheck.reachpro_listing_id.in_(listing_keys))
        .order_by(ScarcityCheck.checked_at)
        .all()
    )
    out: Dict[str, Dict[str, ScarcityCheck]] = {}
    for r in rows:   # oldest first, so the latest one per platform wins
        out.setdefault(r.reachpro_listing_id, {})[r.platform] = r
    return out


def compute_margins(db, listings: List[Dict[str, Any]], scan_started: datetime,
                    source: str) -> List[Dict[str, Any]]:
    """One margin decision per listing (see margin.evaluate), with what it was decided from."""
    keys = [l["reachpro_listing_id"] for l in listings]
    current = _current_readings(db, keys, scan_started, source) if keys else {}

    results = []
    for listing in listings:
        key = listing["reachpro_listing_id"]
        by_platform = current.get(key, {})
        readings = [
            {"platform": p, "price": r.price, "is_available": r.is_available,
             "scarcity_level": r.scarcity_level, "quote_kind": r.quote_kind}
            for p, r in by_platform.items()
        ]
        decision = margin.evaluate(listing.get("our_price"), readings)
        results.append({
            "source": source,
            "listing_key": key,
            "event_key": listing.get("reachpro_event_id"),
            "event_name": listing.get("event_name"),
            "event_venue": listing.get("event_venue"),
            "event_date": listing.get("event_date"),
            "section": listing.get("section"),
            "quantity": listing.get("quantity"),
            "passes_in_hand": listing.get("passes_in_hand"),
            "platform_detail": {
                r["platform"]: {k: r[k] for k in ("price", "scarcity_level", "quote_kind")}
                for r in readings
            },
            **decision,
        })
    return results


def _row(r: Dict[str, Any], checked_at: datetime) -> MarginCheck:
    sh = r["platform_detail"].get("spothero", {})
    pw = r["platform_detail"].get("parkwhiz", {})
    return MarginCheck(
        source=r["source"], listing_key=r["listing_key"], event_key=r["event_key"],
        event_name=r["event_name"], event_venue=r["event_venue"], event_date=r["event_date"],
        section=r["section"], quantity=r["quantity"], passes_in_hand=r["passes_in_hand"],
        our_price=r["our_price"], buy_price=r["buy_price"], buy_platform=r["buy_platform"],
        margin=r["margin"], floor_price=r["floor_price"], suggested_price=r["suggested_price"],
        action=r["action"], reason=r["reason"], needs_review=r["needs_review"],
        spothero_price=sh.get("price"), spothero_level=sh.get("scarcity_level"),
        parkwhiz_price=pw.get("price"), parkwhiz_level=pw.get("scarcity_level"),
        parkwhiz_quote_kind=pw.get("quote_kind"),
        checked_at=checked_at,
    )


# Dashboard order: what needs a price change first (biggest shortfall on top),
# then what couldn't be checked, then what's fine.
_ACTION_ORDER = {margin.RAISE: 0, margin.NO_DATA: 1, margin.SKIP: 2, margin.OK: 3}


def latest_margins(db, source: str) -> Dict[str, Any]:
    """The most recent scan's margin rows for one source — every row of a scan shares its checked_at."""
    latest = (db.query(MarginCheck.checked_at)
              .filter(MarginCheck.source == source)
              .order_by(MarginCheck.checked_at.desc())
              .first())
    if latest is None:
        return {"checked_at": None, "counts": {}, "rows": []}

    rows = (db.query(MarginCheck)
            .filter(MarginCheck.source == source, MarginCheck.checked_at == latest[0])
            .all())
    rows.sort(key=lambda r: (_ACTION_ORDER.get(r.action, 9),
                             r.margin if r.margin is not None else 0,
                             r.event_date or ""))
    columns = [c.name for c in MarginCheck.__table__.columns]
    return {
        "checked_at": latest[0].isoformat(),
        "min_margin": margin.MIN_MARGIN_DOLLARS,
        "counts": dict(Counter(r.action for r in rows)),
        "needs_review": sum(1 for r in rows if r.needs_review),
        "rows": [{c: (getattr(r, c).isoformat() if c == "checked_at" and getattr(r, c) else getattr(r, c))
                  for c in columns} for r in rows],
    }


def _inconclusive(action: str, levels: List[Optional[str]]) -> bool:
    """
    A RAISE worked out while a platform's check failed: the floor came from
    whichever platform answered, so the price it names may be wrong. (An OK
    stays OK whatever the failed platform says — a cheaper price only widens
    the margin.)
    """
    return action == margin.RAISE and "unknown" in levels


def _last_known(db, source: str, listing_keys: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    For each listing, from earlier scans:
      "any":        its latest decision other than NO_DATA
      "conclusive": its latest decision that every platform answered for
    A scan where a check failed (SpotHero rate-limits us now and then) must
    not reset what was already reported — comparing against it re-posted an
    unchanged listing every time a check failed for an hour.
    """
    rows = (db.query(MarginCheck.listing_key, MarginCheck.action, MarginCheck.suggested_price,
                     MarginCheck.spothero_level, MarginCheck.parkwhiz_level)
            .filter(MarginCheck.source == source,
                    MarginCheck.action != margin.NO_DATA,
                    MarginCheck.listing_key.in_(listing_keys))
            .order_by(MarginCheck.checked_at)
            .all())
    out: Dict[str, Dict[str, Any]] = {}
    for key, action, suggested, sh_level, pw_level in rows:   # oldest first, so the latest wins
        state = {"action": action, "suggested_price": suggested}
        known = out.setdefault(key, {"any": None, "conclusive": None})
        known["any"] = state
        if not _inconclusive(action, [sh_level, pw_level]):
            known["conclusive"] = state
    return out


def _is_news(result: Dict[str, Any], known: Optional[Dict[str, Any]] = None) -> bool:
    """
    Worth a Teams line? Only when something changed. A listing nobody has
    repriced yet stays under margin scan after scan; repeating it every hour
    would bury the new ones, so it stays on the dashboard instead.

      * conclusive now: news if the last conclusive reading wasn't under
        margin, or the price it needs has moved since;
      * inconclusive now (a platform's check failed): news only if it wasn't
        under margin at all last time — never because the price it names
        moved, since that move is just the failed platform dropping out.
    """
    if result["action"] != margin.RAISE:
        return False
    known = known or {}
    levels = [d.get("scarcity_level") for d in result["platform_detail"].values()]
    if _inconclusive(result["action"], levels):
        last = known.get("any")
        return last is None or last["action"] != margin.RAISE
    last = known.get("conclusive")
    if last is None or last["action"] != margin.RAISE:
        return True
    return last["suggested_price"] != result["suggested_price"]


def run_margin_check(listings: List[Dict[str, Any]], scan_started: datetime,
                     source: str) -> Dict[str, Any]:
    """
    Compute and store this scan's margin decisions. Its own session, and it
    never raises: called after the selling flow's alerts and deactivations
    are already committed, so a failure here costs the margin report for one
    scan and nothing else.
    """
    db = get_session()
    try:
        results = compute_margins(db, listings, scan_started, source)
        previous = _last_known(db, source, [r["listing_key"] for r in results]) if results else {}
        new_under = [r for r in results if _is_news(r, previous.get(r["listing_key"]))]
        checked_at = datetime.now(timezone.utc)
        for r in results:
            db.add(_row(r, checked_at))
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Margin check failed for %s (scan unaffected): %s", source, exc)
        return {"status": "failed", "error": str(exc)}
    finally:
        db.close()

    counts = Counter(r["action"] for r in results)
    summary = {
        "status": "ok",
        "checked": len(results),
        "under_margin": counts[margin.RAISE],
        "ok": counts[margin.OK],
        "no_data": counts[margin.NO_DATA],
        "skipped": counts[margin.SKIP],
        "needs_review": sum(1 for r in results if r["needs_review"]),
        "results": results,
        "new_under": new_under,
    }
    logger.info("Margin check (%s): %d checked, %d under margin, %d ok, %d no data",
                source, summary["checked"], summary["under_margin"], summary["ok"], summary["no_data"])
    return summary
