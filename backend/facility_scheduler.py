"""
Background scheduler that reruns the facility competitor scan on a fixed
interval (default hourly, via FACILITY_SCAN_INTERVAL_HOURS in .env) — the
"continuously monitors" piece, as opposed to the manual "Run Facility Scan"
button which still works independently.

A plain daemon thread + sleep loop, not APScheduler or a task queue: this
app is a single FastAPI process with one scheduled job, so a dependency
for a general-purpose scheduler isn't warranted — matches the existing
codebase's use of plain threading.Thread for the background scan/import
jobs in main.py.

Usage (called once at app startup, see main.py):
    from facility_scheduler import start_scheduler
    start_scheduler()
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from clustering import DEFAULT_RADIUS_MILES
from facility_scan_runner import run_and_persist_facility_scan

logger = logging.getLogger(__name__)

_scheduler_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_status_lock = threading.Lock()
_status: dict = {"status": "not_started"}


def get_scheduler_status() -> dict:
    with _status_lock:
        return dict(_status)


def _interval_seconds() -> float:
    hours = float(os.getenv("FACILITY_SCAN_INTERVAL_HOURS", "1").strip() or "1")
    return max(hours, 0.05) * 3600  # floor at 3 min so a typo like "0" can't busy-loop


def _run_loop(radius_miles: float, start_hour: int, end_hour: int) -> None:
    global _status
    interval = _interval_seconds()
    logger.info("Facility scan scheduler started — running every %.2f hour(s)", interval / 3600)

    while not _stop_event.is_set():
        with _status_lock:
            _status = {"status": "running", "interval_hours": round(interval / 3600, 2)}
        try:
            summary = run_and_persist_facility_scan(
                radius_miles=radius_miles, start_hour=start_hour, end_hour=end_hour, notify=True,
            )
            with _status_lock:
                _status = {
                    "status": "idle", "interval_hours": round(interval / 3600, 2),
                    "last_run_summary": summary, "last_run_ok": True,
                }
        except Exception as exc:
            logger.error("Scheduled facility scan failed: %s", exc)
            with _status_lock:
                _status = {
                    "status": "idle", "interval_hours": round(interval / 3600, 2),
                    "last_run_error": str(exc), "last_run_ok": False,
                }

        # Wait in small chunks so stop_scheduler() doesn't block for the full interval.
        waited = 0.0
        while waited < interval and not _stop_event.is_set():
            chunk = min(5.0, interval - waited)
            time.sleep(chunk)
            waited += chunk


def start_scheduler(radius_miles: float = DEFAULT_RADIUS_MILES, start_hour: int = 10, end_hour: int = 22) -> None:
    """Start the background scan loop, if not already running. Safe to call multiple times."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _stop_event.clear()
    _scheduler_thread = threading.Thread(
        target=_run_loop, kwargs={"radius_miles": radius_miles, "start_hour": start_hour, "end_hour": end_hour},
        daemon=True,
    )
    _scheduler_thread.start()


def stop_scheduler() -> None:
    _stop_event.set()
