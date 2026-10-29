"""
Is each of our listings still making its minimum margin over what the pass
costs to buy — and if not, what should its price be?

Why (Stagefront kickoff call, 2026-10-28): buying prices on SpotHero/ParkWhiz
move all the time, and a listing priced when the pass cost $35 quietly turns
into a $5 (or negative) margin when that pass goes to $45. The client's rule:

    margin = our listing price - what the pass costs to buy right now
    margin < $10   -> push our price up to buy price + $10, even if a
                      competitor is cheaper ("we don't want to be that cheap")
    margin >= $10  -> leave it alone
    flagged        -> skip (e.g. a pass we hold that can't be cancelled)

It makes no judgment calls on pricing beyond that floor. Undercutting a
cheaper competitor (CDK) while staying above the floor was also asked for, but
waits until there is a CDK data source to read.

Nothing here touches a selling platform or the database. It takes plain
numbers and returns a decision, so the same rule serves Stagefront today and
Lysted/ReachPro later — the caller supplies the readings.

What counts as "the buy price"
------------------------------
The cheapest reading for OUR matched lot that is actually bookable now:
  * a sold-out / not-found / failed reading has no buy price, whatever number
    it carries;
  * a ParkWhiz quote that is not the event rate (quote_kind "standard") is a
    different product at a different price — 555 Parking Garage read $53.98
    as a standard rate against a $30.99 event rate — so it is ignored rather
    than risk pricing a listing off it.

Open with the client (2026-10-29), which is why these are constants here and
not buried in the logic:
  * whether $10 is measured against our list price or our payout after the
    selling platform's fee — today it's the list price;
  * (settled 2026-10-30) the buy prices read already include the buyer
    fees: checked at checkout — SpotHero $37.00 + $2.22 service fee = the
    $39.22 we read; ParkWhiz $27.99 + $1.95 platform fee = the $29.94.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

MIN_MARGIN_DOLLARS = 10.0

# A suggested raise bigger than this is still reported, but marked for a human
# to look at first: a jump that size more often means we matched the wrong lot
# or read the wrong rate than that the market moved. Nothing acts on it yet —
# it is the guard automatic repricing will need.
REVIEW_RAISE_DOLLARS = 25.0

_PLATFORM_NAMES = {"spothero": "SpotHero", "parkwhiz": "ParkWhiz", "way": "Way.com"}

OK = "ok"
RAISE = "raise"
SKIP = "skip"
NO_DATA = "no_data"


def _as_price(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def usable_buy_readings(readings: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The readings that can be bought from right now, at a price we trust."""
    out = []
    for r in readings:
        if r.get("is_available") is not True:
            continue
        if r.get("scarcity_level") in ("sold_out", "not_found", "unknown"):
            continue
        if r.get("quote_kind") == "standard":
            continue
        price = _as_price(r.get("price"))
        if price is None:
            continue
        out.append({**r, "price": price})
    return out


def evaluate(our_price: Any, readings: Iterable[Dict[str, Any]], skip: bool = False,
             min_margin: float = MIN_MARGIN_DOLLARS) -> Dict[str, Any]:
    """
    One listing's margin decision.

    readings: one dict per buying platform for this listing's matched lot —
    {platform, price, is_available, scarcity_level, quote_kind (optional)}.

    Returns {action, reason, our_price, buy_price, buy_platform, margin,
    floor_price, suggested_price, needs_review, review_note}. suggested_price
    is only set for RAISE; review_note says why a RAISE needs a look first.
    """
    readings = list(readings)
    ours = _as_price(our_price)
    usable = usable_buy_readings(readings)
    cheapest = min(usable, key=lambda r: r["price"]) if usable else None

    buy = cheapest["price"] if cheapest else None
    out: Dict[str, Any] = {
        "our_price": ours,
        "buy_price": buy,
        "buy_platform": cheapest.get("platform") if cheapest else None,
        "margin": round(ours - buy, 2) if ours is not None and buy is not None else None,
        "floor_price": round(buy + min_margin, 2) if buy is not None else None,
        "suggested_price": None,
        "needs_review": False,
        "review_note": None,
    }

    if skip:
        return {**out, "action": SKIP, "reason": "flagged to skip"}
    if ours is None:
        return {**out, "action": NO_DATA, "reason": "no listing price"}
    if buy is None:
        found = [r for r in readings if r.get("scarcity_level") not in ("not_found", "unknown")]
        if found and all(r.get("scarcity_level") == "sold_out" for r in found):
            reason = "sold out on every platform it was found on"
        else:
            reason = "no bookable buy price found"
        return {**out, "action": NO_DATA, "reason": reason}

    if out["margin"] >= min_margin:
        return {**out, "action": OK, "reason": f"margin ${out['margin']:.2f}"}

    suggested = out["floor_price"]
    # Reasons a person should look before acting on the suggestion.
    checks = []
    if suggested - ours > REVIEW_RAISE_DOLLARS:
        checks.append("big jump — check the lot match")
    # A platform whose check failed (rate limit, API error) might be the
    # cheaper one; the floor is then only as good as the platforms that answered.
    failed = [_PLATFORM_NAMES.get(r.get("platform"), r.get("platform"))
              for r in readings if r.get("scarcity_level") == "unknown"]
    if failed:
        checks.append(f"{', '.join(failed)} could not be checked — it may be cheaper")
    reason = f"margin ${out['margin']:.2f} is under ${min_margin:.2f}"
    return {
        **out,
        "action": RAISE,
        "suggested_price": suggested,
        "needs_review": bool(checks),
        "review_note": "; ".join(checks) or None,
        "reason": reason + ("; " + "; ".join(checks) if checks else ""),
    }
