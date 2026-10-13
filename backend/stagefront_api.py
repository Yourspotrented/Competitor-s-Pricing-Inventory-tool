"""
Stagefront's own ticket-and-parking portal API — the source for "what do we
have listed on Stagefront right now", parallel to lysted_api.py.

Authentication
--------------
STAGEFRONT_API_TOKEN in backend/.env holds a bearer JWT for
portal.stagefront.com. Like Lysted's, this is a copy of a browser login
session (captured 2026-10-13): it expires within about a day and carries no
API-key-style permissions of its own. The dashboard-pasted credential
(credentials.py, source="stagefront") takes priority over the env var, same
as Lysted/ReachPro, so the team can refresh it without a redeploy.

How listings are read
----------------------
Two calls, both captured from the portal's own network traffic — this is
Stagefront's private web-app API, not a published one; it can change without
notice.
  1. QueryEvents — every open event (Event_ID, name, venue, date/time, and
     aggregate counts) from a given date forward. No listing/lot detail, and
     no page/limit parameter was observed on a real call — it appears to
     return everything in one shot. Worth revisiting if a very large
     portfolio ever makes that look incomplete.
  2. QueryEventInventoryAll — one call per event, keyed by Event_ID. Returns
     one row per physical pass ("Item"); several share one Listing_ID — that
     grouping is what actually identifies a lot (Section), price and status.

Deactivating a listing has no captured endpoint yet, so this module only
reads — see docs/PROJECT_KNOWLEDGE.md for what's still missing.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

API_BASE = "https://portal.stagefront.com/api/Pricing"
TOKEN_ENV = "STAGEFRONT_API_TOKEN"
COMPANY_NAME_ENV = "STAGEFRONT_COMPANY_NAME"
DEFAULT_COMPANY_NAME = "MaxParkArbLLC (Max Lawrence)"

# Politeness between per-event inventory calls — same order of magnitude as
# the SpotHero/ParkWhiz throttling and Lysted's per-event listing-id fetch.
_REQUEST_GAP_SECONDS = 0.3


class StagefrontApiUnavailable(Exception):
    """The API can't be used right now — reason is in the message."""


def _token() -> str:
    """The token to use: whatever was pasted most recently, else the env var."""
    stored = stored_token()
    return stored or os.getenv(TOKEN_ENV, "").strip()


def stored_token() -> str:
    import credentials
    return credentials.stored("stagefront")


def _company_name() -> str:
    return os.getenv(COMPANY_NAME_ENV, "").strip() or DEFAULT_COMPANY_NAME


def token_expires_at(token: str) -> Optional[datetime]:
    """The token's expiry, read from its own JWT payload. Not a signature check."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return datetime.fromtimestamp(exp, timezone.utc) if exp else None
    except Exception:
        return None


def save_token(token: str, saved_by: str = "") -> Dict[str, Any]:
    """
    Store a freshly pasted token. Rejects anything that isn't a JWT we can
    read an expiry out of, so a mistyped paste fails here rather than
    silently breaking every sync until someone notices.
    """
    import credentials

    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise ValueError("no token given")
    expires = token_expires_at(token)
    if expires is None:
        raise ValueError("that doesn't look like a Stagefront token — no expiry could be read from it")
    if expires <= datetime.now(timezone.utc):
        raise ValueError(f"that token expired at {expires.strftime('%Y-%m-%d %H:%M UTC')} — copy a fresh one")

    saved = credentials.save("stagefront", token, expires.replace(tzinfo=None), saved_by)
    return {**saved, "expires_at": expires.isoformat(),
            "hours_left": round((expires - datetime.now(timezone.utc)).total_seconds() / 3600, 1)}


def api_status() -> Dict[str, Any]:
    """Whether a sync could run right now, without calling the API."""
    token = _token()
    if not token:
        return {"configured": False, "usable": False, "source": None,
                "reason": "no Stagefront token saved — paste one on the dashboard"}
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
        "content-type": "application/json",
        # The API sits behind the portal web app and expects to be called from it.
        "origin": "https://portal-ui.stagefront.com",
        "user-agent": "competitors-data-finder/1.0",
    }


def _post(path: str, body: Dict[str, Any]) -> Any:
    status = api_status()
    if not status["usable"]:
        raise StagefrontApiUnavailable(status["reason"])
    try:
        resp = requests.post(f"{API_BASE}/{path}", params={"_dc": int(time.time() * 1000)},
                             json=body, headers=_headers(_token()), timeout=30)
    except requests.RequestException as exc:
        raise StagefrontApiUnavailable(f"request failed: {exc}") from exc
    if resp.status_code in (401, 403):
        raise StagefrontApiUnavailable(f"HTTP {resp.status_code}: {resp.text[:160]}")
    if not resp.ok:
        raise StagefrontApiUnavailable(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise StagefrontApiUnavailable("response was not JSON") from exc


def fetch_events(from_date: Optional[date] = None, show_sold: bool = True) -> List[Dict[str, Any]]:
    """
    Every event from from_date (default: today) forward, as QueryEvents
    returns it — no listing/lot detail. See the module docstring re: the
    unconfirmed assumption that this call isn't paginated.
    """
    from_date = from_date or datetime.now().date()
    body = {
        "CompanyName": _company_name(),
        # A captured real call had this true; it looked like it forces a
        # backend recompute on Stagefront's side rather than just reading a
        # cache, so routine scans leave it off.
        "ForceRefresh": False,
        "ShowSold": show_sold,
        "skipCache": False,
        "skipSavingCache": False,
        "EventFromDate": from_date.strftime("%m/%d/%Y"),
    }
    data = _post("QueryEvents", body)
    events = data.get("Events") or []
    logger.info("Stagefront: QueryEvents returned %d event(s)", len(events))
    return events


def fetch_event_inventory(event_id: Any) -> List[Dict[str, Any]]:
    """Every item (physical pass) listed on one event, tagged with its Listing_ID and Section."""
    body = {"EventId": event_id, "CompanyName": _company_name(), "skipCache": False}
    data = _post("QueryEventInventoryAll", body)
    return list(data.get("EventInventory") or [])


def fetch_all_inventory(events: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    {event_id: [item, ...]} for every event worth checking.

    Only parking events that still have something open are fetched — calling
    this for a sold-out or non-parking event would cost a request for
    nothing. One request per remaining event, paced by _REQUEST_GAP_SECONDS.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    worth_checking = [
        e for e in events
        if (e.get("Cat_Desc") or "").strip().lower() == "parking"
        and (e.get("Total_Listings") or 0) > 0
        and (e.get("Total_Open") or 0) > 0
    ]
    for i, event in enumerate(worth_checking):
        event_id = event.get("Event_ID")
        if event_id is None:
            continue
        try:
            out[str(event_id)] = fetch_event_inventory(event_id)
        except StagefrontApiUnavailable as exc:
            logger.warning("Stagefront: could not read inventory for event %s — %s", event_id, exc)
        if i + 1 < len(worth_checking):
            time.sleep(_REQUEST_GAP_SECONDS)
    logger.info("Stagefront: read inventory for %d of %d parking event(s)", len(out), len(worth_checking))
    return out
