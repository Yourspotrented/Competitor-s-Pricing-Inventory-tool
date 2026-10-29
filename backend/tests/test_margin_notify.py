"""Run from backend/:  python -m unittest discover -s tests -v

Nothing here can reach Teams: requests.post is replaced in every test, and the
webhook variables are fake URLs (the real .env holds a live one).
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

if "DATABASE_PATH" not in os.environ:
    os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for the shared fixtures below

import margin_report  # noqa: E402
import teams_notify  # noqa: E402
import test_margin_scan as scan  # noqa: E402  (shared fixtures: LISTINGS, BY_SECTION, run_stagefront_scan)

MARGIN_URL = "https://example.invalid/margin"
SUMMARY_URL = "https://example.invalid/stagefront-summary"
FAKE_ENV = {k: "" for k in os.environ if k.endswith("TEAMS_WEBHOOK_URL")}
FAKE_ENV.update({"STAGEFRONT_MARGIN_TEAMS_WEBHOOK_URL": MARGIN_URL, "STAGEFRONT_TEAMS_WEBHOOK_URL": SUMMARY_URL})


def under(key="L1", our=50, buy=45, review=False, name="Test Event", sh_level="ok"):
    return {"listing_key": key, "event_name": name, "event_date": "2026-11-20T19:00:00", "section": "Lot A",
            "our_price": our, "buy_price": buy, "buy_platform": "spothero", "margin": our - buy,
            "suggested_price": buy + 10, "needs_review": review,
            "review_note": "big jump — check the lot match" if review else None, "action": "raise",
            "platform_detail": {"spothero": {"scarcity_level": sh_level}, "parkwhiz": {"scarcity_level": "ok"}}}


def card_text(post_call) -> str:
    return json.dumps(post_call.kwargs["json"])


class Fake200:
    status_code = 200

    def raise_for_status(self):
        pass


class NotifyMargins(unittest.TestCase):

    def setUp(self):
        self.env = mock.patch.dict(os.environ, FAKE_ENV)
        self.env.start()
        self.post = mock.patch.object(teams_notify.requests, "post", return_value=Fake200())
        self.sent = self.post.start()

    def tearDown(self):
        self.post.stop()
        self.env.stop()

    def test_nothing_new_sends_nothing(self):
        self.assertFalse(teams_notify.notify_margins([], 4, 10.0))
        self.sent.assert_not_called()

    def test_card_names_the_price_to_set(self):
        self.assertTrue(teams_notify.notify_margins([under()], 3, 10.0))
        self.assertEqual(self.sent.call_args.args[0], MARGIN_URL)
        text = card_text(self.sent.call_args)
        for expected in ("Stagefront: 1 listing under the $10 margin", "Test Event", "Lot A",
                         "Selling $50.00, costs $45.00 on SpotHero (margin $5.00)", "raise to $55.00",
                         "Report only", "Under $10 margin in total"):
            self.assertIn(expected, text)
        self.assertNotIn("Check before changing", text)

    def test_review_warning_and_worst_first(self):
        teams_notify.notify_margins([under("L1", 50, 45, name="Zebra Fest"), under("L2", 30, 60, True, name="Yak Tour")],
                                    2, 10.0)
        text = card_text(self.sent.call_args)
        self.assertIn("Check before changing the price: big jump", text)
        self.assertIn("Need a look first", text)
        self.assertLess(text.index("Yak Tour"), text.index("Zebra Fest"))   # -30 margin before +5

    def test_falls_back_to_summary_group(self):
        with mock.patch.dict(os.environ, {"STAGEFRONT_MARGIN_TEAMS_WEBHOOK_URL": ""}):
            self.assertTrue(teams_notify.notify_margins([under()], 1, 10.0))
        self.assertEqual(self.sent.call_args.args[0], SUMMARY_URL)

    def test_no_group_configured_is_quiet(self):
        with mock.patch.dict(os.environ, {"STAGEFRONT_MARGIN_TEAMS_WEBHOOK_URL": "", "STAGEFRONT_TEAMS_WEBHOOK_URL": ""}):
            self.assertFalse(teams_notify.notify_margins([under()], 1, 10.0))
        self.sent.assert_not_called()


class OnlyNewsIsPosted(unittest.TestCase):

    def test_is_news(self):
        news = margin_report._is_news
        raise55, raise52, ok = ({"action": "raise", "suggested_price": 55}, {"action": "raise", "suggested_price": 52},
                                {"action": "ok", "suggested_price": None})
        now = under(buy=45)                                   # conclusive: needs $55
        self.assertTrue(news(now, None))                      # first time seen
        self.assertTrue(news(now, {"any": ok, "conclusive": ok}))
        self.assertFalse(news(now, {"any": raise55, "conclusive": raise55}))    # same as before
        self.assertTrue(news(now, {"any": raise52, "conclusive": raise52}))     # price moved
        # Last hour was inconclusive at a different price; the reliable one before matches -> quiet.
        self.assertFalse(news(now, {"any": raise52, "conclusive": raise55}))
        self.assertFalse(news({**now, "action": "ok"}, None))

        failed = under(buy=47, sh_level="unknown")            # inconclusive: SpotHero failed
        self.assertTrue(news(failed, None))                   # new problem -> report once (marked Review)
        self.assertTrue(news(failed, {"any": ok, "conclusive": ok}))
        self.assertFalse(news(failed, {"any": raise55, "conclusive": raise55}))  # its price "moving" is no news


class ThroughTheStagefrontScan(unittest.TestCase):
    """The real runner with notify=True — which cards go where, scan after scan."""

    def setUp(self):
        scan.clear()
        self.env = mock.patch.dict(os.environ, FAKE_ENV)
        self.env.start()
        self.post = mock.patch.object(teams_notify.requests, "post", return_value=Fake200())
        self.sent = self.post.start()

    def tearDown(self):
        self.post.stop()
        self.env.stop()

    def run_notifying(self):
        return scan.run_stagefront_scan(notify=True)

    def margin_posts(self):
        return [c for c in self.sent.call_args_list if c.args[0] == MARGIN_URL]

    def test_first_scan_posts_then_repeat_is_quiet(self):
        out = self.run_notifying()
        self.assertEqual((out["margin_new_under"], out["margin_notified"]), (1, True))
        [card] = self.margin_posts()
        self.assertIn("raise to $55.00", card_text(card))

        self.sent.reset_mock()
        out = self.run_notifying()   # same prices again
        self.assertEqual((out["margin_new_under"], out["margin_notified"]), (0, False))
        self.assertEqual(self.margin_posts(), [])

    def test_price_move_posts_again(self):
        self.run_notifying()
        self.sent.reset_mock()
        # SpotHero $45 -> $43 (still the cheapest): needed price $55 -> $53.
        moved = dict(scan.BY_SECTION, A={**scan.BY_SECTION["A"], "spothero": scan.check("spothero", 43)})
        with mock.patch.dict(scan.BY_SECTION, moved):
            out = self.run_notifying()
        self.assertEqual(out["margin_new_under"], 1)
        [card] = self.margin_posts()
        self.assertIn("raise to $53.00", card_text(card))

    def hour(self, spothero_a=None, parkwhiz_a=None):
        """One hourly scan; returns the margin card text posted, or None. *_a override lot A's readings."""
        self.sent.reset_mock()
        sections = dict(scan.BY_SECTION)
        sections["A"] = dict(scan.BY_SECTION["A"])
        if spothero_a is not None:
            sections["A"]["spothero"] = spothero_a
        if parkwhiz_a is not None:
            sections["A"]["parkwhiz"] = parkwhiz_a
        with mock.patch.dict(scan.BY_SECTION, sections):
            self.run_notifying()
        posts = self.margin_posts()
        self.assertLessEqual(len(posts), 1)
        return card_text(posts[0]) if posts else None

    def test_spothero_blocked_for_an_hour_does_not_repeat_the_alert(self):
        blocked = scan.check("spothero", None, "unknown", None)
        self.assertIn("raise to $55.00", self.hour())   # 1: under margin -> reported
        self.assertIsNone(self.hour(blocked))           # 2: SpotHero blocked (ParkWhiz alone says $57) -> quiet
        self.assertIsNone(self.hour())                  # 3: back to normal, same $55 -> quiet
        self.assertIsNone(self.hour(blocked, scan.check("parkwhiz", None, "unknown", None)))   # 4: both failed -> quiet
        self.assertIsNone(self.hour())                  # 5: still the same -> quiet

    def test_lot_gone_from_spothero_is_a_real_change(self):
        # "not_found" is SpotHero confirming our lot isn't listed — a real reading,
        # unlike a failed check. ParkWhiz's $47 is now the cost: one correction.
        self.assertIn("raise to $55.00", self.hour())
        self.assertIn("raise to $57.00", self.hour(scan.check("spothero", None, "not_found", False)))
        self.assertIsNone(self.hour(scan.check("spothero", None, "not_found", False)))

    def test_spothero_down_from_the_start_reports_once_then_corrects_once(self):
        blocked = scan.check("spothero", None, "unknown", None)
        first = self.hour(blocked)                      # 1: only ParkWhiz answered -> reported, marked for review
        self.assertIn("raise to $57.00", first)
        self.assertIn("SpotHero could not be checked", first)
        self.assertIsNone(self.hour(blocked))           # 2: same situation -> quiet
        fixed = self.hour()                             # 3: SpotHero back, real price is $55 -> one correction
        self.assertIn("raise to $55.00", fixed)
        self.assertNotIn("could not be checked", fixed)
        self.assertIsNone(self.hour())                  # 4: quiet again

    def test_repriced_then_under_again_is_reported_again(self):
        self.assertIsNotNone(self.hour())                         # under margin
        self.assertIsNone(self.hour(scan.check("spothero", 30)))  # team fixed it / market fell: OK, nothing to post
        self.assertIn("raise to $55.00", self.hour())             # under again -> new problem

    def test_card_failure_does_not_break_the_scan(self):
        with mock.patch.object(scan.stagefront_scan_runner, "notify_margins", side_effect=RuntimeError("boom")):
            out = self.run_notifying()
        self.assertEqual((out["margin_status"], out["margin_notified"], out["sold_out_alerts_detected"]), ("ok", False, 1))


if __name__ == "__main__":
    unittest.main()
