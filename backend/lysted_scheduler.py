"""
Background scheduler for the Lysted flow — same shape as
facility_scheduler.py / events_scheduler.py. Reruns
run_and_persist_lysted_scan on a fixed interval (default every 3 hours, via
LYSTED_SCAN_INTERVAL_HOURS in .env). A no-op until an export has been
uploaded.

Usage (called once at app startup, see main.py):
    from lysted_scheduler import start_lysted_scheduler
    start_lysted_scheduler()
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from lysted_scan_runner import LYSTED_DEFAULT_INTERVAL_HOURS, run_and_persist_lysted_scan

logger = logging.getLogger(__name__)

_scheduler_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_status_lock = threading.Lock()
_status: dict = {"status": "not_started"}


def get_lysted_scheduler_status() -> dict:
    with _status_lock:
        return dict(_status)


def _interval_seconds() -> float:
    raw = os.getenv("LYSTED_SCAN_INTERVAL_HOURS", "").strip()
    hours = float(raw) if raw else LYSTED_DEFAULT_INTERVAL_HOURS
    return max(hours, 0.05) * 3600


def _run_loop() -> None:
    global _status
    interval = _interval_seconds()
    logger.info("Lysted scan scheduler started — running every %.2f hour(s)", interval / 3600)

    while not _stop_event.is_set():
        with _status_lock:
            _status = {"status": "running", "interval_hours": round(interval / 3600, 2)}
        try:
            summary = run_and_persist_lysted_scan(notify=True)
            with _status_lock:
                _status = {
                    "status": "idle", "interval_hours": round(interval / 3600, 2),
                    "last_run_summary": summary, "last_run_ok": True,
                }
        except Exception as exc:
            logger.error("Scheduled Lysted scan failed: %s", exc)
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


def start_lysted_scheduler() -> None:
    """Start the background Lysted scan loop, if not already running. Safe to call multiple times."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_run_loop, daemon=True)
    _scheduler_thread.start()


def stop_lysted_scheduler() -> None:
    _stop_event.set()
