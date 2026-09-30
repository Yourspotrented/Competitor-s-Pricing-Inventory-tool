"""
Microsoft Teams notifications for competitor price spikes, low-inventory
crossings, and Lysted listings that have sold out at source.

Off by default: if a flow's webhook URL isn't set in .env, the notify_*
functions are a no-op (log and return False) rather than raising, so a scan
or scheduler never fails just because notifications aren't configured yet.

Delivery is through Power Automate "Workflows" (Teams' replacement for the
retired Office 365 Incoming Webhook connectors). Each alert stream has its own
flow, and each flow posts into a Teams group chat:
  1. In Power Automate, create a flow from the "Send webhook alerts to a
     chat" template (trigger: "When a Teams webhook request is received").
  2. In its "Post card in a chat or channel" action set Post as = Flow bot,
     Post in = Group chat, and pick the chat. The flow's connected account
     must be a member of that chat.
  3. Copy the trigger's URL into backend/.env, e.g. TEAMS_WEBHOOK_URL=https://...

Payloads are Adaptive Cards wrapped in the {"type": "message", "attachments":
[...]} envelope those flows read — the same shape the team's other Workflows
alerts already post. The legacy MessageCard format this module used to send
is not an Adaptive Card; a Workflows flow either drops it or posts an empty
card, and the webhook still answers 202, so the failure was silent.

Usage:
    from teams_notify import notify_price_spikes, notify_lysted_summary
    notify_price_spikes(spikes, low_inventory_alerts)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass



def _webhook_url(webhook_env_var: str = "TEAMS_WEBHOOK_URL") -> str:
    return os.getenv(webhook_env_var, "").strip()


# ---------------------------------------------------------------------------
# Adaptive Card building blocks
# ---------------------------------------------------------------------------
# Cards follow the team's "Daily Sold Summary" (Arbitrage Event Sold Alerts):
# a shaded title with the date, a few totals, then a short list. Details live
# in the dashboard; the chat only needs enough to act on.

MAX_LINES = 5            # per list; the rest become "…and N more"
MAX_SOLD_OUT_LINES = 15  # the deactivate list is the one people act on


def _header(title: str) -> dict:
    return {
        "type": "Container",
        "style": "emphasis",
        "bleed": True,
        "items": [
            {"type": "TextBlock", "text": title, "size": "Large", "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
             "isSubtle": True, "wrap": True, "spacing": "Small"},
        ],
    }


def _totals(facts: List[tuple]) -> dict:
    return {"type": "FactSet", "separator": True, "spacing": "Medium",
            "facts": [{"title": k, "value": str(v)} for k, v in facts]}


def _section(title: str) -> dict:
    return {"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True,
            "separator": True, "spacing": "Medium"}


def _line(text: str) -> dict:
    return {"type": "TextBlock", "text": text, "wrap": True, "spacing": "Small"}


def _note(text: str) -> dict:
    return {"type": "TextBlock", "text": text, "wrap": True, "separator": True, "spacing": "Medium"}


def _lines(items: List[Dict[str, Any]], render, limit: int = MAX_LINES) -> List[dict]:
    out = [_line(render(i)) for i in items[:limit]]
    if len(items) > limit:
        out.append({"type": "TextBlock", "text": f"…and {len(items) - limit} more",
                    "isSubtle": True, "wrap": True, "spacing": "Small"})
    return out


def _envelope(body: List[dict], actions: Optional[List[dict]] = None) -> dict:
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
    }
    if actions:
        card["actions"] = actions
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": card,
        }],
    }


def _post(url: str, payload: dict, what: str) -> bool:
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        logger.info("Sent Teams notification: %s", what)
        return True
    except Exception as exc:
        logger.error("Failed to send Teams notification (%s): %s", what, exc)
        return False


# ---------------------------------------------------------------------------
# One-line renderings
# ---------------------------------------------------------------------------

def _money(value: Any) -> str:
    return f"${value:.2f}" if isinstance(value, (int, float)) else "—"


def _event_when(raw: Optional[str]) -> str:
    """
    '2027-07-03T16:31:00' -> 'Jul 03, 2027, 4:31 PM'; raw text if it isn't ISO.

    Date and time both matter: the same act plays the same venue on several
    dates, and the team matches an alert to a listing by them.
    """
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    when = dt.strftime("%b %d")
    if dt.year != datetime.now().year:
        when += dt.strftime(", %Y")
    if (dt.hour, dt.minute) != (0, 0):
        when += dt.strftime(", %I:%M %p").replace(" 0", " ")
    return when


def _platform(name: Optional[str]) -> str:
    names = {"spothero": "SpotHero", "parkwhiz": "ParkWhiz"}
    return ", ".join(names.get(p.strip().lower(), p.strip()) for p in (name or "").split(",") if p.strip()) or "—"


def _lot_label(item: Dict[str, Any]) -> str:
    return item.get("lot_name") or item.get("lot_address") or "Unknown lot"


def _where(item: Dict[str, Any]) -> str:
    if item.get("context_subtitle"):
        return item["context_subtitle"]
    facility = item.get("sample_facility_name")
    return f"near {facility}" if facility else ""


def _spike_text(s: Dict[str, Any]) -> str:
    parts = [f"{_money(s.get('previous_price'))} → {_money(s.get('current_price'))}",
             _platform(s.get("platform")), _where(s)]
    ours = " (our lot)" if s.get("our_lot") else ""
    return f"• **{_lot_label(s)}**{ours} +{s['percent_increase']:.0f}% · " + " · ".join(p for p in parts if p)


def _low_text(a: Dict[str, Any]) -> str:
    left = f"{a['percent_remaining']:.0f}% left"
    if a.get("spots_left") is not None and a.get("capacity") is not None:
        left += f" ({a['spots_left']}/{a['capacity']})"
    parts = [_platform(a.get("platform")), _where(a)]
    return f"• **{_lot_label(a)}** {left} · " + " · ".join(p for p in parts if p)


def _platform_state(detail: Dict[str, Any], platform: str) -> str:
    """"sold out (0 of 25 left)", "available (12 left)", "not listed"."""
    d = (detail or {}).get(platform) or {}
    level, spots, capacity = d.get("level"), d.get("spots_left"), d.get("capacity")
    if level in (None, "unknown"):
        return "not checked"
    if level == "not_found":
        return "not listed"
    words = {"sold_out": "sold out", "limited": "running low", "ok": "available"}
    text = words.get(level, level)
    if spots is not None:
        text += f" ({spots}{f' of {capacity}' if capacity else ''} left)"
    return text


def _sold_out_text(a: Dict[str, Any]) -> str:
    """
    One listing, in the shape the listing team reads it (Leticia, 2026-09-18):
    act, date and venue, how many passes the listing holds, and what each
    platform says — not just the word "sold out".
    """
    detail = a.get("platform_detail") or {}
    qty = a.get("quantity")
    head = " · ".join(x for x in (a.get("event_name") or "(unknown event)",
                                  _event_when(a.get("event_date")), a.get("venue")) if x)
    lines = [f"• **{head}**",
             f"Lot: {a.get('section') or '(unknown lot)'}",
             f"Lysted listing: {qty} pass{'' if qty == 1 else 'es'}" if qty is not None else "Lysted listing: —",
             f"SpotHero: {_platform_state(detail, 'spothero')}",
             f"ParkWhiz: {_platform_state(detail, 'parkwhiz')}",
             f"Passes secured: {_secured_text(a)}"]
    decision = a.get("decision")
    if decision:
        lines.append(f"**Action: {_action_text(decision, qty)}**")
    return "  \n".join(lines)


def _action_text(decision: Dict[str, Any], quantity: Optional[int]) -> str:
    """"reduce 15 → 10 passes" / "deactivate" / "no change" — the instruction."""
    action = decision.get("action")
    target = decision.get("target_quantity")
    if action == "deactivate":
        return "deactivate — nothing left to fulfil it"
    if action == "reduce":
        return f"reduce {quantity} → {target} passes ({decision.get('reason', '')})"
    return "no change — we can still fulfil this listing"


def _secured_text(a: Dict[str, Any]) -> str:
    total, left = a.get("passes_secured"), a.get("passes_left")
    if total is None:
        return "none recorded"
    if not left:
        return f"all {total} used or cancelled"
    return f"yes, {left} of {total} left"


# ---------------------------------------------------------------------------
# Pricing summary (facility and events flows)
# ---------------------------------------------------------------------------

def notify_price_spikes(spikes: List[Dict[str, Any]], low_inventory_alerts: List[Dict[str, Any]] = None,
                         attachment_url: str = "", webhook_env_var: str = "TEAMS_WEBHOOK_URL",
                         subject: str = "our facilities") -> bool:
    """
    One summary card for newly-detected competitor price spikes and
    low-inventory crossings from a scan.

    spikes: dicts with percent_increase, previous_price, current_price,
    platform, lot_name, lot_address, and context_subtitle or
    sample_facility_name for where the lot is.
    low_inventory_alerts: dicts with percent_remaining, spots_left, capacity,
    platform, lot_name, lot_address and the same context fields.
    attachment_url: SharePoint link to this run's Clusters.xlsx, as a button.
    webhook_env_var: the .env variable holding the destination flow URL.
    subject: what the alerts are about ("our facilities").

    Returns True if sent, False if skipped (no webhook, nothing to report) or
    failed (logged, never raised — a notification failure must not break the
    scan that triggered it).
    """
    low_inventory_alerts = low_inventory_alerts or []
    if not spikes and not low_inventory_alerts:
        return False
    url = _webhook_url(webhook_env_var)
    if not url:
        logger.info("%s not set — skipping %d spike(s) / %d low-inventory alert(s)",
                    webhook_env_var, len(spikes), len(low_inventory_alerts))
        return False

    body = [_header("📊 Pricing Alert Summary"),
            _totals([("Price spikes (20%+)", len(spikes)),
                     ("Low inventory (under 20%)", len(low_inventory_alerts))])]
    if spikes:
        body.append(_section("💲 Biggest price jumps"))
        body += _lines(sorted(spikes, key=lambda s: s["percent_increase"], reverse=True), _spike_text)
    if low_inventory_alerts:
        body.append(_section("📉 Low inventory"))
        body += _lines(sorted(low_inventory_alerts, key=lambda a: a["percent_remaining"]), _low_text)
    body.append(_note(f"Competitor lots near {subject}. Full list in the dashboard."))

    actions = ([{"type": "Action.OpenUrl", "title": "Open Clusters.xlsx", "url": attachment_url}]
               if attachment_url else None)
    return _post(url, _envelope(body, actions),
                 f"{len(spikes)} spike(s), {len(low_inventory_alerts)} low-inventory alert(s)")


# ---------------------------------------------------------------------------
# Lysted summary: sold out at source + pricing, one card per scan
# ---------------------------------------------------------------------------

def _day_before_text(a: Dict[str, Any]) -> str:
    """The routine day-before line: no platform state, just what to take down."""
    qty = a.get("quantity")
    head = " · ".join(x for x in (a.get("event_name") or "(unknown event)",
                                  _event_when(a.get("event_date")), a.get("venue")) if x)
    return "  \n".join([f"• **{head}**",
                        f"Lot: {a.get('section') or '(unknown lot)'}",
                        f"Lysted listing: {qty} pass{'' if qty == 1 else 'es'}" if qty is not None
                        else "Lysted listing: —"])


def notify_deactivations(done: List[Dict[str, Any]], failed: List[Dict[str, Any]] = None,
                         source: str = "Lysted",
                         webhook_env_var: str = "DEACTIVATION_TEAMS_WEBHOOK_URL") -> bool:
    """
    One card per scan listing what was actually taken off sale.

    Its own chat and its own card on purpose: the summary card is a reading of
    the market, this is a record of what the tool changed. A group watching
    for changes should not have to read past price spikes to find them, and a
    failed deactivation is something someone must finish by hand.

    done / failed: the lists from listing_actions.carry_out.
    """
    failed = failed or []
    if not done and not failed:
        return False
    url = _webhook_url(webhook_env_var)
    if not url:
        logger.info("%s not set — %d deactivation(s) not reported", webhook_env_var, len(done))
        return False

    counts = [f"{len(done)} deactivated"] + ([f"{len(failed)} failed"] if failed else [])
    body = [_header(f"🚫 {source}: {' · '.join(counts)}"),
            _totals([("Taken off sale", len(done)), ("Failed", len(failed))])]
    if done:
        body.append(_section("Deactivated — sold out at source, nothing left in hand"))
        body += _lines(done, _deactivated_text, MAX_SOLD_OUT_LINES)
    if failed:
        body.append(_section("⚠️ Could not be deactivated — please do these by hand"))
        body += _lines(failed, _deactivation_failed_text, MAX_SOLD_OUT_LINES)
    body.append(_note("Done automatically by the tool — no action needed unless a listing failed."))
    return _post(url, _envelope(body), f"{len(done)} deactivation(s), {len(failed)} failure(s)")


def _deactivated_text(d: Dict[str, Any]) -> str:
    head = " · ".join(x for x in (d.get("event") or "(unknown event)",
                                  _event_when(d.get("event_date")), d.get("venue")) if x)
    return "  \n".join([f"• **{head}**",
                         f"Lot: {d.get('section') or '(unknown lot)'}",
                         f"Listing id: {d.get('id') or '—'}"])


def _deactivation_failed_text(d: Dict[str, Any]) -> str:
    return (f"• **{d.get('event') or d.get('listing') or 'listing'}** "
            f"(id {d.get('id') or '—'})  \n{d.get('reason') or 'unknown error'}")


def notify_lysted_summary(stats: Dict[str, int], sold_out: List[Dict[str, Any]],
                          spikes: List[Dict[str, Any]], low_inventory: List[Dict[str, Any]],
                          webhook_env_var: str = "LYSTED_TEAMS_WEBHOOK_URL",
                          title: str = "📊 Lysted Scan Summary") -> bool:
    """
    One summary card for a Lysted scan.

    stats: listings, found, not_found — counts for the whole scan.
    sold_out: listings that newly went sold out (LystedSoldOutAlert shape) —
    the listing team should deactivate these.
    spikes / low_inventory: as notify_price_spikes, near our Lysted listings.

    Sent only when something is new, so a quiet scan doesn't post a card.
    Same return contract as notify_price_spikes.
    """
    if not (sold_out or spikes or low_inventory):
        return False
    url = _webhook_url(webhook_env_var)
    if not url:
        logger.info("%s not set — skipping Lysted summary (%d sold out, %d spikes, %d low inventory)",
                    webhook_env_var, len(sold_out), len(spikes), len(low_inventory))
        return False

    # Sold out at source, but passes already bought can still be sold — those
    # listings stay up. Only the ones with nothing in hand are deactivations
    # ("we know that we have 6 passes in our inventory. So we can still sell"
    # — Leticia, 2026-09-18).
    def _act(a):
        return (a.get("decision") or {}).get("action") or ("keep" if a.get("passes_left") else "deactivate")

    # The day-before rule is routine housekeeping, not a sell-out, and mixing
    # the two would bury the ones that need a judgement call — in the counts
    # as much as in the list.
    day_before = [a for a in sold_out if a.get("reason") == "day_before_event"]
    sold_out = [a for a in sold_out if a.get("reason") != "day_before_event"]
    deactivate = [a for a in sold_out if _act(a) == "deactivate"]

    totals = [("Listings checked", stats.get("listings", 0)),
              ("Found on SpotHero / ParkWhiz", stats.get("found", 0)),
              ("Newly sold out", len(sold_out))]
    if day_before:
        totals.append(("Deactivate — event tomorrow", len(day_before)))
    totals += [("Price spikes (20%+)", len(spikes)),
               ("Low inventory (under 20%)", len(low_inventory))]
    body = [_header(title), _totals(totals)]
    reduce_to = [a for a in sold_out if _act(a) == "reduce"]
    keep = [a for a in sold_out if _act(a) == "keep"]
    if deactivate:
        body.append(_section("🚫 Deactivate — nothing left to fulfil these"))
        body += _lines(deactivate, _sold_out_text, MAX_SOLD_OUT_LINES)
    if reduce_to:
        body.append(_section("✂️ Reduce the listed quantity to what we can fulfil"))
        body += _lines(reduce_to, _sold_out_text, MAX_SOLD_OUT_LINES)
    if keep:
        body.append(_section("✅ Sold out at source — our passes still cover the listing"))
        body += _lines(keep, _sold_out_text, MAX_SOLD_OUT_LINES)
    if day_before:
        body.append(_section("🗓️ Event is tomorrow — deactivate as usual"))
        body += _lines(day_before, _day_before_text, MAX_SOLD_OUT_LINES)
    if spikes:
        body.append(_section("💲 Our lots — price changed on the platform"))
        body += _lines(sorted(spikes, key=lambda s: s["percent_increase"], reverse=True), _spike_text)
    if low_inventory:
        body.append(_section("📉 Low inventory"))
        body += _lines(sorted(low_inventory, key=lambda a: a["percent_remaining"]), _low_text)
    body.append(_note("No listings sold out this scan." if not sold_out
                      else "Full list in the dashboard."))

    return _post(url, _envelope(body),
                 f"Lysted summary: {len(sold_out)} sold out, {len(spikes)} spike(s), {len(low_inventory)} low inventory")
