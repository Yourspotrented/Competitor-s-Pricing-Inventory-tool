"""
What one of our listings should be changed to, and (later) doing it.

Used by both selling platforms: Lysted (lysted_scan_runner) and ReachPro
(reachpro_scan_runner). The rule is the same on each, so the code is too.

Why (Max Lawrence, 2026-09-22 call): a sell-out on SpotHero or ParkWhiz is not
by itself a reason to pull a listing down, because the passes are often gone
"likely because we bought them". The rule he set is a quantity one:

    "Let's say we have 10 passes but 15 listings, and the passes on SpotHero
    and ParkWhiz have been sold out. Then the bot would reduce the amount of
    listed passes to 10 … We should list 10. That's what we have and that's
    what's available in the market."

So the target for a listing is

    passes we hold (spare spots, not yet used)  +  passes still buyable at source

and the action is whatever moves the listing from its current quantity to that
target: keep, reduce, or deactivate when the target is zero.

Nothing here touches Lysted. It decides and records; applying the decision
needs Lysted's API, which the team has not been given write access to
(Chibuikem: don't do anything that could strain the relationship). When that
arrives, apply_action() is the one place to implement, and LYSTED_ALLOW_WRITES
is the one switch to turn on.

What each source can tell us
----------------------------
SpotHero returns an exact count, so "still buyable" is that count.
ParkWhiz only says available / limited / sold_out — no number — so it can
confirm a sell-out but never adds to the target. That asymmetry is deliberate:
under-listing loses a sale, over-listing loses a customer and a platform
rating.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Never take down more than this in one scan, per platform. A detection bug,
# a bad token, or a platform answering oddly should cost a handful of listings
# and a question, not the whole portfolio. Raise it deliberately once a few
# scans have run clean.
MAX_DEACTIVATIONS_PER_SCAN = 10

KEEP = "keep"
REDUCE = "reduce"
DEACTIVATE = "deactivate"


def writes_enabled(source: str = "lysted") -> bool:
    """
    Is the tool allowed to change anything on this platform? Off unless asked.

    One switch per platform — LYSTED_ALLOW_WRITES, REACHPRO_ALLOW_WRITES — so
    enabling one never quietly enables the other.
    """
    var = f"{source.upper()}_ALLOW_WRITES"
    return os.getenv(var, "").strip().lower() in ("1", "true", "yes")


def _buyable(platform_detail: Dict[str, Any]) -> Optional[int]:
    """
    How many passes could still be bought at source right now.

    None means "no idea" — the lot wasn't found, wasn't checked, or only
    ParkWhiz saw it and ParkWhiz publishes no counts. A listing we know
    nothing about is never touched.
    """
    spothero = (platform_detail or {}).get("spothero") or {}
    parkwhiz = (platform_detail or {}).get("parkwhiz") or {}
    sh_level, sh_spots = spothero.get("level"), spothero.get("spots_left")

    if sh_level == "sold_out":
        return 0
    if sh_spots is not None and sh_level in ("ok", "limited"):
        return int(sh_spots)
    # SpotHero doesn't have it. ParkWhiz can only confirm a sell-out.
    if parkwhiz.get("level") == "sold_out" and sh_level in ("not_found", None, "unknown"):
        return 0
    return None


def decide(listing_quantity: Optional[int], passes_left: Optional[int],
           platform_detail: Dict[str, Any]) -> Dict[str, Any]:
    """
    What should happen to one listing.

    Returns {action, target_quantity, passes_in_hand, buyable_at_source,
    reason}. action is KEEP, REDUCE or DEACTIVATE; target_quantity is None
    when we don't know enough to act.
    """
    in_hand = passes_left or 0
    buyable = _buyable(platform_detail)

    if buyable is None:
        return {"action": KEEP, "target_quantity": None, "passes_in_hand": in_hand,
                "buyable_at_source": None,
                "reason": "not listed on SpotHero, so how many are still buyable is unknown"}

    target = in_hand + buyable
    if listing_quantity is None:
        return {"action": KEEP, "target_quantity": target, "passes_in_hand": in_hand,
                "buyable_at_source": buyable, "reason": "listing quantity unknown"}

    # Sold out at source. The listing can only be honoured out of what we
    # already hold, and Lysted will not let a quantity be changed once a
    # listing exists (Leticia, 2026-09-25) — so Max's "reduce 15 to 10" is not
    # available to us. That leaves two options, and the team chose the
    # cautious one (Rishabh, 2026-10-07): keep the listing only when our own
    # passes cover it in full, otherwise take it down.
    #
    # Holding 10 against 100 listed and leaving it up risks ninety sales we
    # cannot deliver — relocations, refunds and a platform rating, which is
    # the cost Max described. Deactivating loses the ten we could have sold.
    # The ten are worth less than the ninety.
    if buyable == 0:
        if in_hand >= listing_quantity:
            return {"action": KEEP, "target_quantity": target, "passes_in_hand": in_hand,
                    "buyable_at_source": buyable,
                    "reason": f"sold out at source, but our {in_hand} pass(es) cover the listing"}
        return {"action": DEACTIVATE, "target_quantity": 0, "passes_in_hand": in_hand,
                "buyable_at_source": buyable,
                "reason": (f"sold out at source and we hold only {in_hand} of {listing_quantity}"
                           if in_hand else
                           "sold out at source and no passes in hand — cannot be fulfilled")}

    # Still buyable at source: whatever we cannot cover ourselves can be
    # bought when it sells, so the listing stands.
    return {"action": KEEP, "target_quantity": target, "passes_in_hand": in_hand,
            "buyable_at_source": buyable, "reason": "enough available to cover the listing"}


def carry_out(alerts: List[Dict[str, Any]], source: str,
              id_field: str) -> Dict[str, Any]:
    """
    Do the deactivations in `alerts`, if this platform's writes are enabled.

    Returns a summary: what was done, what was skipped and why. Never raises —
    a platform refusing a write must not fail the scan that found it.

    Two guards beyond the write switch itself:
      * only DEACTIVATE is carried out, never a quantity change;
      * at most MAX_DEACTIVATIONS_PER_SCAN, so a bad reading cannot empty the
        portfolio before anyone notices.
    """
    done, skipped, failed = [], [], []
    if not writes_enabled(source):
        return {"enabled": False, "done": [], "skipped": [], "failed": [],
                "reason": f"{source.upper()}_ALLOW_WRITES is off"}

    for alert in alerts:
        decision = alert.get("decision") or {}
        if decision.get("action") != DEACTIVATE:
            continue
        listing_id = str(alert.get(id_field) or "").strip()
        if not listing_id:
            # Lysted's export carries no listing id and their API will not
            # list listings, so this is the normal case there until that
            # changes. Reported rather than guessed at.
            skipped.append({"listing": alert.get("listing_key"), "reason": f"no {id_field}"})
            continue
        if len(done) >= MAX_DEACTIVATIONS_PER_SCAN:
            skipped.append({"listing": alert.get("listing_key"),
                            "reason": f"per-scan cap of {MAX_DEACTIVATIONS_PER_SCAN} reached"})
            continue

        result = apply_action({**alert, id_field: listing_id}, decision, source)
        if result.get("applied"):
            done.append({"listing": alert.get("listing_key"), "id": listing_id,
                         "event": alert.get("event_name"), "event_date": alert.get("event_date"),
                         "venue": alert.get("venue"), "section": alert.get("section"),
                         # What the team needs to relist with: Max asked for
                         # the tool to delist and the team to put it back up
                         # at the number we can actually cover (2026-10-07).
                         "listed": alert.get("quantity"),
                         "passes_in_hand": decision.get("passes_in_hand"),
                         "why": decision.get("reason")})
            logger.info("%s: deactivated %s (%s — %s)", source, listing_id,
                        alert.get("event_name"), alert.get("section"))
        else:
            failed.append({"listing": alert.get("listing_key"), "id": listing_id,
                           "reason": result.get("reason")})
            logger.error("%s: could not deactivate %s — %s", source, listing_id, result.get("reason"))

    return {"enabled": True, "done": done, "skipped": skipped, "failed": failed}


def describe(decision: Dict[str, Any], listing_quantity: Optional[int]) -> str:
    """The alert's action line."""
    action = decision["action"]
    if action == DEACTIVATE:
        return "deactivate — nothing left to fulfil it"
    if action == REDUCE:
        return f"reduce {listing_quantity} → {decision['target_quantity']} passes ({decision['reason']})"
    return "no change needed"


def apply_action(listing: Dict[str, Any], decision: Dict[str, Any],
                 source: str = "lysted") -> Dict[str, Any]:
    """
    Carry the decision out on the selling platform.

    Not implemented for either platform yet, for the same reason on both: no
    endpoint for unlisting or changing a listing's quantity has been verified,
    and both APIs are the web apps' own. Lysted additionally has no approved
    write access. Guessing at an endpoint on a live sales platform is how you
    unlist inventory that was selling fine, so until one is captured from the
    real UI this records what it would have done — the queue the team works
    from is then the same one the writer will consume.
    """
    planned = {"applied": False, "source": source, "action": decision["action"],
               "target_quantity": decision.get("target_quantity"),
               "listing_key": listing.get("reachpro_listing_id")}
    dry_run = not writes_enabled(source)
    if dry_run:
        planned["reason"] = f"{source.upper()}_ALLOW_WRITES is off — recorded, not sent"

    if decision["action"] == KEEP:
        planned.update({"applied": False, "reason": "nothing to change"})
        return planned

    if source == "reachpro" and decision["action"] == DEACTIVATE:
        from reachpro import unlist_marketplace_listings
        result = unlist_marketplace_listings(listing.get("inventory_listing_id") or "",
                                             dry_run=dry_run)
        planned.update({"applied": bool(result.get("ok")) and not dry_run,
                        "dry_run": dry_run, "result": result})
        if not result.get("ok"):
            planned["reason"] = result.get("reason")
        return planned

    if source == "lysted" and decision["action"] == DEACTIVATE:
        from lysted_api import set_broadcast
        result = set_broadcast(listing.get("lysted_listing_id") or "", broadcast=False, dry_run=dry_run)
        planned.update({"applied": bool(result.get("ok")) and not dry_run,
                        "dry_run": dry_run, "result": result})
        if not result.get("ok"):
            planned["reason"] = result.get("reason")
        return planned

    # REDUCE on either platform: no endpoint captured. On Lysted a listing's
    # quantity cannot be changed at all once created (Leticia, 2026-09-25), so
    # there the choice is deactivate or leave it; on ReachPro it is a
    # different call from removing the listing and nobody has captured it.
    planned["reason"] = f"no verified {source} endpoint for '{decision['action']}' yet"
    logger.warning("%s write requested but unavailable: %s", source, planned)
    return planned
