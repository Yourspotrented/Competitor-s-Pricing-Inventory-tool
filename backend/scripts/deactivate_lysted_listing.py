"""
Deactivate ONE Lysted listing by id, and read it back to prove it worked.

    .venv/bin/python scripts/deactivate_lysted_listing.py 24189389

This is the same call the Lysted web app makes when you flip "Not
Broadcasting" and press Save Changes:

    PUT https://api.lysted.com/api/listings/<id>   body: {"broadcast": false}

It uses the token saved on the dashboard, touches exactly the ids given on the
command line, and prints the listing's state before and after. Nothing is
scanned, nothing else is changed, and no Teams card is sent.

LYSTED_ALLOW_WRITES is set for this process only, so the scheduled scans stay
read-only until that switch is turned on deliberately.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["LYSTED_ALLOW_WRITES"] = "1"      # this process only; .env untouched

import listing_actions          # noqa: E402
import lysted_api               # noqa: E402

FIELDS = ("id", "section", "quantity", "broadcast", "status", "price")


def state(listing_id: str) -> dict:
    listing = lysted_api._get(f"/listings/{listing_id}", {})["listing"]
    return {k: listing.get(k) for k in FIELDS}


def main(ids: list) -> int:
    status = lysted_api.api_status()
    if not status["usable"]:
        print(f"Lysted token unusable: {status['reason']}")
        return 1
    print(f"token valid for {status.get('hours_left')}h\n")

    failures = 0
    for listing_id in ids:
        print(f"--- {listing_id} ---")
        try:
            before = state(listing_id)
        except Exception as exc:
            print(f"  could not read it: {exc}")
            failures += 1
            continue
        print(f"  before : {json.dumps(before, default=str)}")
        if not before.get("broadcast"):
            print("  skipped: already not broadcasting")
            continue

        result = listing_actions.apply_action(
            {"reachpro_listing_id": f"lysted:{listing_id}", "lysted_listing_id": listing_id},
            {"action": "deactivate", "target_quantity": 0, "reason": "manual deactivation"},
            "lysted",
        )
        print(f"  sent   : applied={result['applied']} {result.get('reason') or ''}")
        after = state(listing_id)
        print(f"  after  : {json.dumps(after, default=str)}")
        if after.get("broadcast"):
            print("  FAILED : still broadcasting")
            failures += 1
        else:
            print("  OK     : deactivated")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1:]))
