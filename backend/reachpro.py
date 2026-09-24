"""
Fetch our active listings from ReachPro.

Returns a flat list of listing dicts ready to be fed into platform checkers.
Re-uses the same API endpoints as the repricing tool but is fully self-contained.
"""

from __future__ import annotations

import os
import time
import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_CAMPAIGNS_URL = "https://reachpro.com/api/CampaignManager/GetCampaigns"

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

_DEFAULT_ACCOUNT_ID = os.getenv("ACTIVE_ACCOUNT_ID", "7a13a9c9-4ef4-492e-9eb0-19adcfa79da3").strip()

REGION_STATE_IDS: Dict[str, List[int]] = {
    "south_east": [18, 2, 22, 23, 25, 26, 28, 29, 3, 4, 48, 51],
    "mid_western": [11, 14, 21, 31, 33, 34, 38, 45, 6, 7, 8],
    "north_east": [10, 30, 32, 35, 36, 37, 49, 50, 9],
    "south_west": [13, 15, 16, 41, 43],
}


def _get_cookie() -> str:
    """
    The session cookie to authenticate with: whatever was pasted on the
    dashboard most recently, else REACHPRO_COOKIE from .env.

    ReachPro issues no API key, so this is a browser session that goes stale
    (every request then answers 401) and someone refreshes it by hand. The
    dashboard paste wins because it needs no redeploy.
    """
    import credentials
    cookie = credentials.current("reachpro", "REACHPRO_COOKIE")
    if not cookie:
        raise RuntimeError("No ReachPro cookie — paste one on the dashboard, or set REACHPRO_COOKIE in backend/.env")
    return cookie


def check_cookie(cookie: str = "", account_id: str = "") -> Dict[str, Any]:
    """
    Is this cookie usable? One cheap authenticated call against our own
    account, so a bad paste is rejected at paste time rather than silently
    breaking every scan until someone notices the 401s.
    """
    cookie = (cookie or "").strip() or _get_cookie()
    account_id = account_id or _DEFAULT_ACCOUNT_ID
    try:
        # The catalogue count, i.e. the first call a scan makes. GetCampaigns
        # is not a test: it answers 200 to an invalid cookie, so it would
        # wave a bad paste straight through.
        resp = requests.put(
            "https://reachpro.com/api/Catalog/GetCatalogCountForListings",
            headers=_headers(cookie, account_id),
            json={"eventTimeFrameFilter": "Future Events", "timezoneOffsetMins": -300,
                  "isListed": True, "stateProvinceIds": REGION_STATE_IDS["south_east"]},
            timeout=30,
        )
    except requests.RequestException as exc:
        return {"usable": False, "reason": f"could not reach ReachPro: {exc}"}
    if resp.status_code in (401, 403):
        return {"usable": False, "reason": "ReachPro rejected the cookie — copy a fresh one while logged in"}
    if not resp.ok:
        return {"usable": False, "reason": f"ReachPro answered HTTP {resp.status_code}"}
    return {"usable": True, "reason": None}


def _headers(cookie: str, account_id: str) -> Dict[str, str]:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "activeaccountid": account_id,
        "origin": "https://reachpro.com",
        "referer": "https://reachpro.com/inventory",
        "user-agent": "Mozilla/5.0",
        "Cookie": cookie,
    }


def _put(url: str, *, headers: dict, json: Any, timeout: float = 30.0, retries: int = 3) -> requests.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = requests.put(url, headers=headers, json=json, timeout=timeout)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10)) or (5 * (attempt + 1))
                logger.warning("Rate-limited, waiting %ds", wait)
                time.sleep(wait)
                continue
            return resp
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            time.sleep(3 * (attempt + 1))
    raise last_exc or RuntimeError("Request failed after retries")


def get_event_ids(region: str, cookie: str, account_id: str) -> List[str]:
    state_ids = REGION_STATE_IDS.get(region)
    if not state_ids:
        raise ValueError(f"Unknown region: {region}")
    resp = _put(
        "https://reachpro.com/api/Catalog/GetCatalogCountForListings",
        headers=_headers(cookie, account_id),
        json={
            "eventTimeFrameFilter": "Future Events",
            "timezoneOffsetMins": -300,
            "isListed": True,
            "stateProvinceIds": state_ids,
        },
    )
    resp.raise_for_status()
    return [str(item["viagVirtId"]) for item in resp.json().get("entityCnts", []) if "viagVirtId" in item]


def get_event_info(event_ids: List[str], cookie: str, account_id: str) -> Dict[str, Dict[str, str]]:
    if not event_ids:
        return {}
    result: Dict[str, Dict[str, str]] = {}
    chunk_size = 500
    for i in range(0, len(event_ids), chunk_size):
        chunk = event_ids[i: i + chunk_size]
        resp = _put(
            "https://reachpro.com/api/Catalog/GetCatalogSlim",
            headers=_headers(cookie, account_id),
            json=chunk,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        venues = {str(v.get("viagId")): v for v in data.get("venues", []) if isinstance(v, dict)}
        for ev in data.get("events", []):
            if not isinstance(ev, dict):
                continue
            vid = str(ev.get("virtId") or ev.get("viagId") or "")
            if not vid:
                continue
            venue = venues.get(str(ev.get("venId")), {})
            dates = ev.get("dates") or {}
            result[vid] = {
                "event_name": str(ev.get("name") or ev.get("shortName") or ""),
                "event_venue": str(venue.get("name") or ""),
                "event_date": str(dates.get("start") or ""),
            }
    return result


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def get_listings_for_events_bulk(event_ids: List[str], cookie: str, account_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Fetch listings for multiple events in a single API call.
    Returns { event_id: [listing, ...] }
    """
    if not event_ids:
        return {}
    resp = _put(
        "https://reachpro.com/api/Listing/GetListingsSectionalDataForEvents"
        "?dataToInclude=Basic&dataToInclude=Pricing&processListingGroup=true",
        headers=_headers(cookie, account_id),
        json={
            "listingQuery": {
                "eventOrMappingIds": event_ids,
                "timezoneOffsetMins": -300,
                "isListed": True,
                "oldPosEventIds": [],
            },
            "curListings": None,
        },
    )
    resp.raise_for_status()
    data = resp.json() or {}

    result: Dict[str, List[Dict[str, Any]]] = {}
    for event_id in event_ids:
        event_block = data.get(str(event_id)) or {}
        listings: List[Dict[str, Any]] = []
        for item in event_block.get("items") or []:
            for mkp in item.get("mkpListings") or []:
                listing_id = str(
                    mkp.get("mkpListingId") or mkp.get("listingId") or mkp.get("id") or ""
                )
                if not listing_id:
                    continue
                web_price = mkp.get("webPrice")
                if isinstance(web_price, dict):
                    web_price = web_price.get("amt") or web_price.get("disp")
                try:
                    web_price = float(web_price) if web_price is not None else None
                except (TypeError, ValueError):
                    web_price = None
                listings.append({
                    "listing_id": listing_id,
                    "section": str(mkp.get("overrideSection") or mkp.get("section") or ""),
                    "price": web_price,
                    "marketplace": str(mkp.get("mkp") or ""),
                    "id_on_mkp": str(mkp.get("idOnMkp") or ""),
                    "mkp_status": str(mkp.get("status") or ""),
                    # ReachPro is the system of record for passes we have
                    # bought (purchase orders become ticket groups), so
                    # "inventory in hand" is read straight off the listing
                    # rather than from a spreadsheet: unsoldQty is what is
                    # left after sales, availQty what can still be listed.
                    "quantity": _to_int(item.get("availQty")),
                    "unsold_qty": _to_int(item.get("unsoldQty")),
                    "sold_qty": _to_int(item.get("soldQty")),
                    "ticket_count": _to_int(item.get("ticketCnt")),
                    # The listing's own id and the actions ReachPro says it
                    # supports ("Unbroadcast" is the deactivation) — needed by
                    # whatever eventually writes back.
                    "inventory_listing_id": str(item.get("id") or ""),
                    "broadcast_status": str(item.get("brdcstStatus") or ""),
                    "actions": list(item.get("actions") or []),
                })
        result[str(event_id)] = listings
    return result


def get_our_listings_for_event(event_id: str, cookie: str, account_id: str) -> List[Dict[str, Any]]:
    resp = _put(
        "https://reachpro.com/api/Listing/GetListingsSectionalDataForEvents"
        "?dataToInclude=Basic&dataToInclude=Pricing&processListingGroup=true",
        headers=_headers(cookie, account_id),
        json={
            "listingQuery": {
                "eventOrMappingIds": [event_id],
                "timezoneOffsetMins": -300,
                "isListed": True,
                "oldPosEventIds": [],
            },
            "curListings": None,
        },
    )
    resp.raise_for_status()
    data = resp.json() or {}

    listings: List[Dict[str, Any]] = []
    event_block = data.get(str(event_id)) or {}
    for item in event_block.get("items") or []:
        for mkp in item.get("mkpListings") or []:
            listing_id = str(
                mkp.get("mkpListingId") or mkp.get("listingId") or mkp.get("id") or ""
            )
            if not listing_id:
                continue
            web_price = mkp.get("webPrice")
            if isinstance(web_price, dict):
                web_price = web_price.get("amt") or web_price.get("disp")
            try:
                web_price = float(web_price) if web_price is not None else None
            except (TypeError, ValueError):
                web_price = None
            listings.append({
                "listing_id": listing_id,
                "section": str(mkp.get("overrideSection") or mkp.get("section") or ""),
                "price": web_price,
                "marketplace": str(mkp.get("mkp") or ""),
                "id_on_mkp": str(mkp.get("idOnMkp") or ""),
                "mkp_status": str(mkp.get("status") or ""),
                # Inventory in hand, straight from ReachPro: a purchase order
                # becomes a ticket group, so unsoldQty is what we hold and
                # have not sold yet. Already net of sales, unlike a sheet.
                "quantity": _to_int(item.get("availQty")),
                "unsold_qty": _to_int(item.get("unsoldQty")),
                "sold_qty": _to_int(item.get("soldQty")),
                "ticket_count": _to_int(item.get("ticketCnt")),
                # For whatever eventually writes back: the inventory listing's
                # own id, its broadcast state, and the operations ReachPro
                # says it supports ("Unbroadcast" is the deactivation).
                "inventory_listing_id": str(item.get("id") or ""),
                "broadcast_status": str(item.get("brdcstStatus") or ""),
                "actions": list(item.get("actions") or []),
            })
    return listings


def fetch_all_active_listings(
    regions: Optional[List[str]] = None,
    events_per_region: Optional[int] = None,
    event_limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Pull our active listings from ReachPro across the given regions.

    Parameters
    ----------
    events_per_region : int, optional
        Cap events fetched per region (applied before metadata fetch).
    event_limit : int, optional
        Hard cap on total listings returned across all regions combined.

    Returns a list of dicts:
      {
        reachpro_listing_id, reachpro_event_id, event_name, event_venue,
        event_date, section, our_price, region
      }
    """
    cookie = _get_cookie()
    account_id = _DEFAULT_ACCOUNT_ID
    regions = regions or list(REGION_STATE_IDS.keys())

    all_listings: List[Dict[str, Any]] = []

    for region in regions:
        # Early-exit if global limit already hit
        if event_limit and len(all_listings) >= event_limit:
            logger.info("Global event_limit=%d reached, skipping region %s", event_limit, region)
            break

        logger.info("[%s] Fetching event IDs…", region)
        try:
            event_ids = get_event_ids(region, cookie, account_id)
        except Exception as exc:
            logger.error("[%s] Failed to get event IDs: %s", region, exc)
            continue

        # Apply per-region cap BEFORE metadata fetch (avoids fetching 1939 event names)
        if events_per_region and len(event_ids) > events_per_region:
            logger.info("[%s] Capping to %d of %d events (events_per_region)", region, events_per_region, len(event_ids))
            event_ids = event_ids[:events_per_region]

        # Also cap based on how many more listings we still need
        if event_limit:
            remaining = event_limit - len(all_listings)
            if len(event_ids) > remaining:
                logger.info("[%s] Capping to %d events (event_limit headroom)", region, remaining)
                event_ids = event_ids[:remaining]

        logger.info("[%s] Fetching metadata for %d events…", region, len(event_ids))
        try:
            event_info = get_event_info(event_ids, cookie, account_id)
        except Exception as exc:
            logger.error("[%s] Failed to get event info: %s", region, exc)
            event_info = {}

        logger.info("[%s] Fetching our listings event-by-event…", region)
        for idx, event_id in enumerate(event_ids, 1):
            info = event_info.get(event_id, {})
            event_name = info.get("event_name", event_id)
            try:
                listings = get_our_listings_for_event(event_id, cookie, account_id)
            except Exception as exc:
                logger.warning("[%s] Event %d/%d (%s): listings fetch failed — %s",
                               region, idx, len(event_ids), event_name, exc)
                continue

            if listings:
                logger.info("[%s] Event %d/%d (%s): %d listing(s) found",
                            region, idx, len(event_ids), event_name, len(listings))
            else:
                logger.debug("[%s] Event %d/%d (%s): no listings", region, idx, len(event_ids), event_name)

            for listing in listings:
                all_listings.append({
                    "reachpro_listing_id": listing["listing_id"],
                    "reachpro_event_id": event_id,
                    "event_name": info.get("event_name", ""),
                    "event_venue": info.get("event_venue", ""),
                    "event_date": info.get("event_date", ""),
                    "section": listing["section"],
                    "our_price": listing["price"],
                    "region": region,
                    "marketplace": listing.get("marketplace", ""),
                    "id_on_mkp": listing.get("id_on_mkp", ""),
                    "mkp_status": listing.get("mkp_status", ""),
                    "quantity": listing.get("quantity"),
                    "unsold_qty": listing.get("unsold_qty"),
                    "sold_qty": listing.get("sold_qty"),
                    "inventory_listing_id": listing.get("inventory_listing_id"),
                    "broadcast_status": listing.get("broadcast_status"),
                    "actions": listing.get("actions") or [],
                })

            # Stop early if global limit reached mid-region
            if event_limit and len(all_listings) >= event_limit:
                logger.info("[%s] Global event_limit=%d reached at event %d/%d",
                            region, event_limit, idx, len(event_ids))
                break

    logger.info("Total active listings fetched: %d", len(all_listings))
    return all_listings


# ---------------------------------------------------------------------------
# Writing back: taking a listing off the marketplace
# ---------------------------------------------------------------------------

def unlist_marketplace_listings(inventory_listing_id: str, cookie: str = "",
                                account_id: str = "", dry_run: bool = True) -> Dict[str, Any]:
    """
    Take one inventory listing off the marketplaces it is listed on.

    This is the request ReachPro's own UI sends when a listing is removed
    (captured from the Inventory page, 2026-09-25):

        POST /api/Listing/DeleteMarketplaceListings?listingId=<id>
        body: []

    The id is the INVENTORY listing id — item.id in
    GetListingsSectionalDataForEvents, carried through the scan as
    inventory_listing_id — not the marketplace listing id. The empty body
    means "all of this listing's marketplace listings".

    The passes themselves stay in ReachPro inventory; only the offer to sell
    goes away. That is the deactivation the 2026-09-22 call asked for: stop
    selling what we can no longer fulfil, keep what we already bought.

    dry_run=True (the default) reports what would be sent and sends nothing.
    Callers must pass dry_run=False deliberately, and listing_actions only
    does so when REACHPRO_ALLOW_WRITES is set.
    """
    listing_id = str(inventory_listing_id or "").strip()
    if not listing_id:
        return {"ok": False, "dry_run": dry_run, "reason": "no inventory listing id"}

    url = f"https://reachpro.com/api/Listing/DeleteMarketplaceListings?listingId={listing_id}"
    if dry_run:
        return {"ok": True, "dry_run": True, "would_post": url, "listing_id": listing_id}

    cookie = (cookie or "").strip() or _get_cookie()
    account_id = account_id or _DEFAULT_ACCOUNT_ID
    try:
        resp = requests.post(url, headers=_headers(cookie, account_id), json=[], timeout=30)
    except requests.RequestException as exc:
        return {"ok": False, "dry_run": False, "listing_id": listing_id, "reason": f"request failed: {exc}"}
    if resp.status_code in (401, 403):
        return {"ok": False, "dry_run": False, "listing_id": listing_id,
                "reason": "ReachPro rejected the cookie — paste a fresh one"}
    if not resp.ok:
        return {"ok": False, "dry_run": False, "listing_id": listing_id,
                "reason": f"HTTP {resp.status_code}: {resp.text[:160]}"}
    logger.info("ReachPro: unlisted inventory listing %s from its marketplaces", listing_id)
    return {"ok": True, "dry_run": False, "listing_id": listing_id, "status_code": resp.status_code}


def fetch_campaigns(regions: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Fetch all ad campaigns from ReachPro CampaignManager for the active account.

    Returns a list of dicts with campaign + event metadata:
      {
        event_id, event_name, event_venue, event_date,
        campaign_id, campaign_name, campaign_state,
        bidding_mode, flight_duration,
        total_budget, total_spend,
        has_ended,
        active_flight: { base_bid, max_bid, flight_budget, flight_spend, start, end } | None
      }
    """
    cookie = _get_cookie()
    account_id = _DEFAULT_ACCOUNT_ID
    regions = regions or list(REGION_STATE_IDS.keys())

    # 1. Collect all event IDs across requested regions
    all_event_ids: List[str] = []
    for region in regions:
        try:
            ids = get_event_ids(region, cookie, account_id)
            all_event_ids.extend(ids)
        except Exception as exc:
            logger.warning("fetch_campaigns: failed to get event IDs for %s: %s", region, exc)

    all_event_ids = list(dict.fromkeys(all_event_ids))
    if not all_event_ids:
        return []

    # 2. Call GetCampaigns in chunks (Int32 IDs)
    headers = _headers(cookie, account_id)
    raw_campaigns: List[Dict] = []
    chunk_size = 500
    for i in range(0, len(all_event_ids), chunk_size):
        chunk = [int(x) for x in all_event_ids[i: i + chunk_size]]
        try:
            resp = requests.put(_CAMPAIGNS_URL, headers=headers, json=chunk, timeout=30)
            resp.raise_for_status()
            raw_campaigns.extend(resp.json())
        except Exception as exc:
            logger.warning("fetch_campaigns: GetCampaigns chunk failed: %s", exc)

    if not raw_campaigns:
        return []

    # 3. Enrich with event metadata
    campaign_event_ids = list(dict.fromkeys(str(c["eventId"]) for c in raw_campaigns))
    try:
        event_info = get_event_info(campaign_event_ids, cookie, account_id)
    except Exception as exc:
        logger.warning("fetch_campaigns: event info fetch failed: %s", exc)
        event_info = {}

    results: List[Dict[str, Any]] = []
    for c in raw_campaigns:
        eid = str(c.get("eventId", ""))
        info = event_info.get(eid, {})
        af = c.get("activeFlight") or {}
        results.append({
            "event_id": eid,
            "event_name": info.get("event_name", c.get("campaignName", "")),
            "event_venue": info.get("event_venue", ""),
            "event_date": info.get("event_date", ""),
            "campaign_id": c.get("campaignId"),
            "campaign_name": c.get("campaignName", ""),
            "campaign_state": c.get("campaignState", ""),
            "bidding_mode": c.get("biddingMode", ""),
            "flight_duration": c.get("flightDuration", ""),
            "total_budget": c.get("totalBudget"),
            "total_spend": c.get("totalSpend"),
            "has_ended": c.get("hasEnded", False),
            "active_flight": {
                "base_bid": af.get("baseBid"),
                "max_bid": af.get("maxBid"),
                "flight_budget": af.get("flightBudget"),
                "flight_spend": af.get("flightSpend"),
                "start": (af.get("dateRange") or {}).get("start"),
                "end": (af.get("dateRange") or {}).get("end"),
            } if af else None,
        })

    return results
