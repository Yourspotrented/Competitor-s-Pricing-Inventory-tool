"""Run from backend/:  python -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

# A throwaway database — must be set before `database` is first imported.
_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402
from database import ScarcityCheck, create_tables, get_session  # noqa: E402
import margin  # noqa: E402
import margin_report  # noqa: E402
import orchestrator  # noqa: E402


def listing(key, our_price, section="Lot A - 100 Main St"):
    return {"reachpro_listing_id": key, "stagefront_listing_id": key, "reachpro_event_id": "ev1",
            "event_name": "Test Event", "event_venue": "Test Arena", "event_date": "2026-11-20 19:00",
            "section": section, "our_price": our_price, "region": "TX", "city": "Dallas",
            "state": "TX", "quantity": 4, "quantity_total": 4, "passes_in_hand": 0,
            "source": "stagefront"}


def check(platform, price, level="ok", available=True, quote_kind=None):
    return {"platform": platform, "price": price, "is_available": available,
            "scarcity_level": level, "quote_kind": quote_kind, "spots_left": None,
            "capacity": None, "percent_remaining": None, "availability_status": None, "error": None}


class ComputeMargins(unittest.TestCase):

    def setUp(self):
        create_tables()
        self.db = get_session()
        self.db.query(ScarcityCheck).delete()
        self.db.commit()
        self.scan_started = datetime.now(timezone.utc)

    def tearDown(self):
        self.db.close()

    def add(self, key, platform, price, at=None, source="stagefront", level="ok",
            available=True, quote_kind=None):
        self.db.add(ScarcityCheck(
            reachpro_listing_id=key, reachpro_event_id="ev1", source=source, platform=platform,
            price=price, is_available=available, scarcity_level=level, quote_kind=quote_kind,
            checked_at=at or self.scan_started + timedelta(seconds=1)))
        self.db.commit()

    def test_uses_this_scan_and_ignores_older_readings(self):
        # An hour ago the pass cost $30 (margin fine); this scan says $45.
        self.add("L1", "spothero", 30, at=self.scan_started - timedelta(hours=1))
        self.add("L1", "spothero", 45)
        [r] = margin_report.compute_margins(self.db, [listing("L1", 50)], self.scan_started, "stagefront")
        self.assertEqual((r["buy_price"], r["action"], r["suggested_price"]), (45, margin.RAISE, 55))

    def test_only_old_readings_means_no_data(self):
        self.add("L1", "spothero", 45, at=self.scan_started - timedelta(hours=1))
        [r] = margin_report.compute_margins(self.db, [listing("L1", 50)], self.scan_started, "stagefront")
        self.assertEqual(r["action"], margin.NO_DATA)

    def test_latest_reading_per_platform_wins(self):
        self.add("L1", "parkwhiz", 30, at=self.scan_started + timedelta(seconds=1))
        self.add("L1", "parkwhiz", 44, at=self.scan_started + timedelta(seconds=5))
        [r] = margin_report.compute_margins(self.db, [listing("L1", 50)], self.scan_started, "stagefront")
        self.assertEqual(r["buy_price"], 44)

    def test_way_and_other_sources_are_ignored(self):
        self.add("L1", "way", 20)                       # not a platform the team buys from
        self.add("L1", "spothero", 25, source="lysted")  # another selling flow's reading
        self.add("L1", "spothero", 45)
        [r] = margin_report.compute_margins(self.db, [listing("L1", 50)], self.scan_started, "stagefront")
        self.assertEqual((r["buy_price"], r["buy_platform"]), (45, "spothero"))
        self.assertNotIn("way", r["platform_detail"])

    def test_parkwhiz_standard_rate_ignored_end_to_end(self):
        self.add("L1", "parkwhiz", 30, quote_kind="standard")
        self.add("L1", "spothero", 45)
        [r] = margin_report.compute_margins(self.db, [listing("L1", 50)], self.scan_started, "stagefront")
        self.assertEqual(r["buy_price"], 45)
        self.assertEqual(r["platform_detail"]["parkwhiz"]["quote_kind"], "standard")

    def test_listings_are_kept_apart(self):
        self.add("L1", "spothero", 45)
        self.add("L2", "spothero", 20)
        out = margin_report.compute_margins(self.db, [listing("L1", 50), listing("L2", 50)],
                                            self.scan_started, "stagefront")
        self.assertEqual([r["action"] for r in out], [margin.RAISE, margin.OK])
        self.assertEqual(out[0]["listing_key"], "L1")

    def test_no_listings(self):
        self.assertEqual(margin_report.compute_margins(self.db, [], self.scan_started, "stagefront"), [])


class ThroughTheRealScan(unittest.TestCase):
    """orchestrator.run_scan with the platform calls faked: does quote_kind reach the row and the decision?"""

    def setUp(self):
        create_tables()
        db = get_session()
        db.query(ScarcityCheck).delete()
        db.commit()
        db.close()

    def test_scan_rows_feed_the_margin_check(self):
        listings = [listing("L1", 50), listing("L2", 50)]
        results = {
            "L1": {"spothero": check("spothero", 45),
                   "parkwhiz": check("parkwhiz", 30, quote_kind="standard"),
                   "way": check("way", 10)},
            "L2": {"spothero": check("spothero", None, "not_found", False),
                   "parkwhiz": check("parkwhiz", 38, quote_kind="event_rate"),
                   "way": check("way", None, "not_found", False)},
        }
        by_section = {"A": results["L1"], "B": results["L2"]}
        listings[0]["section"], listings[1]["section"] = "A", "B"

        scan_started = datetime.now(timezone.utc)
        with mock.patch("stagefront_listings.load_active_listings", return_value=listings), \
                mock.patch.object(orchestrator, "check_scarcity",
                                  side_effect=lambda **kw: by_section[kw["our_section"]]), \
                mock.patch.object(orchestrator, "list_event_lots", return_value=[]):
            summary = orchestrator.run_scan(source="stagefront")
        self.assertEqual(summary["status"], "completed")

        db = get_session()
        try:
            pw = db.query(ScarcityCheck).filter_by(reachpro_listing_id="L1", platform="parkwhiz").one()
            self.assertEqual(pw.quote_kind, "standard")
            out = margin_report.compute_margins(db, listings, scan_started, "stagefront")
        finally:
            db.close()

        l1, l2 = out
        # L1: ParkWhiz $30 is a standard rate and Way $10 isn't bought from, so $45 is the cost.
        self.assertEqual((l1["buy_price"], l1["action"], l1["suggested_price"]), (45, margin.RAISE, 55))
        # L2: only on ParkWhiz, event rate $38 -> $12 margin.
        self.assertEqual((l2["buy_price"], l2["buy_platform"], l2["action"]), (38, "parkwhiz", margin.OK))


if __name__ == "__main__":
    unittest.main()
