"""
Pull our own facility data from the team's Notion database ("Dashboard").

Obinna provided the credentials/database id for this — it's a lease/finance
tracker (rent, expenses, net remit per platform, lease dates), not a
purpose-built live-availability feed. It does have a per-facility 'Inventory'
number and a 'Facilty ID' field (typo intact in Notion) that matches our
sheet's Facility Id, so this pulls that field as-is.

What "Inventory" actually represents (total capacity vs. currently-available)
hasn't been confirmed with Obinna/Tatsav yet — reported under its own Notion
column name, not remapped to "Our total inv left", so nothing is silently
reinterpreted before that's confirmed.

Usage:
    from our_inventory import fetch_our_inventory
    rows = fetch_our_inventory()   # [{facility_id, facility_name, inventory, status, facility_status}, ...]
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

_PAGE_SIZE = 100


def _get_client():
    from notion_client import Client
    api_key = os.getenv("NOTION_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("NOTION_API_KEY env var is not set. Add it to backend/.env")
    return Client(auth=api_key)


def _get_data_source_id(notion, database_id: str) -> str:
    """
    This database uses Notion's newer data_sources model (a database can
    have one or more data sources; the classic /databases/{id}/query
    endpoint doesn't work for it). Retrieve the database to find its
    data_source id, then query goes through /data_sources/{id}/query.
    """
    db = notion.databases.retrieve(database_id=database_id)
    sources = db.get("data_sources") or []
    if not sources:
        raise RuntimeError(f"Notion database {database_id} has no data_sources — unexpected shape")
    return sources[0]["id"]


def _extract_title(prop: Optional[Dict[str, Any]]) -> str:
    if not prop:
        return ""
    return "".join(t.get("plain_text", "") for t in prop.get("title", []))


def _extract_select_name(prop: Optional[Dict[str, Any]]) -> Optional[str]:
    if not prop:
        return None
    sel = prop.get("select")
    return sel.get("name") if sel else None


def _extract_status_name(prop: Optional[Dict[str, Any]]) -> Optional[str]:
    if not prop:
        return None
    status = prop.get("status")
    return status.get("name") if status else None


def fetch_our_inventory(database_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Pull every row from the Notion "Dashboard" database, one dict per row:
      { facility_id, facility_name, inventory, status, facility_status }
    facility_id is None for rows missing a "Facilty ID" value — those are
    skipped by the caller when matching against our own facility sheet
    (see /api/our-inventory in main.py), not silently coerced to a guess.
    """
    database_id = database_id or os.getenv("NOTION_STALL_RENTS_DATABASE_ID", "").strip()
    if not database_id:
        raise RuntimeError("NOTION_STALL_RENTS_DATABASE_ID env var is not set. Add it to backend/.env")

    notion = _get_client()
    data_source_id = _get_data_source_id(notion, database_id)

    rows: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        body: Dict[str, Any] = {"page_size": _PAGE_SIZE}
        if cursor:
            body["start_cursor"] = cursor
        resp = notion.request(path=f"data_sources/{data_source_id}/query", method="POST", body=body)

        for page in resp.get("results", []):
            props = page.get("properties", {})
            facility_id_num = (props.get("Facilty ID") or {}).get("number")
            rows.append({
                "facility_id": str(int(facility_id_num)) if facility_id_num is not None else None,
                "facility_name": _extract_title(props.get("Facility")),
                "inventory": (props.get("Inventory") or {}).get("number"),
                "status": _extract_status_name(props.get("Status & Availability ")),
                "facility_status": _extract_select_name(props.get("Facility Status")),
            })

        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

    logger.info("Fetched %d rows from Notion (%d with a Facilty ID)",
                len(rows), sum(1 for r in rows if r["facility_id"]))
    return rows
