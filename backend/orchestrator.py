"""
Orchestrator: fetch our active listings from ReachPro, check competitor
scarcity (SpotHero + ParkWhiz) for each, persist results, and detect
significant per-lot price increases vs. each lot's previous reading.

Can be run as a script:
  python orchestrator.py                       # scan all regions
  python orchestrator.py --region south_east
  python orchestrator.py --event-limit 50       # cap for a quick test run
"""
from __future__ import annotations

import argparse
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from database import ScarcityCheck, LotCheck, PriceSpike, create_tables, get_session
from reachpro import fetch_all_active_listings, REGION_STATE_IDS
from checkers.scarcity_check import check_scarcity, list_event_lots

# A lot's current price must be at least this much higher than its previous
# reading to count as a "significant increase" worth flagging.
SPIKE_THRESHOLD_PERCENT = 20.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

_run_lock = threading.Lock()
_current_run: Optional[Dict[str, Any]] = None
_stop_requested = threading.Event()


def get_current_run() -> Optional[Dict[str, Any]]:
    with _run_lock:
        return dict(_current_run) if _current_run else None


def request_stop() -> bool:
    """Signal the running scan to stop after its current listing. Returns True if a scan was running."""
    current = get_current_run()
    if not current or current.get("status") != "running":
        return False
    _stop_requested.set()
    return True


def _update_run(**kwargs: Any) -> None:
    with _run_lock:
        if _current_run is not None:
            _current_run.update(kwargs)


def run_scan(
    regions: Optional[List[str]] = None,
    event_limit: Optional[int] = None,
    events_per_region: Optional[int] = None,
    source: str = "reachpro",
) -> Dict[str, Any]:
    """
    source: where the list of our active listings comes from.
      "reachpro" — the ReachPro API (regions / events_per_region apply)
      "lysted"   — the latest uploaded Lysted CSV export (see lysted_listings.py);
                   regions don't apply, event_limit still caps the run
    Everything after the fetch — the SpotHero/ParkWhiz checks, persistence,
    per-lot spike detection — is identical for both.
    """
    global _current_run

    if source == "lysted":
        regions = []
    else:
        regions = regions or list(REGION_STATE_IDS.keys())
    _stop_requested.clear()

    with _run_lock:
        _current_run = {
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "regions": regions,
            "source": source,
            "total_listings": 0,
            "checked": 0,
            "limited_or_sold_out": 0,
            "price_spikes": 0,
            "not_found": 0,
            "errors": 0,
        }

    create_tables()
    db = get_session()

    try:
        if source == "lysted":
            from lysted_listings import load_active_listings
            logger.info("Loading active listings from the latest Lysted upload…")
            listings = load_active_listings()
            if event_limit:
                listings = listings[:event_limit]
        else:
            logger.info("Fetching active listings from ReachPro…")
            listings = fetch_all_active_listings(
                regions=regions,
                events_per_region=events_per_region,
                event_limit=event_limit,
            )
        _update_run(total_listings=len(listings))
        logger.info("Total listings to scan: %d", len(listings))

        scarce_count = 0
        error_count = 0
        not_found_count = 0
        spike_count = 0
        scan_started = datetime.now(timezone.utc)
        # Multiple listings (lots) can share the same event — only fetch/compare
        # that event's competitor lots once per scan, not once per listing.
        events_processed_for_lots: set = set()

        for idx, listing in enumerate(listings, 1):
            if _stop_requested.is_set():
                db.commit()
                summary = {
                    "status": "stopped",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "regions": regions,
                    "source": source,
                    "total_listings": len(listings),
                    "checked": idx - 1,
                    "limited_or_sold_out": scarce_count,
                    "price_spikes": spike_count,
                    "not_found": not_found_count,
                    "errors": error_count,
                }
                _update_run(**summary)
                logger.info("Scan stopped by user at %d/%d: %s", idx - 1, len(listings), summary)
                return summary

            try:
                result = check_scarcity(
                    event_name=listing.get("event_name", ""),
                    event_date=listing.get("event_date", ""),
                    event_venue=listing.get("event_venue", ""),
                    our_section=listing.get("section", ""),
                )
            except Exception as exc:
                logger.error("Scarcity check crashed on listing %s: %s",
                             listing.get("reachpro_listing_id"), exc)
                result = {
                    "spothero": {"platform": "spothero", "scarcity_level": "unknown", "error": str(exc)},
                    "parkwhiz": {"platform": "parkwhiz", "scarcity_level": "unknown", "error": str(exc)},
                }

            for platform_result in result.values():
                level = platform_result.get("scarcity_level")
                if level in ("limited", "sold_out"):
                    scarce_count += 1
                if level == "not_found":
                    # A completed check that positively confirmed the competitor
                    # doesn't have this lot/event listed — not a failure.
                    not_found_count += 1
                elif platform_result.get("error"):
                    # Any other error (e.g. "unknown" from a geocode/API failure)
                    # is a real failed check, distinct from a confirmed not_found.
                    error_count += 1

                row = ScarcityCheck(
                    reachpro_listing_id=listing["reachpro_listing_id"],
                    reachpro_event_id=listing["reachpro_event_id"],
                    event_name=listing.get("event_name"),
                    event_venue=listing.get("event_venue"),
                    event_date=listing.get("event_date"),
                    section=listing.get("section"),
                    our_price=listing.get("our_price"),
                    region=listing.get("region"),
                    source=source,
                    platform=platform_result["platform"],
                    is_available=platform_result.get("is_available"),
                    price=platform_result.get("price"),
                    spots_left=platform_result.get("spots_left"),
                    capacity=platform_result.get("capacity"),
                    percent_remaining=platform_result.get("percent_remaining"),
                    availability_status=platform_result.get("availability_status"),
                    scarcity_level=platform_result.get("scarcity_level"),
                    error=platform_result.get("error"),
                    checked_at=datetime.now(timezone.utc),
                )
                db.add(row)

            # Per-lot price capture + spike detection (once per event, not per listing)
            reachpro_event_id = listing["reachpro_event_id"]
            if reachpro_event_id not in events_processed_for_lots:
                events_processed_for_lots.add(reachpro_event_id)
                try:
                    lots = list_event_lots(
                        event_name=listing.get("event_name", ""),
                        event_date=listing.get("event_date", ""),
                        event_venue=listing.get("event_venue", ""),
                        our_section=listing.get("section", ""),
                    )
                except Exception as exc:
                    logger.warning("list_event_lots crashed for event %s: %s", reachpro_event_id, exc)
                    lots = []

                now = datetime.now(timezone.utc)
                for lot in lots:
                    if lot.get("price") is None:
                        continue

                    prev = (
                        db.query(LotCheck)
                        .filter(
                            LotCheck.reachpro_event_id == reachpro_event_id,
                            LotCheck.platform == lot["platform"],
                            LotCheck.lot_name == lot.get("lot_name"),
                            LotCheck.lot_address == lot.get("lot_address"),
                        )
                        .order_by(LotCheck.checked_at.desc())
                        .first()
                    )

                    if prev is not None and prev.price and prev.price > 0:
                        pct_change = (lot["price"] - prev.price) / prev.price * 100
                        if pct_change >= SPIKE_THRESHOLD_PERCENT:
                            spike_count += 1
                            db.add(PriceSpike(
                                reachpro_event_id=reachpro_event_id,
                                event_name=listing.get("event_name"),
                                event_date=listing.get("event_date"),
                                region=listing.get("region"),
                                platform=lot["platform"],
                                lot_name=lot.get("lot_name"),
                                lot_address=lot.get("lot_address"),
                                previous_price=prev.price,
                                current_price=lot["price"],
                                percent_increase=round(pct_change, 1),
                                previous_checked_at=prev.checked_at,
                                detected_at=now,
                            ))

                    db.add(LotCheck(
                        reachpro_event_id=reachpro_event_id,
                        event_name=listing.get("event_name"),
                        event_date=listing.get("event_date"),
                        platform=lot["platform"],
                        lot_name=lot.get("lot_name"),
                        lot_address=lot.get("lot_address"),
                        price=lot.get("price"),
                        spots_left=lot.get("spots_left"),
                        capacity=lot.get("capacity"),
                        percent_remaining=lot.get("percent_remaining"),
                        availability_status=lot.get("availability_status"),
                        scarcity_level=lot.get("scarcity_level"),
                        is_our_lot=lot.get("is_our_lot"),
                        checked_at=now,
                    ))

            elapsed = (datetime.now(timezone.utc) - scan_started).total_seconds()
            rate = idx / elapsed if elapsed > 0 else 0
            eta_seconds = int((len(listings) - idx) / rate) if rate > 0 else None
            _update_run(checked=idx, limited_or_sold_out=scarce_count, errors=error_count,
                        price_spikes=spike_count, not_found=not_found_count, eta_seconds=eta_seconds)
            if idx % 20 == 0:
                db.commit()
                logger.info("Progress: %d/%d listings scanned", idx, len(listings))

        db.commit()

        summary = {
            "status": "completed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "regions": regions,
            "source": source,
            "total_listings": len(listings),
            "checked": len(listings),
            "limited_or_sold_out": scarce_count,
            "price_spikes": spike_count,
            "not_found": not_found_count,
            "errors": error_count,
        }
        _update_run(**summary)
        logger.info("Scan complete: %s", summary)
        return summary

    except Exception as exc:
        db.rollback()
        logger.error("Scan failed: %s", exc)
        _update_run(status="failed", error=str(exc))
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Competitor Scarcity Finder")
    parser.add_argument("--region", nargs="*", help="Regions to scan (default: all)")
    parser.add_argument("--event-limit", type=int, help="Cap total events scanned")
    parser.add_argument("--events-per-region", type=int, help="Cap events scanned per region")
    args = parser.parse_args()
    run_scan(regions=args.region, event_limit=args.event_limit, events_per_region=args.events_per_region)
