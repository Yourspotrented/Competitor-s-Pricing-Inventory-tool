from __future__ import annotations

import base64
import logging
import os
import secrets
import threading
from typing import Optional

from fastapi import FastAPI, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import (
    ScarcityCheck, LotCheck, PriceSpike, FacilityCompetitorCheck, FacilityPriceSpike,
    OurInventorySnapshot, OurFacilityDatabaseSnapshot, OurSpotheroInventorySnapshot,
    EventsCompetitorCheck, EventsPriceSpike, EventsLowInventoryAlert,
    LystedListing, LystedSoldOutAlert,
    create_tables, get_db, get_session,
)
from orchestrator import run_scan, get_current_run, request_stop
from checkers.scarcity_check import check_scarcity, list_event_lots
from clustering import (
    DEFAULT_RADIUS_MILES,
    find_competitors_for_facility, find_parkwhiz_competitors_for_facility,
)
from facility_scan_runner import run_and_persist_facility_scan
from facility_scheduler import start_scheduler, get_scheduler_status
from events_facilities import load_events_facilities
from events_scan_runner import EVENTS_DEFAULT_RADIUS_MILES, run_and_persist_events_scan
from events_scheduler import start_events_scheduler, get_events_scheduler_status
from lysted_listings import get_latest_upload, save_upload
from lysted_scan_runner import run_and_persist_lysted_scan
from lysted_scheduler import start_lysted_scheduler, get_lysted_scheduler_status
from our_inventory import fetch_our_inventory
from our_facility_database import load_facility_database

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Competitors Data Finder")

_HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Password gate. Off when DASHBOARD_PASSWORD is unset (local dev). When set,
# every route except /health needs either
#   - HTTP Basic auth (what a browser sends after its own prompt when the
#     dashboard is opened straight on the backend's URL), or
#   - an X-Dashboard-Key header (what the dashboard sends when it is hosted
#     elsewhere, e.g. Vercel, and calls this API cross-origin — the page
#     prompts once and remembers it).
# Registered before CORSMiddleware on purpose: Starlette makes the last-added
# middleware outermost, so CORS wraps this and a 401 still gets CORS headers.
# ---------------------------------------------------------------------------
_DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()


def _password_ok(request: Request) -> bool:
    expected = _DASHBOARD_PASSWORD.encode()
    key = request.headers.get("x-dashboard-key", "")
    if key and secrets.compare_digest(key.encode(), expected):
        return True
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            _, _, password = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            return secrets.compare_digest(password.encode(), expected)
        except Exception:
            return False
    return False


@app.middleware("http")
async def dashboard_password_gate(request: Request, call_next):
    if not _DASHBOARD_PASSWORD or request.method == "OPTIONS" or request.url.path == "/health":
        return await call_next(request)
    if _password_ok(request):
        return await call_next(request)
    # Only a page load gets the Basic challenge (so the browser prompts). API
    # calls get a bare 401 — the dashboard handles that with its own prompt.
    is_page = not request.url.path.startswith("/api") and "text/html" in request.headers.get("accept", "")
    headers = {"WWW-Authenticate": 'Basic realm="Competitors Data Finder"'} if is_page else {}
    return PlainTextResponse("Unauthorized", status_code=401, headers=headers)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    create_tables()
    start_scheduler()
    start_events_scheduler()
    start_lysted_scheduler()


@app.get("/")
def index():
    return FileResponse(os.path.join(_HERE, "static", "index.html"))


@app.get("/health")
def health():
    """Unauthenticated liveness check for the hosting platform."""
    return {"status": "ok"}


@app.get("/config.js")
def dashboard_config():
    """
    Runtime config for the dashboard. When the page is served by this app the
    API is same-origin, so API_BASE is empty. A separately hosted copy of the
    page (Vercel) ships its own config.js pointing here — see vercel-build.sh.
    """
    return PlainTextResponse('window.API_BASE = "";\n', media_type="application/javascript")


app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")


# ---------------------------------------------------------------------------
# Scan control
# ---------------------------------------------------------------------------

@app.post("/api/scan")
def trigger_scan(
    region: Optional[list[str]] = Query(None),
    event_limit: Optional[int] = Query(None),
    events_per_region: Optional[int] = Query(None),
):
    """Kick off a background scan. Poll /api/scan/status for progress."""
    current = get_current_run()
    if current and current.get("status") == "running":
        return {"status": "already_running", "run": current}

    thread = threading.Thread(
        target=run_scan,
        kwargs={"regions": region, "event_limit": event_limit, "events_per_region": events_per_region},
        daemon=True,
    )
    thread.start()
    return {"status": "started"}


@app.get("/api/scan/status")
def scan_status():
    return get_current_run() or {"status": "idle"}


@app.post("/api/scan/stop")
def stop_scan():
    """Request the running scan to stop after its current listing. Results already checked are kept."""
    stopped = request_stop()
    return {"status": "stop_requested" if stopped else "no_scan_running"}


@app.get("/api/check-one")
def check_one(event_name: str, event_date: str, event_venue: str, our_section: str = ""):
    """On-demand single-event scarcity check (bypasses ReachPro/DB)."""
    return check_scarcity(event_name, event_date, event_venue, our_section)


@app.get("/api/event-lots")
def event_lots(
    event_name: str, event_date: str, event_venue: str,
    our_section: str = "", reachpro_event_id: str = "",
):
    """
    All competing lots (SpotHero + ParkWhiz) found for one event, each with
    its own spots-left/status. Fetched live on click, then persisted so past
    breakdowns are visible later via /api/event-lots/history.
    """
    lots = list_event_lots(event_name, event_date, event_venue, our_section)

    if reachpro_event_id:
        db = get_session()
        try:
            for lot in lots:
                db.add(LotCheck(
                    reachpro_event_id=reachpro_event_id,
                    event_name=event_name,
                    event_date=event_date,
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
                ))
            db.commit()
        finally:
            db.close()

    return lots


@app.get("/api/event-lots/history")
def event_lots_history(
    reachpro_event_id: str,
    platform: Optional[str] = Query(None),
    lot_name: Optional[str] = Query(None),
    lot_address: Optional[str] = Query(None),
    limit: int = Query(500, le=2000),
    db: Session = Depends(get_db),
):
    """
    Past lot-check readings for one event, most recent first. Pass
    platform + lot_name + lot_address to narrow to a single lot's price
    trend (used by the Price Spikes expand view); omit them to get every
    lot on the event (used by the Scarcity expand view).
    """
    q = db.query(LotCheck).filter(LotCheck.reachpro_event_id == reachpro_event_id)
    if platform:
        q = q.filter(LotCheck.platform == platform)
    if lot_name is not None:
        q = q.filter(LotCheck.lot_name == lot_name)
    if lot_address is not None:
        q = q.filter(LotCheck.lot_address == lot_address)
    rows = q.order_by(LotCheck.checked_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "platform": r.platform,
            "lot_name": r.lot_name,
            "lot_address": r.lot_address,
            "price": r.price,
            "spots_left": r.spots_left,
            "capacity": r.capacity,
            "percent_remaining": r.percent_remaining,
            "availability_status": r.availability_status,
            "scarcity_level": r.scarcity_level,
            "is_our_lot": r.is_our_lot,
            "checked_at": r.checked_at.isoformat() if r.checked_at else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Dashboard data
# ---------------------------------------------------------------------------

_LEVEL_RANK = {"sold_out": 0, "limited": 1, "not_found": 2, "unknown": 3, "ok": 4}


@app.get("/api/dashboard")
def dashboard(
    scarcity: Optional[str] = Query(None, description="Filter: limited, sold_out, or omit for all scarce"),
    region: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    limit: int = Query(200, le=1000),
    db: Session = Depends(get_db),
):
    """
    Latest scarcity reading per (event, platform), sorted most-scarce first.
    This is the primary data feed for the dashboard table.
    """
    # Get the latest checked_at per (reachpro_event_id, platform)
    subq = (
        db.query(
            ScarcityCheck.reachpro_event_id,
            ScarcityCheck.platform,
            func.max(ScarcityCheck.checked_at).label("max_checked_at"),
        )
        .group_by(ScarcityCheck.reachpro_event_id, ScarcityCheck.platform)
        .subquery()
    )

    q = db.query(ScarcityCheck).join(
        subq,
        (ScarcityCheck.reachpro_event_id == subq.c.reachpro_event_id)
        & (ScarcityCheck.platform == subq.c.platform)
        & (ScarcityCheck.checked_at == subq.c.max_checked_at),
    )

    if region:
        q = q.filter(ScarcityCheck.region == region)
    if platform:
        q = q.filter(ScarcityCheck.platform == platform)
    if scarcity == "all":
        pass  # no scarcity filter — show every latest reading
    elif scarcity:
        q = q.filter(ScarcityCheck.scarcity_level == scarcity)
    else:
        q = q.filter(ScarcityCheck.scarcity_level.in_(["limited", "sold_out"]))

    rows = q.order_by(ScarcityCheck.checked_at.desc()).limit(limit).all()
    rows.sort(key=lambda r: _LEVEL_RANK.get(r.scarcity_level, 9))

    return [
        {
            "id": r.id,
            "reachpro_event_id": r.reachpro_event_id,
            "event_name": r.event_name,
            "event_venue": r.event_venue,
            "event_date": r.event_date,
            "section": r.section,
            "our_price": r.our_price,
            "region": r.region,
            "platform": r.platform,
            "competitor_price": r.price,
            "spots_left": r.spots_left,
            "capacity": r.capacity,
            "percent_remaining": r.percent_remaining,
            "availability_status": r.availability_status,
            "scarcity_level": r.scarcity_level,
            "checked_at": r.checked_at.isoformat() if r.checked_at else None,
        }
        for r in rows
    ]


@app.get("/api/dashboard/history")
def dashboard_history(reachpro_event_id: str, limit: int = Query(200, le=2000), db: Session = Depends(get_db)):
    """
    Every past scarcity reading for one event (across all scans), most recent
    first — lets you see how scarcity/price trended over time, not just the
    latest snapshot shown on the main dashboard table.
    """
    rows = (
        db.query(ScarcityCheck)
        .filter(ScarcityCheck.reachpro_event_id == reachpro_event_id)
        .order_by(ScarcityCheck.checked_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "platform": r.platform,
            "competitor_price": r.price,
            "spots_left": r.spots_left,
            "capacity": r.capacity,
            "percent_remaining": r.percent_remaining,
            "availability_status": r.availability_status,
            "scarcity_level": r.scarcity_level,
            "our_price": r.our_price,
            "checked_at": r.checked_at.isoformat() if r.checked_at else None,
        }
        for r in rows
    ]


@app.get("/api/dashboard/summary")
def dashboard_summary(region: Optional[str] = Query(None), db: Session = Depends(get_db)):
    subq = (
        db.query(
            ScarcityCheck.reachpro_event_id,
            ScarcityCheck.platform,
            func.max(ScarcityCheck.checked_at).label("max_checked_at"),
        )
        .group_by(ScarcityCheck.reachpro_event_id, ScarcityCheck.platform)
        .subquery()
    )
    q = db.query(ScarcityCheck).join(
        subq,
        (ScarcityCheck.reachpro_event_id == subq.c.reachpro_event_id)
        & (ScarcityCheck.platform == subq.c.platform)
        & (ScarcityCheck.checked_at == subq.c.max_checked_at),
    )
    if region:
        q = q.filter(ScarcityCheck.region == region)
    rows = q.all()
    summary = {"total": len(rows), "sold_out": 0, "limited": 0, "not_found": 0, "ok": 0, "unknown": 0}
    for r in rows:
        summary[r.scarcity_level or "unknown"] = summary.get(r.scarcity_level or "unknown", 0) + 1

    spike_q = db.query(PriceSpike)
    if region:
        spike_q = spike_q.filter(PriceSpike.region == region)
    summary["price_spikes"] = spike_q.count()
    return summary


# ---------------------------------------------------------------------------
# Price spikes — significant competitor price increases vs. their previous reading
# ---------------------------------------------------------------------------

@app.get("/api/price-spikes")
def price_spikes(
    region: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    min_percent: Optional[float] = Query(None),
    limit: int = Query(200, le=1000),
    db: Session = Depends(get_db),
):
    """
    Detected significant per-lot price increases, most recent and largest first.
    A spike is recorded whenever a scan finds a lot priced 20%+ above its own
    previous reading — this is the "unusual demand / rate increase" signal.
    """
    q = db.query(PriceSpike)
    if region:
        q = q.filter(PriceSpike.region == region)
    if platform:
        q = q.filter(PriceSpike.platform == platform)
    if min_percent is not None:
        q = q.filter(PriceSpike.percent_increase >= min_percent)

    rows = q.order_by(PriceSpike.detected_at.desc()).limit(limit).all()
    rows.sort(key=lambda r: r.percent_increase, reverse=True)

    return [
        {
            "id": r.id,
            "reachpro_event_id": r.reachpro_event_id,
            "event_name": r.event_name,
            "event_date": r.event_date,
            "region": r.region,
            "platform": r.platform,
            "lot_name": r.lot_name,
            "lot_address": r.lot_address,
            "previous_price": r.previous_price,
            "current_price": r.current_price,
            "percent_increase": r.percent_increase,
            "previous_checked_at": r.previous_checked_at.isoformat() if r.previous_checked_at else None,
            "detected_at": r.detected_at.isoformat() if r.detected_at else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Facility competitor scan — radius clustering over our static portfolio
# (garages/lots), independent of ReachPro events. Answers "Competitor total
# inv left" per facility for the team's pricing sheet.
# ---------------------------------------------------------------------------

_facility_scan_lock = threading.Lock()
_facility_scan_status: dict = {"status": "idle"}


def _run_facility_scan_background(radius_miles: float, start_hour: int, end_hour: int) -> None:
    global _facility_scan_status
    with _facility_scan_lock:
        _facility_scan_status = {"status": "running", "radius_miles": radius_miles,
                                  "start_hour": start_hour, "end_hour": end_hour}

    try:
        summary = run_and_persist_facility_scan(
            radius_miles=radius_miles, start_hour=start_hour, end_hour=end_hour, notify=True,
        )
        with _facility_scan_lock:
            _facility_scan_status = {
                "status": "completed", "radius_miles": radius_miles,
                "start_hour": start_hour, "end_hour": end_hour,
                "facilities_checked": summary["facilities_checked"],
                "spikes_detected": summary["spikes_detected"],
                "notified": summary["notified"],
            }
    except Exception as exc:
        logging.getLogger(__name__).error("Facility scan failed: %s", exc)
        with _facility_scan_lock:
            _facility_scan_status = {"status": "failed", "error": str(exc)}


@app.post("/api/facility-scan")
def trigger_facility_scan(radius_miles: float = Query(DEFAULT_RADIUS_MILES),
                           start_hour: int = Query(10, ge=0, le=23),
                           end_hour: int = Query(22, ge=1, le=24)):
    """Kick off a background radius-clustering competitor scan over our facility portfolio."""
    with _facility_scan_lock:
        if _facility_scan_status.get("status") == "running":
            return {"status": "already_running", "run": _facility_scan_status}

    thread = threading.Thread(
        target=_run_facility_scan_background,
        kwargs={"radius_miles": radius_miles, "start_hour": start_hour, "end_hour": end_hour},
        daemon=True,
    )
    thread.start()
    return {"status": "started"}


@app.get("/api/facility-scan/status")
def facility_scan_status():
    with _facility_scan_lock:
        return dict(_facility_scan_status)


# ---------------------------------------------------------------------------
# Events-team scan — same pattern as the pricing-team facility scan above,
# fully separate DB tables/scheduler/Teams channel (see events_scan_runner.py).
# ---------------------------------------------------------------------------

_events_scan_lock = threading.Lock()
_events_scan_status: dict = {"status": "idle"}


def _run_events_scan_background(radius_miles: float, start_hour: int, end_hour: int) -> None:
    global _events_scan_status
    with _events_scan_lock:
        _events_scan_status = {"status": "running", "radius_miles": radius_miles,
                                "start_hour": start_hour, "end_hour": end_hour}

    try:
        summary = run_and_persist_events_scan(
            radius_miles=radius_miles, start_hour=start_hour, end_hour=end_hour, notify=True,
        )
        with _events_scan_lock:
            _events_scan_status = {
                "status": "completed", "radius_miles": radius_miles,
                "start_hour": start_hour, "end_hour": end_hour,
                "facilities_checked": summary["facilities_checked"],
                "spikes_detected": summary["spikes_detected"],
                "notified": summary["notified"],
            }
    except Exception as exc:
        logging.getLogger(__name__).error("Events scan failed: %s", exc)
        with _events_scan_lock:
            _events_scan_status = {"status": "failed", "error": str(exc)}


@app.post("/api/events-scan")
def trigger_events_scan(radius_miles: float = Query(EVENTS_DEFAULT_RADIUS_MILES),
                         start_hour: int = Query(10, ge=0, le=23),
                         end_hour: int = Query(22, ge=1, le=24)):
    """Kick off a background radius-clustering competitor scan over the Events team's facility portfolio."""
    with _events_scan_lock:
        if _events_scan_status.get("status") == "running":
            return {"status": "already_running", "run": _events_scan_status}

    thread = threading.Thread(
        target=_run_events_scan_background,
        kwargs={"radius_miles": radius_miles, "start_hour": start_hour, "end_hour": end_hour},
        daemon=True,
    )
    thread.start()
    return {"status": "started"}


@app.get("/api/events-scan/status")
def events_scan_status():
    with _events_scan_lock:
        return dict(_events_scan_status)


@app.get("/api/events-scan/scheduler-status")
def events_scan_scheduler_status():
    return get_events_scheduler_status()


def _only_latest_run(q, model):
    """
    Narrow a spike/alert query to a single scan's worth of rows.

    Every row produced by one scan carries an identical detected_at (the
    runner stamps one timestamp for the whole run), so max(detected_at)
    identifies a run exactly.

    Evaluated after the caller's own filters, so "latest run" means the most
    recent run that produced a row matching them — otherwise asking for
    SpotHero would come back empty whenever the newest scan happened to turn
    up only ParkWhiz rows.
    """
    latest = q.with_entities(func.max(model.detected_at)).scalar()
    return q if latest is None else q.filter(model.detected_at == latest)


def _latest_our_spothero_inventory(db: Session) -> dict:
    """
    Latest live SpotHero reading of OUR own inventory, keyed by facility id.

    Shared by both portfolio endpoints: the reading is a property of the
    facility, not of whichever scan happened to record it, so an Events
    facility that also sits in the pricing sheet shows the same number in
    both tabs.
    """
    subq = (
        db.query(
            OurSpotheroInventorySnapshot.facility_id,
            func.max(OurSpotheroInventorySnapshot.checked_at).label("max_checked_at"),
        )
        .group_by(OurSpotheroInventorySnapshot.facility_id)
        .subquery()
    )
    rows = (
        db.query(OurSpotheroInventorySnapshot)
        .join(
            subq,
            (OurSpotheroInventorySnapshot.facility_id == subq.c.facility_id)
            & (OurSpotheroInventorySnapshot.checked_at == subq.c.max_checked_at),
        )
        .all()
    )
    return {r.facility_id: r for r in rows}


def _our_spothero_fields(row) -> dict:
    return {
        "our_spots_left": row.spots_left if row else None,
        "our_capacity": row.capacity if row else None,
        "our_percent_remaining": row.percent_remaining if row else None,
        "our_spothero_price": row.price if row else None,
        "our_scarcity_level": row.scarcity_level if row else None,
        "our_spothero_checked_at": row.checked_at.isoformat() if row and row.checked_at else None,
    }


@app.get("/api/events-competitors")
def events_competitors(db: Session = Depends(get_db)):
    """Latest competitor-inventory reading per Events-team facility."""
    subq = (
        db.query(
            EventsCompetitorCheck.facility_id,
            func.max(EventsCompetitorCheck.checked_at).label("max_checked_at"),
        )
        .group_by(EventsCompetitorCheck.facility_id)
        .subquery()
    )
    rows = (
        db.query(EventsCompetitorCheck)
        .join(
            subq,
            (EventsCompetitorCheck.facility_id == subq.c.facility_id)
            & (EventsCompetitorCheck.checked_at == subq.c.max_checked_at),
        )
        .order_by(EventsCompetitorCheck.facility_name)
        .all()
    )
    our_spothero = _latest_our_spothero_inventory(db)
    return [
        {
            **_our_spothero_fields(our_spothero.get(r.facility_id)),
            "facility_id": r.facility_id,
            "facility_name": r.facility_name,
            "facility_address": r.facility_address,
            "cluster_label": r.cluster_label,
            "radius_group_id": r.radius_group_id,
            "radius_group_size": r.radius_group_size,
            "radius_miles": r.radius_miles,
            "start_hour": r.start_hour,
            "end_hour": r.end_hour,
            "competitor_count": r.competitor_count,
            "competitor_total_inv_left": r.competitor_total_inv_left,
            "competitor_total_capacity": r.competitor_total_capacity,
            "competitor_percent_remaining": r.competitor_percent_remaining,
            "lots_with_known_inventory": r.lots_with_known_inventory,
            "any_sold_out": r.any_sold_out,
            "any_limited": r.any_limited,
            "error": r.error,
            "checked_at": r.checked_at.isoformat() if r.checked_at else None,
        }
        for r in rows
    ]


@app.get("/api/events-price-spikes")
def events_price_spikes(
    cluster_label: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    min_percent: Optional[float] = Query(None),
    latest_run_only: bool = Query(False),
    limit: int = Query(200, le=1000),
    db: Session = Depends(get_db),
):
    """Detected competitor price increases near Events-team facilities. See facility_price_spikes for latest_run_only."""
    q = db.query(EventsPriceSpike)
    if cluster_label:
        q = q.filter(EventsPriceSpike.cluster_label == cluster_label)
    if platform:
        q = q.filter(EventsPriceSpike.platform == platform)
    if min_percent is not None:
        q = q.filter(EventsPriceSpike.percent_increase >= min_percent)
    if latest_run_only:
        q = _only_latest_run(q, EventsPriceSpike)

    rows = q.order_by(EventsPriceSpike.detected_at.desc()).limit(limit).all()
    rows.sort(key=lambda r: r.percent_increase, reverse=True)

    return [
        {
            "id": r.id,
            "radius_group_id": r.radius_group_id,
            "cluster_label": r.cluster_label,
            "radius_miles": r.radius_miles,
            "sample_facility_id": r.sample_facility_id,
            "sample_facility_name": r.sample_facility_name,
            "platform": r.platform,
            "lot_name": r.lot_name,
            "lot_address": r.lot_address,
            "previous_price": r.previous_price,
            "current_price": r.current_price,
            "percent_increase": r.percent_increase,
            "previous_checked_at": r.previous_checked_at.isoformat() if r.previous_checked_at else None,
            "detected_at": r.detected_at.isoformat() if r.detected_at else None,
            "notified": r.notified,
        }
        for r in rows
    ]


@app.get("/api/events-low-inventory-alerts")
def events_low_inventory_alerts(
    cluster_label: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    latest_run_only: bool = Query(False),
    limit: int = Query(200, le=1000),
    db: Session = Depends(get_db),
):
    """Detected low-inventory crossings near Events-team facilities. See facility_price_spikes for latest_run_only."""
    q = db.query(EventsLowInventoryAlert)
    if cluster_label:
        q = q.filter(EventsLowInventoryAlert.cluster_label == cluster_label)
    if platform:
        q = q.filter(EventsLowInventoryAlert.platform == platform)
    if latest_run_only:
        q = _only_latest_run(q, EventsLowInventoryAlert)

    rows = q.order_by(EventsLowInventoryAlert.detected_at.desc()).limit(limit).all()

    return [
        {
            "id": r.id,
            "radius_group_id": r.radius_group_id,
            "cluster_label": r.cluster_label,
            "radius_miles": r.radius_miles,
            "sample_facility_id": r.sample_facility_id,
            "sample_facility_name": r.sample_facility_name,
            "platform": r.platform,
            "lot_name": r.lot_name,
            "lot_address": r.lot_address,
            "spots_left": r.spots_left,
            "capacity": r.capacity,
            "percent_remaining": r.percent_remaining,
            "previous_percent_remaining": r.previous_percent_remaining,
            "detected_at": r.detected_at.isoformat() if r.detected_at else None,
            "notified": r.notified,
        }
        for r in rows
    ]


@app.get("/api/facility-competitors")
def facility_competitors(db: Session = Depends(get_db)):
    """
    Latest competitor-inventory reading per facility, joined with our own
    latest Notion "Inventory" snapshot when one exists (see
    OurInventorySnapshot) — reported under Notion's own field names
    (our_inventory, our_status, our_facility_status), not remapped to
    "Our total inv left", since that mapping hasn't been confirmed yet.
    """
    subq = (
        db.query(
            FacilityCompetitorCheck.facility_id,
            func.max(FacilityCompetitorCheck.checked_at).label("max_checked_at"),
        )
        .group_by(FacilityCompetitorCheck.facility_id)
        .subquery()
    )
    rows = (
        db.query(FacilityCompetitorCheck)
        .join(
            subq,
            (FacilityCompetitorCheck.facility_id == subq.c.facility_id)
            & (FacilityCompetitorCheck.checked_at == subq.c.max_checked_at),
        )
        .order_by(FacilityCompetitorCheck.facility_name)
        .all()
    )

    inv_subq = (
        db.query(
            OurInventorySnapshot.facility_id,
            func.max(OurInventorySnapshot.checked_at).label("max_checked_at"),
        )
        .group_by(OurInventorySnapshot.facility_id)
        .subquery()
    )
    inv_rows = (
        db.query(OurInventorySnapshot)
        .join(
            inv_subq,
            (OurInventorySnapshot.facility_id == inv_subq.c.facility_id)
            & (OurInventorySnapshot.checked_at == inv_subq.c.max_checked_at),
        )
        .all()
    )
    inv_by_facility = {r.facility_id: r for r in inv_rows}

    fdb_subq = (
        db.query(
            OurFacilityDatabaseSnapshot.facility_id,
            func.max(OurFacilityDatabaseSnapshot.checked_at).label("max_checked_at"),
        )
        .group_by(OurFacilityDatabaseSnapshot.facility_id)
        .subquery()
    )
    fdb_rows = (
        db.query(OurFacilityDatabaseSnapshot)
        .join(
            fdb_subq,
            (OurFacilityDatabaseSnapshot.facility_id == fdb_subq.c.facility_id)
            & (OurFacilityDatabaseSnapshot.checked_at == fdb_subq.c.max_checked_at),
        )
        .all()
    )
    fdb_by_facility = {r.facility_id: r for r in fdb_rows}
    our_spothero = _latest_our_spothero_inventory(db)

    result = []
    for r in rows:
        inv = inv_by_facility.get(r.facility_id)
        fdb = fdb_by_facility.get(r.facility_id)
        result.append({
            **_our_spothero_fields(our_spothero.get(r.facility_id)),
            "facility_id": r.facility_id,
            "facility_name": r.facility_name,
            "facility_address": r.facility_address,
            "cluster_label": r.cluster_label,
            "radius_group_id": r.radius_group_id,
            "radius_group_size": r.radius_group_size,
            "radius_miles": r.radius_miles,
            "start_hour": r.start_hour,
            "end_hour": r.end_hour,
            "competitor_count": r.competitor_count,
            "competitor_total_inv_left": r.competitor_total_inv_left,
            "competitor_total_capacity": r.competitor_total_capacity,
            "competitor_percent_remaining": r.competitor_percent_remaining,
            "lots_with_known_inventory": r.lots_with_known_inventory,
            "any_sold_out": r.any_sold_out,
            "any_limited": r.any_limited,
            "error": r.error,
            "checked_at": r.checked_at.isoformat() if r.checked_at else None,
            "our_inventory": inv.inventory if inv else None,
            "our_status": inv.status if inv else None,
            "our_facility_status": inv.facility_status if inv else None,
            "our_inventory_checked_at": inv.checked_at.isoformat() if inv and inv.checked_at else None,
            "fdb_no_of_stalls": fdb.no_of_stalls if fdb else None,
            "fdb_monthly_inventory": fdb.monthly_inventory if fdb else None,
            "fdb_facility_status": fdb.facility_status if fdb else None,
            "fdb_facility_category": fdb.facility_category if fdb else None,
            "fdb_parkwhiz_status": fdb.parkwhiz_status if fdb else None,
            "fdb_parkwhiz_stalls": fdb.parkwhiz_stalls if fdb else None,
            "fdb_way_status": fdb.way_status if fdb else None,
            "fdb_way_stalls": fdb.way_stalls if fdb else None,
            "fdb_neighbor_status": fdb.neighbor_status if fdb else None,
            "fdb_neighbor_stalls": fdb.neighbor_stalls if fdb else None,
            "fdb_spacer_status": fdb.spacer_status if fdb else None,
            "fdb_spacer_stalls": fdb.spacer_stalls if fdb else None,
            "fdb_checked_at": fdb.checked_at.isoformat() if fdb and fdb.checked_at else None,
        })
    return result


@app.get("/api/facility-competitor-lots")
def facility_competitor_lots(facility_id: str, radius_miles: float = Query(DEFAULT_RADIUS_MILES),
                              start_hour: int = Query(10, ge=0, le=23),
                              end_hour: int = Query(22, ge=1, le=24),
                              portfolio: str = Query("pricing")):
    """
    The individual competitor lots (name, address, price, spots left) behind
    one facility's shared radius-group number — fetched live on click, since
    /api/facility-competitors only stores the aggregated total, not the
    per-lot breakdown.

    portfolio: "pricing" (default, the Clusters sheet) or "events" (the
    Events team's sheet) — needed because 76 Events facilities don't exist
    in the pricing sheet at all.
    """
    competitors, err = find_competitors_for_facility(
        facility_id, radius_miles=radius_miles, start_hour=start_hour, end_hour=end_hour,
        load_facilities_fn=load_events_facilities if portfolio == "events" else None,
    )
    if err:
        return {"error": err, "competitors": []}
    return {"error": None, "competitors": competitors}


@app.get("/api/facility-competitor-lots/parkwhiz")
def facility_competitor_lots_parkwhiz(facility_id: str, radius_miles: float = Query(DEFAULT_RADIUS_MILES),
                                       start_hour: int = Query(10, ge=0, le=23),
                                       end_hour: int = Query(22, ge=1, le=24),
                                       portfolio: str = Query("pricing")):
    """
    ParkWhiz equivalent of /api/facility-competitor-lots. Reported as its
    own endpoint, not merged with the SpotHero one, since ParkWhiz only
    exposes a 3-state status per lot (no exact spots_left/capacity).
    """
    competitors, err = find_parkwhiz_competitors_for_facility(
        facility_id, radius_miles=radius_miles, start_hour=start_hour, end_hour=end_hour,
        load_facilities_fn=load_events_facilities if portfolio == "events" else None,
    )
    if err:
        return {"error": err, "competitors": []}
    return {"error": None, "competitors": competitors}


# ---------------------------------------------------------------------------
# Our own inventory — pulled from the team's Notion "Dashboard" database
# (see our_inventory.py). Reported under Notion's own field names, not
# "Our total inv left", until that mapping is confirmed with the team.
# ---------------------------------------------------------------------------

_our_inventory_lock = threading.Lock()
_our_inventory_status: dict = {"status": "idle"}


def _run_our_inventory_sync_background() -> None:
    global _our_inventory_status
    with _our_inventory_lock:
        _our_inventory_status = {"status": "running"}

    try:
        rows = fetch_our_inventory()

        db = get_session()
        try:
            matched = 0
            for r in rows:
                if not r["facility_id"]:
                    continue
                matched += 1
                db.add(OurInventorySnapshot(
                    facility_id=r["facility_id"],
                    facility_name=r["facility_name"],
                    inventory=r["inventory"],
                    status=r["status"],
                    facility_status=r["facility_status"],
                ))
            db.commit()
        finally:
            db.close()

        with _our_inventory_lock:
            _our_inventory_status = {"status": "completed", "rows_fetched": len(rows), "rows_with_facility_id": matched}
    except Exception as exc:
        logging.getLogger(__name__).error("Our-inventory sync failed: %s", exc)
        with _our_inventory_lock:
            _our_inventory_status = {"status": "failed", "error": str(exc)}


@app.post("/api/our-inventory/sync")
def trigger_our_inventory_sync():
    """Kick off a background pull of our facility data from Notion."""
    with _our_inventory_lock:
        if _our_inventory_status.get("status") == "running":
            return {"status": "already_running", "run": _our_inventory_status}

    thread = threading.Thread(target=_run_our_inventory_sync_background, daemon=True)
    thread.start()
    return {"status": "started"}


@app.get("/api/our-inventory/sync/status")
def our_inventory_sync_status():
    with _our_inventory_lock:
        return dict(_our_inventory_status)


@app.get("/api/our-inventory")
def our_inventory(db: Session = Depends(get_db)):
    """Latest Notion inventory snapshot per facility."""
    subq = (
        db.query(
            OurInventorySnapshot.facility_id,
            func.max(OurInventorySnapshot.checked_at).label("max_checked_at"),
        )
        .group_by(OurInventorySnapshot.facility_id)
        .subquery()
    )
    rows = (
        db.query(OurInventorySnapshot)
        .join(
            subq,
            (OurInventorySnapshot.facility_id == subq.c.facility_id)
            & (OurInventorySnapshot.checked_at == subq.c.max_checked_at),
        )
        .order_by(OurInventorySnapshot.facility_name)
        .all()
    )
    return [
        {
            "facility_id": r.facility_id,
            "facility_name": r.facility_name,
            "inventory": r.inventory,
            "status": r.status,
            "facility_status": r.facility_status,
            "checked_at": r.checked_at.isoformat() if r.checked_at else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Our facility database — imported from a manually-downloaded SharePoint CSV
# export (see our_facility_database.py). Not a live pull — Graph API access
# to that SharePoint site is blocked pending tenant admin consent, so this
# reads whatever CSV is currently dropped in the project root. "Import", not
# "sync", since nothing here talks to SharePoint directly.
# ---------------------------------------------------------------------------

_fdb_import_lock = threading.Lock()
_fdb_import_status: dict = {"status": "idle"}


def _run_fdb_import_background() -> None:
    global _fdb_import_status
    with _fdb_import_lock:
        _fdb_import_status = {"status": "running"}

    try:
        rows = load_facility_database()

        db = get_session()
        try:
            for r in rows:
                db.add(OurFacilityDatabaseSnapshot(
                    facility_id=r["facility_id"],
                    facility_name=r["facility_name"],
                    facility_address=r["facility_address"],
                    facility_status=r["facility_status"],
                    facility_category=r["facility_category"],
                    no_of_stalls=r["no_of_stalls"],
                    monthly_inventory=r["monthly_inventory"],
                    parkwhiz_status=r["parkwhiz_status"],
                    parkwhiz_stalls=r["parkwhiz_stalls"],
                    way_status=r["way_status"],
                    way_stalls=r["way_stalls"],
                    neighbor_status=r["neighbor_status"],
                    neighbor_stalls=r["neighbor_stalls"],
                    spacer_status=r["spacer_status"],
                    spacer_stalls=r["spacer_stalls"],
                ))
            db.commit()
        finally:
            db.close()

        with _fdb_import_lock:
            _fdb_import_status = {"status": "completed", "rows_imported": len(rows)}
    except FileNotFoundError as exc:
        logging.getLogger(__name__).error("Facility database import failed — file not found: %s", exc)
        with _fdb_import_lock:
            _fdb_import_status = {"status": "failed", "error": f"CSV not found: {exc}"}
    except Exception as exc:
        logging.getLogger(__name__).error("Facility database import failed: %s", exc)
        with _fdb_import_lock:
            _fdb_import_status = {"status": "failed", "error": str(exc)}


@app.post("/api/our-facility-database/import")
def trigger_fdb_import():
    """Re-read the FACILITY DATABASE CSV currently on disk and persist a new snapshot."""
    with _fdb_import_lock:
        if _fdb_import_status.get("status") == "running":
            return {"status": "already_running", "run": _fdb_import_status}

    thread = threading.Thread(target=_run_fdb_import_background, daemon=True)
    thread.start()
    return {"status": "started"}


@app.get("/api/our-facility-database/import/status")
def fdb_import_status():
    with _fdb_import_lock:
        return dict(_fdb_import_status)


@app.get("/api/our-facility-database")
def our_facility_database(db: Session = Depends(get_db)):
    """Latest facility-database snapshot per facility."""
    subq = (
        db.query(
            OurFacilityDatabaseSnapshot.facility_id,
            func.max(OurFacilityDatabaseSnapshot.checked_at).label("max_checked_at"),
        )
        .group_by(OurFacilityDatabaseSnapshot.facility_id)
        .subquery()
    )
    rows = (
        db.query(OurFacilityDatabaseSnapshot)
        .join(
            subq,
            (OurFacilityDatabaseSnapshot.facility_id == subq.c.facility_id)
            & (OurFacilityDatabaseSnapshot.checked_at == subq.c.max_checked_at),
        )
        .order_by(OurFacilityDatabaseSnapshot.facility_name)
        .all()
    )
    return [
        {
            "facility_id": r.facility_id,
            "facility_name": r.facility_name,
            "facility_address": r.facility_address,
            "facility_status": r.facility_status,
            "facility_category": r.facility_category,
            "no_of_stalls": r.no_of_stalls,
            "monthly_inventory": r.monthly_inventory,
            "parkwhiz_status": r.parkwhiz_status,
            "parkwhiz_stalls": r.parkwhiz_stalls,
            "way_status": r.way_status,
            "way_stalls": r.way_stalls,
            "neighbor_status": r.neighbor_status,
            "neighbor_stalls": r.neighbor_stalls,
            "spacer_status": r.spacer_status,
            "spacer_stalls": r.spacer_stalls,
            "checked_at": r.checked_at.isoformat() if r.checked_at else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Facility price spikes — significant competitor price increases near our
# facility radius-groups, detected during any facility scan (manual or
# scheduled) — facility-flow parallel to /api/price-spikes.
# ---------------------------------------------------------------------------

@app.get("/api/facility-price-spikes")
def facility_price_spikes(
    cluster_label: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    min_percent: Optional[float] = Query(None),
    latest_run_only: bool = Query(False),
    limit: int = Query(200, le=1000),
    db: Session = Depends(get_db),
):
    """
    Detected significant competitor price increases near our facilities.

    latest_run_only keeps just the most recent scan's spikes. Without it the
    endpoint returns the full accumulated history sorted by size, so the
    biggest jumps ever recorded sit at the top permanently and the view looks
    frozen even when scans are running fine.
    """
    q = db.query(FacilityPriceSpike)
    if cluster_label:
        q = q.filter(FacilityPriceSpike.cluster_label == cluster_label)
    if platform:
        q = q.filter(FacilityPriceSpike.platform == platform)
    if min_percent is not None:
        q = q.filter(FacilityPriceSpike.percent_increase >= min_percent)
    if latest_run_only:
        q = _only_latest_run(q, FacilityPriceSpike)

    rows = q.order_by(FacilityPriceSpike.detected_at.desc()).limit(limit).all()
    rows.sort(key=lambda r: r.percent_increase, reverse=True)

    return [
        {
            "id": r.id,
            "radius_group_id": r.radius_group_id,
            "cluster_label": r.cluster_label,
            "radius_miles": r.radius_miles,
            "sample_facility_id": r.sample_facility_id,
            "sample_facility_name": r.sample_facility_name,
            "platform": r.platform,
            "lot_name": r.lot_name,
            "lot_address": r.lot_address,
            "previous_price": r.previous_price,
            "current_price": r.current_price,
            "percent_increase": r.percent_increase,
            "previous_checked_at": r.previous_checked_at.isoformat() if r.previous_checked_at else None,
            "detected_at": r.detected_at.isoformat() if r.detected_at else None,
            "notified": r.notified,
        }
        for r in rows
    ]


@app.get("/api/facility-scan/scheduler-status")
def facility_scan_scheduler_status():
    """Status of the background hourly (configurable) facility scan loop."""
    return get_scheduler_status()


# ---------------------------------------------------------------------------
# Lysted listings — the listing team's CSV export replaces ReachPro as the
# source of "what do we have listed right now" (2026-09-04 call). Listings are
# checked against the buying platforms every few hours; anything that has just
# sold out at source is alerted for deactivation. See lysted_listings.py and
# lysted_scan_runner.py.
# ---------------------------------------------------------------------------

_lysted_scan_lock = threading.Lock()
_lysted_scan_status: dict = {"status": "idle"}


def _run_lysted_scan_background(event_limit: Optional[int]) -> None:
    global _lysted_scan_status
    with _lysted_scan_lock:
        _lysted_scan_status = {"status": "running"}
    try:
        summary = run_and_persist_lysted_scan(notify=True, event_limit=event_limit)
        with _lysted_scan_lock:
            _lysted_scan_status = dict(summary)
    except Exception as exc:
        logging.getLogger(__name__).error("Lysted scan failed: %s", exc)
        with _lysted_scan_lock:
            _lysted_scan_status = {"status": "failed", "error": str(exc)}


def _upload_dict(up) -> Optional[dict]:
    if up is None:
        return None
    return {
        "id": up.id,
        "filename": up.filename,
        "uploaded_at": up.uploaded_at.isoformat() if up.uploaded_at else None,
        "row_count": up.row_count,
        "active_count": up.active_count,
        "skipped_count": up.skipped_count,
        "warnings": (up.warnings or "").split("\n") if up.warnings else [],
    }


@app.post("/api/lysted/upload")
async def lysted_upload(file: UploadFile = File(...)):
    """
    Upload a Lysted inventory export (CSV). Becomes the current active
    listing set; the previous upload is kept as history.
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    try:
        return save_upload(file.filename or "lysted-export.csv", content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/lysted/upload/latest")
def lysted_latest_upload(db: Session = Depends(get_db)):
    return {"upload": _upload_dict(get_latest_upload(db))}


@app.get("/api/lysted/listings")
def lysted_listings(db: Session = Depends(get_db)):
    """
    The latest upload's live listings, each with its most recent SpotHero and
    ParkWhiz reading (or none, if not checked since upload).
    """
    up = get_latest_upload(db)
    if up is None:
        return {"upload": None, "listings": []}

    rows = (
        db.query(LystedListing)
        .filter(LystedListing.upload_id == up.id, LystedListing.is_active == True)  # noqa: E712
        .order_by(LystedListing.event_date, LystedListing.event_name)
        .all()
    )

    subq = (
        db.query(
            ScarcityCheck.reachpro_listing_id,
            ScarcityCheck.platform,
            func.max(ScarcityCheck.checked_at).label("max_checked_at"),
        )
        .filter(ScarcityCheck.source == "lysted")
        .group_by(ScarcityCheck.reachpro_listing_id, ScarcityCheck.platform)
        .subquery()
    )
    checks = (
        db.query(ScarcityCheck)
        .join(
            subq,
            (ScarcityCheck.reachpro_listing_id == subq.c.reachpro_listing_id)
            & (ScarcityCheck.platform == subq.c.platform)
            & (ScarcityCheck.checked_at == subq.c.max_checked_at),
        )
        .all()
    )
    by_key = {(c.reachpro_listing_id, c.platform): c for c in checks}

    def _reading(c):
        if c is None:
            return None
        return {
            "scarcity_level": c.scarcity_level,
            "is_available": c.is_available,
            "price": c.price,
            "spots_left": c.spots_left,
            "capacity": c.capacity,
            "percent_remaining": c.percent_remaining,
            "availability_status": c.availability_status,
            "error": c.error,
            "checked_at": c.checked_at.isoformat() if c.checked_at else None,
        }

    out = []
    for r in rows:
        sh = by_key.get((r.listing_key, "spothero"))
        pw = by_key.get((r.listing_key, "parkwhiz"))
        stamps = [c.checked_at for c in (sh, pw) if c is not None and c.checked_at]
        out.append({
            "listing_key": r.listing_key,
            "event_key": r.event_key,
            "event_name": r.event_name,
            "event_name_raw": r.event_name_raw,
            "event_date": r.event_date,
            "event_date_raw": r.event_date_raw,
            "venue": r.venue,
            "city": r.city,
            "state": r.state,
            "section": r.section,
            "row": r.row,
            "quantity": r.quantity,
            "list_price": r.list_price,
            "total_cost": r.total_cost,
            "status": r.status,
            "broadcast": r.broadcast,
            "spothero": _reading(sh),
            "parkwhiz": _reading(pw),
            "last_checked_at": max(stamps).isoformat() if stamps else None,
        })
    return {"upload": _upload_dict(up), "listings": out}


@app.post("/api/lysted/scan")
def trigger_lysted_scan(event_limit: Optional[int] = Query(None)):
    """Check the latest upload's listings against the buying platforms now (background)."""
    current = get_current_run()
    if current and current.get("status") == "running":
        return {"status": "already_running", "run": current}
    with _lysted_scan_lock:
        if _lysted_scan_status.get("status") == "running":
            return {"status": "already_running", "run": _lysted_scan_status}
    if get_latest_upload() is None:
        return {"status": "no_upload"}

    threading.Thread(target=_run_lysted_scan_background, kwargs={"event_limit": event_limit}, daemon=True).start()
    return {"status": "started"}


@app.get("/api/lysted/scan/status")
def lysted_scan_status():
    with _lysted_scan_lock:
        status = dict(_lysted_scan_status)
    if status.get("status") == "running":
        # progress comes from the shared orchestrator run
        current = get_current_run() or {}
        status.update({k: current.get(k) for k in ("checked", "total_listings", "eta_seconds", "limited_or_sold_out")})
    return status


@app.get("/api/lysted/scheduler-status")
def lysted_scheduler_status():
    return get_lysted_scheduler_status()


@app.get("/api/lysted/sold-out-alerts")
def lysted_sold_out_alerts(
    latest_run_only: bool = Query(False),
    limit: int = Query(200, le=1000),
    db: Session = Depends(get_db),
):
    """Listings that went sold-out at source, most recent first. See facility_price_spikes for latest_run_only."""
    q = db.query(LystedSoldOutAlert)
    if latest_run_only:
        q = _only_latest_run(q, LystedSoldOutAlert)
    rows = q.order_by(LystedSoldOutAlert.detected_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "listing_key": r.listing_key,
            "event_name": r.event_name,
            "event_date": r.event_date,
            "venue": r.venue,
            "city": r.city,
            "state": r.state,
            "section": r.section,
            "platforms": r.platforms,
            "quantity": r.quantity,
            "list_price": r.list_price,
            "previous_levels": r.previous_levels,
            "detected_at": r.detected_at.isoformat() if r.detected_at else None,
            "notified": r.notified,
        }
        for r in rows
    ]
