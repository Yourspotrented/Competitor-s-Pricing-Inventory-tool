"""
Microsoft Teams Incoming Webhook notifications for detected facility
competitor price spikes and low-inventory crossings.

Off by default: if TEAMS_WEBHOOK_URL isn't set in .env, notify_price_spikes()
is a no-op (logs and returns) rather than raising, so the scan/scheduler
never fails just because notifications aren't configured yet.

Setup (for whoever wires this up in Teams):
  1. In the target Teams channel: ... -> Connectors -> Incoming Webhook -> Create
  2. Copy the webhook URL it gives you
  3. Add to backend/.env:  TEAMS_WEBHOOK_URL=https://...

Usage:
    from teams_notify import notify_price_spikes
    notify_price_spikes(spikes, low_inventory_alerts)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass


def _webhook_url(webhook_env_var: str = "TEAMS_WEBHOOK_URL") -> str:
    return os.getenv(webhook_env_var, "").strip()


def _spike_to_teams_fact_set(spike: Dict[str, Any]) -> dict:
    """One MessageCard section for a single price spike."""
    facility = spike.get("sample_facility_name") or "(unknown facility)"
    cluster = spike.get("cluster_label") or ""
    title = f"💲 {spike['lot_name'] or spike['lot_address'] or 'Unknown lot'} — +{spike['percent_increase']}%"
    subtitle = f"Near {facility}" + (f" ({cluster})" if cluster else "")
    return {
        "activityTitle": title,
        "activitySubtitle": subtitle,
        "facts": [
            {"name": "Platform", "value": spike["platform"]},
            {"name": "Previous price", "value": f"${spike['previous_price']:.2f}"},
            {"name": "Current price", "value": f"${spike['current_price']:.2f}"},
            {"name": "Address", "value": spike.get("lot_address") or "—"},
        ],
    }


def _low_inventory_to_teams_fact_set(alert: Dict[str, Any]) -> dict:
    """One MessageCard section for a single low-inventory crossing."""
    facility = alert.get("sample_facility_name") or "(unknown facility)"
    cluster = alert.get("cluster_label") or ""
    title = f"📉 {alert['lot_name'] or alert['lot_address'] or 'Unknown lot'} — {alert['percent_remaining']:.1f}% left"
    subtitle = f"Near {facility}" + (f" ({cluster})" if cluster else "")
    spots = alert.get("spots_left")
    capacity = alert.get("capacity")
    spots_value = f"{spots} / {capacity}" if spots is not None and capacity is not None else "—"
    return {
        "activityTitle": title,
        "activitySubtitle": subtitle,
        "facts": [
            {"name": "Platform", "value": alert["platform"]},
            {"name": "Spots left / total", "value": spots_value},
            {"name": "Address", "value": alert.get("lot_address") or "—"},
        ],
    }


def notify_price_spikes(spikes: List[Dict[str, Any]], low_inventory_alerts: List[Dict[str, Any]] = None,
                         attachment_url: str = "", webhook_env_var: str = "TEAMS_WEBHOOK_URL") -> bool:
    """
    Post one bundled Teams message covering newly-detected facility
    competitor price spikes and low-inventory crossings from the same scan.

    spikes: dicts shaped like FacilityPriceSpike's columns (percent_increase,
    previous_price, current_price, platform, lot_name, lot_address,
    sample_facility_name, cluster_label).

    low_inventory_alerts: dicts shaped like FacilityLowInventoryAlert's
    columns (percent_remaining, spots_left, capacity, platform, lot_name,
    lot_address, sample_facility_name, cluster_label) — a competitor lot's
    remaining inventory just crossed below the low-inventory threshold
    (SpotHero only; ParkWhiz has no exact percent_remaining).

    attachment_url, if given, is a SharePoint link to the filled
    Clusters.xlsx for this run (see clusters_export.py /
    sharepoint_upload.py) — included as a plain link in the card text,
    not a real Teams file attachment. A true attachment would need the
    Power Automate flow's own "Attachments" input wired up in a specific
    shape we haven't verified; a link is simpler and known to render.

    webhook_env_var: which .env variable holds the destination webhook URL
    — defaults to TEAMS_WEBHOOK_URL (pricing team's channel). Pass
    "EVENTS_TEAMS_WEBHOOK_URL" to send to the Events team's channel
    instead, once that webhook is set up (see events_scan_runner.py).

    Returns True if a notification was sent, False if skipped (no webhook
    configured, or nothing to report) or failed (logged, not raised — a
    notification failure shouldn't break the scan that triggered it).
    """
    low_inventory_alerts = low_inventory_alerts or []
    if not spikes and not low_inventory_alerts:
        return False

    url = _webhook_url(webhook_env_var)
    if not url:
        logger.info("%s not set — skipping %d spike(s) / %d low-inventory alert(s)",
                    webhook_env_var, len(spikes), len(low_inventory_alerts))
        return False

    title_parts = []
    if spikes:
        title_parts.append(f"{len(spikes)} price spike(s)")
    if low_inventory_alerts:
        title_parts.append(f"{len(low_inventory_alerts)} low-inventory alert(s)")
    title = f"⚠ {' · '.join(title_parts)} detected"

    text_lines = []
    if spikes:
        text_lines.append("Competitor pricing jumped 20%+ near one or more of our facilities.")
    if low_inventory_alerts:
        text_lines.append("Competitor inventory dropped below 20% remaining near one or more of our facilities.")
    text = " ".join(text_lines)
    if attachment_url:
        text += f"\n\n[📄 Open the updated Clusters.xlsx]({attachment_url})"

    sections = [_spike_to_teams_fact_set(s) for s in spikes[:10]]
    if len(spikes) > 10:
        sections.append({"text": f"...and {len(spikes) - 10} more price spike(s)."})
    sections += [_low_inventory_to_teams_fact_set(a) for a in low_inventory_alerts[:10]]
    if len(low_inventory_alerts) > 10:
        sections.append({"text": f"...and {len(low_inventory_alerts) - 10} more low-inventory alert(s)."})

    card = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "summary": title,
        "themeColor": "D9534F",
        "title": title,
        "text": text,
        "sections": sections,
    }

    try:
        resp = requests.post(url, json=card, timeout=15)
        resp.raise_for_status()
        logger.info("Sent Teams notification: %d spike(s), %d low-inventory alert(s)",
                    len(spikes), len(low_inventory_alerts))
        return True
    except Exception as exc:
        logger.error("Failed to send Teams notification: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Lysted: listings whose parking has sold out at source → deactivate
# ---------------------------------------------------------------------------

def _sold_out_to_teams_section(alert: Dict[str, Any]) -> dict:
    """One MessageCard section for a listing that has just sold out at source."""
    when = alert.get("event_date") or "—"
    where = alert.get("venue") or "—"
    place = ", ".join(x for x in (alert.get("city"), alert.get("state")) if x)
    qty = alert.get("quantity")
    price = alert.get("list_price")
    return {
        "activityTitle": f"🚫 {alert.get('event_name') or '(unknown event)'} — {alert.get('section') or '(unknown lot)'}",
        "activitySubtitle": f"{when} · {where}" + (f" ({place})" if place else ""),
        "facts": [
            {"name": "Sold out on", "value": alert.get("platforms") or "—"},
            {"name": "Passes listed", "value": str(qty) if qty is not None else "—"},
            {"name": "Our list price", "value": f"${price:.2f}" if price is not None else "—"},
            {"name": "Was showing", "value": alert.get("previous_levels") or "first check"},
        ],
    }


def notify_sold_out_listings(alerts: List[Dict[str, Any]],
                             webhook_env_var: str = "LYSTED_TEAMS_WEBHOOK_URL") -> bool:
    """
    One bundled Teams message telling the listing team which Lysted listings
    to deactivate: the parking behind them is sold out on the buying platform,
    so they can no longer be fulfilled.

    alerts: dicts shaped like LystedSoldOutAlert's columns (event_name,
    event_date, venue, city, state, section, platforms, quantity, list_price,
    previous_levels).

    Same contract as notify_price_spikes: False if skipped (no webhook, nothing
    to send) or failed (logged, never raised).
    """
    if not alerts:
        return False
    url = _webhook_url(webhook_env_var)
    if not url:
        logger.info("%s not set — skipping %d sold-out listing alert(s)", webhook_env_var, len(alerts))
        return False

    title = f"🚫 {len(alerts)} Lysted listing(s) sold out at source — deactivate"
    text = ("The parking behind these listings is sold out on the buying platform, so they "
            "can't be fulfilled anymore. Deactivate them on Lysted.")
    sections = [_sold_out_to_teams_section(a) for a in alerts[:15]]
    if len(alerts) > 15:
        sections.append({"text": f"...and {len(alerts) - 15} more."})

    card = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "summary": title,
        "themeColor": "B33A32",
        "title": title,
        "text": text,
        "sections": sections,
    }
    try:
        resp = requests.post(url, json=card, timeout=15)
        resp.raise_for_status()
        logger.info("Sent Teams sold-out notification: %d listing(s)", len(alerts))
        return True
    except Exception as exc:
        logger.error("Failed to send Teams sold-out notification: %s", exc)
        return False
