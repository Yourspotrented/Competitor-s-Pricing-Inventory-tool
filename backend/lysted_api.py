"""
Our Lysted inventory read straight from Lysted's API, as the automatic
alternative to the listing team exporting and uploading a CSV.

Both sources write the same rows through lysted_listings.save_rows, and the
most recent snapshot is the active set. So:
  - an API sync runs before every scheduled Lysted scan and refreshes the set;
  - a CSV uploaded by hand becomes the set immediately, until the next sync;
  - if the API is unavailable (no token, token expired, request refused), the
    sync is skipped and the last good snapshot — API or CSV — keeps being used.
A failed sync never stops a scan.

Authentication
--------------
LYSTED_API_TOKEN in backend/.env holds a bearer token for api.lysted.com.
Today that is a copy of a browser login session (issued by Automatiq's Auth0,
who run Lysted): it expires after 24 hours and has no API permissions of its
own. That is fine for testing, not for unattended running — ask Lysted for an
API key or service account and put it here instead. When the token is expired
this module says so and skips, rather than failing every request.

This is Lysted's private web-app API, not a published one; it can change
without notice.

How the inventory is fetched
----------------------------
GET /api/listings/v2 only lists events (venue, city, state — no lots), which
the scan can't use: it matches on our lot. So the sync takes the Inventory
page's CSV export instead (fetch_export_csv), which is byte-for-byte the file
the listing team downloads by hand, and feeds it through the same
lysted_listings.save_upload as a manual upload. fetch_events is kept for
status and diagnostics.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

API_BASE = "https://api.lysted.com/api"
TOKEN_ENV = "LYSTED_API_TOKEN"
PAGE_LIMIT = 50
_REQUEST_GAP_SECONDS = 0.5   # be a polite client on an API we don't own
_MAX_PAGES = 40              # 2,000 events; a runaway guard, not a real limit


class LystedApiUnavailable(Exception):
    """The API can't be used right now — reason is in the message."""


def _token() -> str:
    """
    The token to use: whatever was pasted most recently, else the env var.

    The dashboard paste wins because it is the one someone can refresh daily
    without a redeploy; LYSTED_API_TOKEN stays supported for local testing and
    for the day Lysted issues a real key.
    """
    stored = stored_token()
    return stored or os.getenv(TOKEN_ENV, "").strip()


def stored_token() -> str:
    import credentials
    return credentials.stored("lysted")


def save_token(token: str, saved_by: str = "") -> Dict[str, Any]:
    """
    Store a freshly pasted token. Rejects anything that isn't a JWT we can
    read an expiry out of, so a mistyped paste fails here rather than silently
    breaking every sync for a day.
    """
    import credentials

    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise ValueError("no token given")
    expires = token_expires_at(token)
    if expires is None:
        raise ValueError("that doesn't look like a Lysted token — no expiry could be read from it")
    if expires <= datetime.now(timezone.utc):
        raise ValueError(f"that token expired at {expires.strftime('%Y-%m-%d %H:%M UTC')} — copy a fresh one")

    saved = credentials.save("lysted", token, expires.replace(tzinfo=None), saved_by)
    return {**saved, "expires_at": expires.isoformat(),
            "hours_left": round((expires - datetime.now(timezone.utc)).total_seconds() / 3600, 1)}


def token_expires_at(token: str) -> Optional[datetime]:
    """
    The token's expiry, read from its own payload. Not a signature check —
    only used to skip cleanly instead of sending a request we know will fail.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return datetime.fromtimestamp(exp, timezone.utc) if exp else None
    except Exception:
        return None


def api_status() -> Dict[str, Any]:
    """Whether a sync could run right now, without calling the API."""
    token = _token()
    if not token:
        return {"configured": False, "usable": False, "source": None,
                "reason": "no Lysted token saved — paste one on the dashboard"}
    exp = token_expires_at(token)
    now = datetime.now(timezone.utc)
    expired = exp is not None and exp <= now
    return {
        "configured": True,
        "usable": not expired,
        "source": "dashboard" if stored_token() else "env",
        "expires_at": exp.isoformat() if exp else None,
        "hours_left": round((exp - now).total_seconds() / 3600, 1) if exp and not expired else 0,
        "reason": "token expired — paste a fresh one on the dashboard" if expired else None,
    }


def _headers(token: str) -> Dict[str, str]:
    return {
        "accept": "application/json",
        "authorization": f"Bearer {token}",
        # The API sits behind the web app and expects to be called from it.
        "origin": "https://app.lysted.com",
        "referer": "https://app.lysted.com/",
        "user-agent": "competitors-data-finder/1.0",
    }


def _get(path: str, params: Dict[str, Any]) -> Any:
    status = api_status()
    if not status["usable"]:
        raise LystedApiUnavailable(status["reason"])
    try:
        resp = requests.get(f"{API_BASE}{path}", params=params, headers=_headers(_token()), timeout=30)
    except requests.RequestException as exc:
        raise LystedApiUnavailable(f"request failed: {exc}") from exc
    if resp.status_code in (401, 403):
        # 403 is also what this API answers for routes it won't serve, so
        # don't guess further — report and stop.
        raise LystedApiUnavailable(f"HTTP {resp.status_code}: {resp.text[:160]}")
    if not resp.ok:
        raise LystedApiUnavailable(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise LystedApiUnavailable("response was not JSON") from exc


def fetch_events() -> List[Dict[str, Any]]:
    """
    Every event on the account, across all pages, as
    {event_id, event_name, event_date, event_type, venue, city, state,
     timezone, listings, tickets, stubhub_url, vivid_url}.
    """
    events: List[Dict[str, Any]] = []
    page, last_page = 1, 1
    while page <= min(last_page, _MAX_PAGES):
        data = _get("/listings/v2", {"page": page, "limit": PAGE_LIMIT, "search": "",
                                     "dateOffset": 0, "selectedEventId": ""})
        last_page = int(data.get("lastPage") or 1)
        for row in data.get("rows") or []:
            ev, venue = row.get("event") or {}, row.get("venue") or {}
            events.append({
                "event_id": ev.get("id"),
                "event_name": ev.get("name"),
                "event_date": ev.get("date"),
                "event_type": ev.get("type"),
                "stubhub_url": ev.get("stubhub_url"),
                "vivid_url": ev.get("vivid_url"),
                "venue": venue.get("name"),
                "city": venue.get("city"),
                "state": venue.get("state"),
                "timezone": venue.get("timezone"),
                "listings": row.get("listings"),
                "tickets": row.get("tickets"),
            })
        page += 1
        if page <= last_page:
            time.sleep(_REQUEST_GAP_SECONDS)
    return events


def upcoming(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Events that haven't happened yet — past ones can't be sold or checked."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    return [e for e in events if (e.get("event_date") or "") >= now]


EXPORT_READY_TIMEOUT_SECONDS = 180   # Lysted builds the file in the background
SOCKET_LOGIN_TIMEOUT_SECONDS = 20


def fetch_export_csv() -> bytes:
    """
    The account's full inventory as the same CSV the Inventory page's
    download button produces — every column, every listing, lots included.

    Lysted builds exports asynchronously, so this mirrors what the web app
    does (traced from the browser):
      1. open the app's socket.io connection and send the login token as an
         "auth" event; the server answers MSG "You've logged on";
      2. GET /api/listings/download, which returns {"key": <job id>} at once;
      3. wait for a BKGPROC event carrying that key — its output.url is a
         short-lived signed S3 link to the finished file;
      4. download the file.
    The listing page's page/limit parameters don't cap the export: one file
    holds every listing.

    Raises LystedApiUnavailable on any failure.
    """
    import socketio   # python-socketio[client]; imported here so the module loads without it

    status = api_status()
    if not status["usable"]:
        raise LystedApiUnavailable(status["reason"])
    token = _token()

    logged_on = threading.Event()
    finished: Dict[str, Dict[str, Any]] = {}   # BKGPROC payloads by job key
    finished_cv = threading.Condition()

    sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)

    @sio.event
    def connect():
        sio.emit("auth", token)

    @sio.on("MSG")
    def on_msg(data):
        if isinstance(data, dict) and "logged on" in str(data.get("message", "")):
            logged_on.set()

    @sio.on("BKGPROC")
    def on_background_done(data):
        # Other background jobs on the account arrive on the same channel —
        # keep them all and pick ours out by key below. Storing rather than
        # checking on arrival also covers the file finishing before the
        # download request has even returned its key.
        if isinstance(data, dict) and data.get("key"):
            with finished_cv:
                finished[str(data["key"])] = data
                finished_cv.notify_all()

    try:
        try:
            sio.connect("https://api.lysted.com", socketio_path="api/socket.io",
                        transports=["websocket"], headers={"Origin": "https://app.lysted.com"},
                        wait_timeout=SOCKET_LOGIN_TIMEOUT_SECONDS)
        except Exception as exc:
            raise LystedApiUnavailable(f"could not open Lysted's live connection: {exc}") from exc
        if not logged_on.wait(SOCKET_LOGIN_TIMEOUT_SECONDS):
            raise LystedApiUnavailable("Lysted did not accept the token on its live connection")

        job = _get("/listings/download", {"page": 1, "limit": PAGE_LIMIT, "search": "",
                                          "dateOffset": 0, "selectedEventId": ""})
        key = str((job or {}).get("key") or "")
        if not key:
            raise LystedApiUnavailable(f"export request returned no job key: {str(job)[:120]}")

        with finished_cv:
            done = finished_cv.wait_for(lambda: key in finished, timeout=EXPORT_READY_TIMEOUT_SECONDS)
            result = finished.get(key)
        if not done or result is None:
            raise LystedApiUnavailable(f"export {key} not ready after {EXPORT_READY_TIMEOUT_SECONDS}s")
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass

    output = result.get("output") or {}
    url = output.get("url")
    if not (result.get("success") and output.get("success") and url):
        raise LystedApiUnavailable(f"export {key} failed: {str(output.get('stderr') or result)[:160]}")

    try:
        resp = requests.get(url, timeout=60)   # signed link: no auth header, and don't log it
    except requests.RequestException as exc:
        raise LystedApiUnavailable(f"downloading export {key} failed: {exc}") from exc
    if not resp.ok or not resp.content:
        raise LystedApiUnavailable(f"downloading export {key}: HTTP {resp.status_code}")
    return resp.content


# ---------------------------------------------------------------------------
# Writing back: taking a listing off sale
# ---------------------------------------------------------------------------

def set_broadcast(listing_id: str, broadcast: bool = False, dry_run: bool = True) -> Dict[str, Any]:
    """
    Turn a Lysted listing's broadcast on or off — their word for deactivating.

    This is the request the Lysted web app sends when the "Not Broadcasting"
    switch is flipped and Save Changes is pressed (captured 2026-09-25):

        PUT https://api.lysted.com/api/listings/<listing id>
        body: {"broadcast": false}

    The response comes back with "broadcast": false and "status": "READY",
    which is exactly the pair the inventory export calls not-live — the same
    test lysted_listings.is_live applies when deciding what to scan. So a
    listing deactivated this way disappears from our own active set on the
    next sync, with no special handling.

    Quantity is deliberately not touched: Leticia (2026-09-25) says a
    listing's quantity cannot be changed once created. The response does carry
    a separate "shownQuantity" field, which may be how fewer passes are
    offered than held, but nobody has confirmed that and guessing at it on
    live inventory is not worth the risk.

    dry_run=True (the default) reports what would be sent and sends nothing.
    """
    listing_id = str(listing_id or "").strip()
    if not listing_id:
        return {"ok": False, "dry_run": dry_run, "reason": "no Lysted listing id"}

    url = f"{API_BASE}/listings/{listing_id}"
    payload = {"broadcast": bool(broadcast)}
    if dry_run:
        return {"ok": True, "dry_run": True, "would_put": url, "body": payload,
                "listing_id": listing_id}

    status = api_status()
    if not status["usable"]:
        return {"ok": False, "dry_run": False, "listing_id": listing_id, "reason": status["reason"]}
    try:
        resp = requests.put(url, json=payload, headers=_headers(_token()), timeout=30)
    except requests.RequestException as exc:
        return {"ok": False, "dry_run": False, "listing_id": listing_id, "reason": f"request failed: {exc}"}
    if resp.status_code in (401, 403):
        return {"ok": False, "dry_run": False, "listing_id": listing_id,
                "reason": f"Lysted refused it (HTTP {resp.status_code}) — token expired, or no write permission"}
    if not resp.ok:
        return {"ok": False, "dry_run": False, "listing_id": listing_id,
                "reason": f"HTTP {resp.status_code}: {resp.text[:160]}"}
    logger.info("Lysted: listing %s broadcast set to %s", listing_id, broadcast)
    return {"ok": True, "dry_run": False, "listing_id": listing_id, "broadcast": broadcast}


def sync_from_api() -> Dict[str, Any]:
    """
    Refresh the active Lysted inventory from the API. Never raises.

    Returns {"status": "synced", ...upload summary} on success, otherwise
    {"status": "skipped" | "unavailable", "reason": ...} and the current
    active set is left exactly as it was.
    """
    from lysted_listings import save_upload

    status = api_status()
    if not status["configured"]:
        return {"status": "skipped", "reason": status["reason"]}
    if not status["usable"]:
        logger.warning("Lysted API sync skipped: %s", status["reason"])
        return {"status": "skipped", "reason": status["reason"], "expires_at": status["expires_at"]}

    try:
        content = fetch_export_csv()
    except LystedApiUnavailable as exc:
        logger.warning("Lysted API sync failed: %s", exc)
        return {"status": "unavailable", "reason": str(exc)}

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        summary = save_upload(f"Lysted API sync {stamp}.csv", content)
    except ValueError as exc:
        # Wrong shape — Lysted changed the export. Keep the last good set.
        logger.error("Lysted API sync: export was not a usable inventory file: %s", exc)
        return {"status": "unavailable", "reason": f"export format changed: {exc}"}

    logger.info("Lysted API sync saved %d listings (%d live)", summary["row_count"], summary["active_count"])
    return {"status": "synced", **summary}
