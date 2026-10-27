"""
Background scheduler for the Lysted flow — same shape as
facility_scheduler.py / events_scheduler.py. Reruns
run_and_persist_lysted_scan on a fixed interval (default every hour, via
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
        summary = None
        last_error = None
        for attempt in range(1, 4):
            try:
                summary = run_and_persist_lysted_scan(notify=True)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if "locked" in str(exc).lower() and attempt < 3:
                    logger.warning("Lysted scan hit DB lock on attempt %d/3, retrying in 10s...", attempt)
                    time.sleep(10.0)
                else:
                    break

        if last_error is None and summary is not None:
            with _status_lock:
                _status = {
                    "status": "idle", "interval_hours": round(interval / 3600, 2),
                    "last_run_summary": summary, "last_run_ok": True,
                }
        else:
            logger.error("Scheduled Lysted scan failed: %s", last_error)
            with _status_lock:
                _status = {
                    "status": "idle", "interval_hours": round(interval / 3600, 2),
                    "last_run_error": str(last_error), "last_run_ok": False,
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
