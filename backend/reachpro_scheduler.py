"""
Background scheduler for the ReachPro flow — same shape as lysted_scheduler.py.
Reruns run_and_persist_reachpro_scan on a fixed interval (default every 2 hours,
via REACHPRO_SCAN_INTERVAL_HOURS in .env).

Skips a tick if a scan is already in flight anywhere in the app (the manual
/api/scan button, the Lysted scheduler, or a manual ReachPro scan) —
orchestrator.run_scan() tracks exactly one run at a time via a single global
_current_run/_stop_requested pair (orchestrator.py), so two calls in flight
together would corrupt that shared state and double up on live API traffic to
SpotHero/ParkWhiz/ReachPro. The manual /api/scan and /api/reachpro/scan
endpoints already guard themselves this way; this gives the scheduler the same
protection.

Usage (called once at app startup, see main.py):
    from reachpro_scheduler import start_reachpro_scheduler
    start_reachpro_scheduler()
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from reachpro_scan_runner import run_and_persist_reachpro_scan

logger = logging.getLogger(__name__)

REACHPRO_DEFAULT_INTERVAL_HOURS = 2.0

_scheduler_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_status_lock = threading.Lock()
_status: dict = {"status": "not_started"}


def get_reachpro_scheduler_status() -> dict:
    with _status_lock:
        return dict(_status)


def _interval_seconds() -> float:
    raw = os.getenv("REACHPRO_SCAN_INTERVAL_HOURS", "").strip()
    hours = float(raw) if raw else REACHPRO_DEFAULT_INTERVAL_HOURS
    return max(hours, 0.05) * 3600


def _run_loop() -> None:
    global _status
    from orchestrator import get_current_run   # lazy: orchestrator configures logging at import
    interval = _interval_seconds()
    logger.info("ReachPro scan scheduler started — running every %.2f hour(s)", interval / 3600)

    while not _stop_event.is_set():
        current = get_current_run()
        if current and current.get("status") == "running":
            logger.info("ReachPro scheduled scan skipped — a scan is already running (source=%s)",
                        current.get("source"))
            with _status_lock:
                _status = {
                    "status": "idle", "interval_hours": round(interval / 3600, 2),
                    "last_run_summary": _status.get("last_run_summary"),
                    "last_run_ok": _status.get("last_run_ok"),
                    "skipped": "another scan was already running",
                }
        else:
            with _status_lock:
                _status = {"status": "running", "interval_hours": round(interval / 3600, 2)}
            try:
                summary = run_and_persist_reachpro_scan(notify=True)
                with _status_lock:
                    _status = {
                        "status": "idle", "interval_hours": round(interval / 3600, 2),
                        "last_run_summary": summary, "last_run_ok": True,
                    }
            except Exception as exc:
                logger.error("Scheduled ReachPro scan failed: %s", exc)
                with _status_lock:
                    _status = {
                        "status": "idle", "interval_hours": round(interval / 3600, 2),
                        "last_run_error": str(exc), "last_run_ok": False,
                    }

        waited = 0.0
        while waited < interval and not _stop_event.is_set():
            chunk = min(5.0, interval - waited)
            time.sleep(chunk)
            waited += chunk


def start_reachpro_scheduler() -> None:
    """Start the background ReachPro scan loop, if not already running. Safe to call multiple times."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_run_loop, daemon=True)
    _scheduler_thread.start()


def stop_reachpro_scheduler() -> None:
    _stop_event.set()
