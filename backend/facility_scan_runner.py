"""
Orchestrates a full facility-competitor scan: run the radius-clustered
SpotHero/ParkWhiz search, persist per-facility summaries, persist per-lot
price history, detect significant price spikes vs. each lot's previous
reading, and (if configured) notify Teams.

Facility-flow parallel to orchestrator.py, which does the same thing for
ReachPro events. Kept as its own module (not inline in main.py) so both the
manual "Run Facility Scan" button and the background scheduler can call the
same function and get the same persistence/spike/notify behavior.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from database import (
    FacilityCompetitorCheck, FacilityLotCheck, FacilityPriceSpike,
    FacilityLowInventoryAlert, OurInventorySnapshot, OurSpotheroInventorySnapshot, get_session,
)
from clustering import DEFAULT_RADIUS_MILES, run_facility_scan_with_lots
from teams_notify import notify_price_spikes
from clusters_export import build_filled_clusters_workbook
from sharepoint_upload import upload_file_to_sharepoint

# Same threshold as the event-flow's SPIKE_THRESHOLD_PERCENT (orchestrator.py) —
# a lot's current price must be at least this much higher than its own
# previous reading to count as a spike worth flagging.
SPIKE_THRESHOLD_PERCENT = 20.0

# A competitor lot's remaining inventory dropping below this counts as a
# low-inventory alert — matches the call's original framing (low competitor
# supply nearby = demand signal, opportunity to raise our own price).
# SpotHero only: ParkWhiz doesn't expose an exact percent_remaining.
LOW_INVENTORY_THRESHOLD_PERCENT = 20.0

# Serializes scans across their two entry points (the hourly scheduler and
# the manual "Run Facility Scan" button) — without this, both could run at
# once, doubling SpotHero/ParkWhiz request load (the rate-limit/IP-block
# concern) and contending for SQLite writes. Overlapping callers wait their
# turn rather than being rejected; a scan takes ~1 minute.
_scan_lock = threading.Lock()

logger = logging.getLogger(__name__)


def _detect_and_persist_lot_spikes(db, cluster: Dict[str, Any], platform: str,
                                    lots: List[Dict[str, Any]], now: datetime
                                    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    For one cluster's lots on one platform: compare each lot's current price
    and remaining-inventory percent to its own most recent prior
    FacilityLotCheck reading (matched by radius_group_id + platform +
    lot_name + lot_address), flag price spikes and low-inventory crossings,
    persist the new reading either way.

    Low-inventory alerts fire only on a crossing — this reading's
    percent_remaining is below LOW_INVENTORY_THRESHOLD_PERCENT and the
    previous reading (or lack of one) was NOT already below it — so a lot
    sitting at 5% for days doesn't re-alert every hourly scan. SpotHero
    only: ParkWhiz has no exact percent_remaining.

    Returns (spikes, low_inventory_alerts) — both lists of plain dicts,
    ready for their respective notify_*/persist calls.
    """
    spikes: List[Dict[str, Any]] = []
    low_inventory_alerts: List[Dict[str, Any]] = []
    radius_group_id = cluster["radius_group_id"]

    for lot in lots:
        price = lot.get("price")
        percent_remaining = lot.get("percent_remaining")

        prev = (
            db.query(FacilityLotCheck)
            .filter(
                FacilityLotCheck.radius_group_id == radius_group_id,
                FacilityLotCheck.platform == platform,
                FacilityLotCheck.lot_name == lot.get("lot_name"),
                FacilityLotCheck.lot_address == lot.get("lot_address"),
            )
            .order_by(FacilityLotCheck.checked_at.desc())
            .first()
        )

        if price is not None and prev is not None and prev.price and prev.price > 0:
            pct_change = (price - prev.price) / prev.price * 100
            if pct_change >= SPIKE_THRESHOLD_PERCENT:
                spikes.append({
                    "radius_group_id": radius_group_id,
                    "cluster_label": cluster.get("cluster_label"),
                    "radius_miles": cluster.get("radius_miles"),
                    "sample_facility_id": cluster.get("sample_facility_id"),
                    "sample_facility_name": cluster.get("sample_facility_name"),
                    "platform": platform,
                    "lot_name": lot.get("lot_name"),
                    "lot_address": lot.get("lot_address"),
                    "previous_price": prev.price,
                    "current_price": price,
                    "percent_increase": round(pct_change, 1),
                    "previous_checked_at": prev.checked_at,
                    "detected_at": now,
                })

        if percent_remaining is not None and percent_remaining < LOW_INVENTORY_THRESHOLD_PERCENT:
            prev_was_already_low = (
                prev is not None and prev.percent_remaining is not None
                and prev.percent_remaining < LOW_INVENTORY_THRESHOLD_PERCENT
            )
            if not prev_was_already_low:
                low_inventory_alerts.append({
                    "radius_group_id": radius_group_id,
                    "cluster_label": cluster.get("cluster_label"),
                    "radius_miles": cluster.get("radius_miles"),
                    "sample_facility_id": cluster.get("sample_facility_id"),
                    "sample_facility_name": cluster.get("sample_facility_name"),
                    "platform": platform,
                    "lot_name": lot.get("lot_name"),
                    "lot_address": lot.get("lot_address"),
                    "spots_left": lot.get("spots_left"),
                    "capacity": lot.get("capacity"),
                    "percent_remaining": percent_remaining,
                    "previous_percent_remaining": prev.percent_remaining if prev else None,
                    "detected_at": now,
                })

        db.add(FacilityLotCheck(
            radius_group_id=radius_group_id,
            cluster_label=cluster.get("cluster_label"),
            radius_miles=cluster.get("radius_miles"),
            platform=platform,
            lot_name=lot.get("lot_name"),
            lot_address=lot.get("lot_address"),
            price=price,
            spots_left=lot.get("spots_left"),
            capacity=lot.get("capacity"),
            percent_remaining=percent_remaining,
            availability_status=lot.get("availability_status"),
            scarcity_level=lot.get("scarcity_level"),
            checked_at=now,
        ))

    return spikes, low_inventory_alerts


def _build_and_upload_clusters_export(db, facility_results: List[Dict[str, Any]]) -> str:
    """
    Fill Clusters.xlsx with this run's competitor totals + the latest known
    Notion inventory per facility, upload it to SharePoint, and return the
    web link — or "" if either step fails, so a export/upload problem never
    blocks the Teams notification itself from going out.
    """
    try:
        inv_rows = db.query(OurInventorySnapshot).all()
        # Latest snapshot per facility_id (checked_at desc, first wins).
        latest_inv: Dict[str, int] = {}
        for r in sorted(inv_rows, key=lambda x: x.checked_at, reverse=True):
            latest_inv.setdefault(r.facility_id, r.inventory)

        export_rows = [
            {
                "facility_id": r["facility_id"],
                "competitor_total_inv_left": r.get("competitor_total_inv_left"),
                "our_inventory": latest_inv.get(r["facility_id"]),
            }
            for r in facility_results
        ]
        xlsx_bytes = build_filled_clusters_workbook(export_rows)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        result = upload_file_to_sharepoint(f"Clusters_{timestamp}.xlsx", xlsx_bytes)
        return result.get("web_url", "")
    except Exception as exc:
        logger.error("Clusters.xlsx export/upload failed (notification will still send without it): %s", exc)
        return ""


def run_and_persist_facility_scan(radius_miles: float = DEFAULT_RADIUS_MILES,
                                   start_hour: int = 10, end_hour: int = 22,
                                   csv_path: Optional[str] = None,
                                   notify: bool = True) -> Dict[str, Any]:
    """
    Full facility scan: search, persist facility summaries + per-lot price
    history, detect price spikes and low-inventory crossings, notify Teams
    (one bundled message covering both) if any were found and notify=True.

    Returns a summary dict:
      { facilities_checked, clusters_checked, spikes_detected,
        low_inventory_alerts_detected, notified }
    """
    with _scan_lock:
        return _run_and_persist_locked(radius_miles, start_hour, end_hour, csv_path, notify)


def _run_and_persist_locked(radius_miles: float, start_hour: int, end_hour: int,
                             csv_path: Optional[str], notify: bool) -> Dict[str, Any]:
    facility_results, cluster_lots = run_facility_scan_with_lots(
        radius_miles=radius_miles, csv_path=csv_path, start_hour=start_hour, end_hour=end_hour,
    )

    now = datetime.now(timezone.utc)
    db = get_session()
    all_spikes: List[Dict[str, Any]] = []
    try:
        for r in facility_results:
            db.add(FacilityCompetitorCheck(
                facility_id=r["facility_id"],
                facility_name=r["facility_name"],
                facility_address=r["facility_address"],
                cluster_label=r.get("cluster_label"),
                radius_group_id=r.get("radius_group_id"),
                radius_group_size=r.get("radius_group_size"),
                radius_miles=r["radius_miles"],
                start_hour=start_hour,
                end_hour=end_hour,
                competitor_count=r["competitor_count"],
                competitor_total_inv_left=r["competitor_total_inv_left"],
                competitor_total_capacity=r.get("competitor_total_capacity"),
                competitor_percent_remaining=r.get("competitor_percent_remaining"),
                lots_with_known_inventory=r["lots_with_known_inventory"],
                any_sold_out=r["any_sold_out"],
                any_limited=r["any_limited"],
                error=r["error"],
            ))

            if r.get("our_spots_left") is not None:
                db.add(OurSpotheroInventorySnapshot(
                    facility_id=r["facility_id"],
                    facility_name=r["facility_name"],
                    spots_left=r["our_spots_left"],
                    capacity=r.get("our_capacity"),
                    percent_remaining=r.get("our_percent_remaining"),
                    price=r.get("our_price"),
                    scarcity_level=r.get("our_scarcity_level"),
                    start_hour=start_hour,
                    end_hour=end_hour,
                    checked_at=now,
                ))

        all_low_inventory: List[Dict[str, Any]] = []
        for cluster in cluster_lots:
            sh_spikes, sh_low = _detect_and_persist_lot_spikes(db, cluster, "spothero", cluster["spothero_lots"], now)
            pw_spikes, pw_low = _detect_and_persist_lot_spikes(db, cluster, "parkwhiz", cluster["parkwhiz_lots"], now)
            all_spikes.extend(sh_spikes)
            all_spikes.extend(pw_spikes)
            all_low_inventory.extend(sh_low)
            all_low_inventory.extend(pw_low)

        has_alerts = bool(all_spikes or all_low_inventory)
        notified = False
        if has_alerts and notify:
            attachment_url = _build_and_upload_clusters_export(db, facility_results)
            notified = notify_price_spikes(all_spikes, low_inventory_alerts=all_low_inventory, attachment_url=attachment_url)

        for s in all_spikes:
            db.add(FacilityPriceSpike(
                radius_group_id=s["radius_group_id"],
                cluster_label=s["cluster_label"],
                radius_miles=s["radius_miles"],
                sample_facility_id=s["sample_facility_id"],
                sample_facility_name=s["sample_facility_name"],
                platform=s["platform"],
                lot_name=s["lot_name"],
                lot_address=s["lot_address"],
                previous_price=s["previous_price"],
                current_price=s["current_price"],
                percent_increase=s["percent_increase"],
                previous_checked_at=s["previous_checked_at"],
                detected_at=s["detected_at"],
                notified=notified,
            ))

        for a in all_low_inventory:
            db.add(FacilityLowInventoryAlert(
                radius_group_id=a["radius_group_id"],
                cluster_label=a["cluster_label"],
                radius_miles=a["radius_miles"],
                sample_facility_id=a["sample_facility_id"],
                sample_facility_name=a["sample_facility_name"],
                platform=a["platform"],
                lot_name=a["lot_name"],
                lot_address=a["lot_address"],
                spots_left=a["spots_left"],
                capacity=a["capacity"],
                percent_remaining=a["percent_remaining"],
                previous_percent_remaining=a["previous_percent_remaining"],
                detected_at=a["detected_at"],
                notified=notified,
            ))

        db.commit()
    finally:
        db.close()

    summary = {
        "facilities_checked": len(facility_results),
        "clusters_checked": len(cluster_lots),
        "spikes_detected": len(all_spikes),
        "low_inventory_alerts_detected": len(all_low_inventory),
        "notified": notified if has_alerts else False,
    }
    logger.info("Facility scan + spike detection complete: %s", summary)
    return summary
