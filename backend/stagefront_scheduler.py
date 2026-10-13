"""
Background scheduler for the Stagefront flow — same shape as
lysted_scheduler.py / facility_scheduler.py / events_scheduler.py. Reruns
run_and_persist_stagefront_scan on a fixed interval (default every 3 hours,
via STAGEFRONT_SCAN_INTERVAL_HOURS in .env). A no-op until a sync succeeds.

Both this scheduler and lysted_scheduler.py call orchestrator.run_scan,
which shares one global progress dict (orchestrator._current_run) across
every source — there's no lock around a whole scan, only around individual
reads/writes of that dict. Lysted's scheduler fires its first run
immediately on app startup, same as this one would, so every restart would
otherwise race both scans' progress into the same dict at once (the scan
data itself stays correct either way — each run writes through its own DB
session — but the dashboard's "checked X/Y" readout would jitter between
the two). _INITIAL_DELAY_SECONDS staggers this scheduler's first run so
that collision isn't routine; it's a one-sided fix in the new scheduler
rather than a lock change in shared orchestrator.py code.

Usage (called once at app startup, see main.py):
    from stagefront_scheduler import start_stagefront_scheduler
    start_stagefront_scheduler()
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from stagefront_scan_runner import STAGEFRONT_DEFAULT_INTERVAL_HOURS, run_and_persist_stagefront_scan

logger = logging.getLogger(__name__)

_scheduler_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_status_lock = threading.Lock()
_status: dict = {"status": "not_started"}

# See the module docstring: avoids this scheduler's first run landing in the
# same instant as lysted_scheduler's (both fire immediately on app startup).
_INITIAL_DELAY_SECONDS = 90.0


def get_stagefront_scheduler_status() -> dict:
    with _status_lock:
        return dict(_status)


def _interval_seconds() -> float:
    raw = os.getenv("STAGEFRONT_SCAN_INTERVAL_HOURS", "").strip()
    hours = float(raw) if raw else STAGEFRONT_DEFAULT_INTERVAL_HOURS
    return max(hours, 0.05) * 3600


def _run_loop() -> None:
    global _status
    interval = _interval_seconds()
    logger.info("Stagefront scan scheduler started — running every %.2f hour(s)", interval / 3600)

    with _status_lock:
        _status = {"status": "starting", "interval_hours": round(interval / 3600, 2)}
    if _stop_event.wait(_INITIAL_DELAY_SECONDS):
        return

    while not _stop_event.is_set():
        with _status_lock:
            _status = {"status": "running", "interval_hours": round(interval / 3600, 2)}
        try:
            summary = run_and_persist_stagefront_scan(notify=True)
            with _status_lock:
                _status = {
                    "status": "idle", "interval_hours": round(interval / 3600, 2),
                    "last_run_summary": summary, "last_run_ok": True,
                }
        except Exception as exc:
            logger.error("Scheduled Stagefront scan failed: %s", exc)
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


def start_stagefront_scheduler() -> None:
    """Start the background Stagefront scan loop, if not already running. Safe to call multiple times."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_run_loop, daemon=True)
    _scheduler_thread.start()


def stop_stagefront_scheduler() -> None:
    _stop_event.set()
