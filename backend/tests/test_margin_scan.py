"""Run from backend/:  python -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

if "DATABASE_PATH" not in os.environ:   # shared with the other test modules when run together
    os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402
from database import LystedSoldOutAlert, MarginCheck, ScarcityCheck, create_tables, get_session  # noqa: E402
import margin_report  # noqa: E402
import orchestrator  # noqa: E402
import stagefront_scan_runner  # noqa: E402


def listing(key, our_price, section):
    return {"reachpro_listing_id": key, "stagefront_listing_id": key, "reachpro_event_id": "ev1",
            "event_name": "Test Event", "event_venue": "Test Arena", "event_date": "2026-11-20 19:00",
            "section": section, "our_price": our_price, "region": "TX", "city": "Dallas",
            "state": "TX", "quantity": 4, "quantity_total": 4, "passes_in_hand": 1,
            "source": "stagefront"}


def check(platform, price, level="ok", available=True, quote_kind=None):
    return {"platform": platform, "price": price, "is_available": available,
            "scarcity_level": level, "quote_kind": quote_kind, "spots_left": None,
            "capacity": None, "percent_remaining": None, "availability_status": None, "error": None}


def clear():
    create_tables()
    db = get_session()
    for model in (ScarcityCheck, MarginCheck, LystedSoldOutAlert):
        db.query(model).delete()
    db.commit()
    db.close()


LISTINGS = [listing("L1", 50, "A"), listing("L2", 50, "B"), listing("L3", 50, "C")]
BY_SECTION = {
    # L1: $45 to buy -> $5 margin -> raise to $55
    "A": {"spothero": check("spothero", 45), "parkwhiz": check("parkwhiz", 47, quote_kind="event_rate"),
          "way": check("way", None, "not_found", False)},
    # L2: $35 -> $15 margin -> ok
    "B": {"spothero": check("spothero", 35), "parkwhiz": check("parkwhiz", None, "not_found", False),
          "way": check("way", None, "not_found", False)},
    # L3: sold out everywhere -> no buy price (and a sold-out alert, from the existing flow)
    "C": {"spothero": check("spothero", 40, "sold_out", False),
          "parkwhiz": check("parkwhiz", 42, "sold_out", False, "event_rate"),
          "way": check("way", None, "not_found", False)},
}


def run_stagefront_scan(notify=False):
    """The real Stagefront runner, with Stagefront's API and the buying platforms faked."""
    with mock.patch("stagefront_listings.sync_now", return_value={"status": "synced"}), \
            mock.patch.object(stagefront_scan_runner, "get_latest_sync", return_value=SimpleNamespace(id=1)), \
            mock.patch.object(stagefront_scan_runner, "load_active_listings", return_value=LISTINGS), \
            mock.patch("stagefront_listings.load_active_listings", return_value=LISTINGS), \
            mock.patch.object(orchestrator, "check_scarcity",
                              side_effect=lambda **kw: BY_SECTION[kw["our_section"]]), \
            mock.patch.object(orchestrator, "list_event_lots", return_value=[]), \
            mock.patch("spare_spots.load_rows", return_value=[]), \
            mock.patch.dict(os.environ, {"STAGEFRONT_ALLOW_WRITES": ""}):   # never a real deactivation
        return stagefront_scan_runner.run_and_persist_stagefront_scan(notify=notify)


class StagefrontScanStoresMargins(unittest.TestCase):

    def setUp(self):
        clear()

    def test_every_listing_gets_a_margin_row(self):
        out = run_stagefront_scan()
        self.assertEqual((out["margin_status"], out["margin_under"], out["margin_no_data"]), ("ok", 1, 1))

        db = get_session()
        try:
            rows = {r.listing_key: r for r in db.query(MarginCheck).all()}
        finally:
            db.close()
        self.assertEqual(set(rows), {"L1", "L2", "L3"})

        l1 = rows["L1"]
        self.assertEqual((l1.action, l1.our_price, l1.buy_price, l1.buy_platform, l1.margin, l1.suggested_price),
                         ("raise", 50, 45, "spothero", 5, 55))
        self.assertEqual((l1.spothero_price, l1.parkwhiz_price, l1.parkwhiz_quote_kind), (45, 47, "event_rate"))
        self.assertEqual((l1.source, l1.event_name, l1.section, l1.passes_in_hand), ("stagefront", "Test Event", "A", 1))

        self.assertEqual((rows["L2"].action, rows["L2"].margin), ("ok", 15))
        self.assertEqual(rows["L3"].action, "no_data")
        self.assertIn("sold out", rows["L3"].reason)

    def test_existing_sold_out_alerts_still_work(self):
        out = run_stagefront_scan()
        self.assertEqual(out["sold_out_alerts_detected"], 1)
        db = get_session()
        try:
            self.assertEqual([a.listing_key for a in db.query(LystedSoldOutAlert).all()], ["L3"])
        finally:
            db.close()

    def test_margin_crash_does_not_break_the_scan(self):
        with mock.patch.object(margin_report, "compute_margins", side_effect=RuntimeError("boom")):
            out = run_stagefront_scan()
        self.assertEqual(out["margin_status"], "failed")
        self.assertEqual(out["sold_out_alerts_detected"], 1)
        db = get_session()
        try:
            self.assertEqual(db.query(LystedSoldOutAlert).count(), 1)   # alert kept
            self.assertEqual(db.query(MarginCheck).count(), 0)          # nothing half-written
        finally:
            db.close()


class MarginsEndpoint(unittest.TestCase):
    """GET /api/stagefront/margins through the real app (schedulers don't start: no startup event)."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import main
        cls.client = TestClient(main.app)

    def setUp(self):
        clear()

    def test_empty_before_any_scan(self):
        body = self.client.get("/api/stagefront/margins").json()
        self.assertEqual((body["checked_at"], body["rows"]), (None, []))

    def test_latest_scan_only_problems_first(self):
        run_stagefront_scan()
        run_stagefront_scan()   # a second scan: its rows replace the first's on screen
        resp = self.client.get("/api/stagefront/margins")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["rows"]), 3)
        self.assertEqual([r["action"] for r in body["rows"]], ["raise", "no_data", "ok"])
        self.assertEqual(body["counts"], {"raise": 1, "no_data": 1, "ok": 1})
        self.assertEqual(body["min_margin"], 10.0)
        top = body["rows"][0]
        self.assertEqual((top["listing_key"], top["our_price"], top["buy_price"], top["suggested_price"]),
                         ("L1", 50, 45, 55))

    def test_lysted_rows_do_not_show_on_stagefront(self):
        db = get_session()
        db.add(MarginCheck(source="lysted", listing_key="X", action="raise",
                           checked_at=datetime.now(timezone.utc)))
        db.commit()
        db.close()
        self.assertEqual(self.client.get("/api/stagefront/margins").json()["rows"], [])


class Retention(unittest.TestCase):

    def setUp(self):
        clear()

    def test_old_margin_rows_are_pruned(self):
        now = datetime.now(timezone.utc)
        db = get_session()
        db.add(MarginCheck(source="stagefront", listing_key="old", action="ok", checked_at=now - timedelta(days=8)))
        db.add(MarginCheck(source="stagefront", listing_key="new", action="ok", checked_at=now - timedelta(days=1)))
        db.commit()
        db.close()

        database.prune_old_data()

        db = get_session()
        try:
            self.assertEqual([r.listing_key for r in db.query(MarginCheck).all()], ["new"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
