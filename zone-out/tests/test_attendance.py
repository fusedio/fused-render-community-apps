import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import attendance  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 16, 14, 0, tzinfo=UTC)
EVENT = {"key": "u1::2026-09-16T14:00:00Z", "uid": "u1", "title": "Standup", "start": "2026-09-16T14:00:00Z",
         "end": "2026-09-16T14:30:00Z", "links": [{"provider": "meet", "url": "https://meet.google.com/abc-defg-hij", "code": "abc-defg-hij"}],
         "attendees": [{"email": "me@x.io", "partstat": "ACCEPTED", "name": ""}]}
S = attendance.merge_settings({"my_email": "me@x.io"})


def P(state, other=()):
    return {"state": state, "confidence": "high", "via": "test", "app": "", "other_calls": list(other)}


class Settings(unittest.TestCase):
    def test_merge_normalises(self):
        s = attendance.merge_settings({"ics_urls": "a\n b \n", "poll_s": "3", "unknown": 1})
        self.assertEqual(s["ics_urls"], ["a", "b"])
        self.assertEqual(s["poll_s"], 10)
        self.assertNotIn("unknown", s)

    def test_tracking_rules(self):
        self.assertEqual(attendance.is_tracked(EVENT, S), (True, "all meetings"))
        declined = dict(EVENT, attendees=[{"email": "me@x.io", "partstat": "DECLINED", "name": ""}])
        self.assertEqual(attendance.is_tracked(declined, S)[0], False)
        self.assertEqual(attendance.is_tracked(declined, attendance.merge_settings({"my_email": "me@x.io", "track_declined": True}))[0], True)
        self.assertEqual(attendance.is_tracked(EVENT, attendance.merge_settings({"ignored_uids": ["u1"]})), (False, "ignored by you"))
        sel = attendance.merge_settings({"track_mode": "selected", "tracked_uids": ["other"]})
        self.assertEqual(attendance.is_tracked(EVENT, sel), (False, "not selected"))
        self.assertEqual(attendance.is_tracked(dict(EVENT, links=[]), attendance.merge_settings({"require_link": True}))[0], False)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = attendance.Store(self.tmp, local_tz=UTC)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_segments_and_durations(self):
        st = self.store
        rec = st.observe(EVENT, "absent", T0, True)
        rec = st.observe(EVENT, "absent", T0 + dt.timedelta(minutes=1), True)
        rec = st.observe(EVENT, "in_call", T0 + dt.timedelta(minutes=2), True)
        rec = st.observe(EVENT, "in_call", T0 + dt.timedelta(minutes=12), True)
        self.assertEqual([s[0] for s in rec["segments"]], ["absent", "in_call"])
        self.assertEqual(rec["first_in"], "2026-09-16T14:02:00Z")
        self.assertEqual(rec["late_s"], 120.0)
        # 10 min span + capped 60 s lead-in
        self.assertEqual(rec["in_s"], 660.0)
        self.assertEqual(rec["verdict"], "attending")  # provisional while in the call
        self.assertFalse(rec["final"])

    def test_finalize_and_persist(self):
        st = self.store
        st.observe(EVENT, "absent", T0 + dt.timedelta(minutes=5), True)
        rec = st.observe(EVENT, "absent", T0 + dt.timedelta(minutes=31), True)
        self.assertTrue(rec["final"])
        self.assertEqual(rec["verdict"], "missed")
        again = attendance.Store(self.tmp, local_tz=UTC)
        self.assertEqual(again.records_for_day("2026-09-16")[EVENT["key"]]["verdict"], "missed")
        self.assertEqual(again.days(), ["2026-09-16"])

    def test_partial(self):
        st = self.store
        st.observe(EVENT, "in_call", T0 + dt.timedelta(minutes=1), True)
        st.observe(EVENT, "in_call", T0 + dt.timedelta(minutes=3), True)
        rec = st.observe(EVENT, "absent", T0 + dt.timedelta(minutes=31), True)
        self.assertEqual(rec["verdict"], "partial")


class Alerts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = attendance.Store(self.tmp, local_tz=UTC)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_heads_up_once(self):
        rec = self.store.record(EVENT)
        d = attendance.alert_decision(rec, EVENT, P("absent"), T0 - dt.timedelta(minutes=2), S, False)
        self.assertEqual(d["kind"], "heads_up")
        self.store.add_alert(rec, "heads_up", d["title"], T0 - dt.timedelta(minutes=2))
        self.assertIsNone(attendance.alert_decision(rec, EVENT, P("absent"), T0 - dt.timedelta(minutes=1), S, False))

    def test_missing_after_grace_then_repeat(self):
        rec = self.store.record(EVENT)
        self.assertIsNone(attendance.alert_decision(rec, EVENT, P("absent"), T0 + dt.timedelta(minutes=1), S, False))
        d = attendance.alert_decision(rec, EVENT, P("absent", other=["Slack"]), T0 + dt.timedelta(minutes=2), S, False)
        self.assertEqual(d["kind"], "missing")
        self.assertIn("Slack", d["message"])
        self.store.add_alert(rec, "missing", d["title"], T0 + dt.timedelta(minutes=2))
        # default repeat: every minute until in the call
        self.assertEqual(S["repeat_min"], 1)
        self.assertIsNone(attendance.alert_decision(rec, EVENT, P("absent"), T0 + dt.timedelta(minutes=2, seconds=40), S, False))
        d2 = attendance.alert_decision(rec, EVENT, P("absent"), T0 + dt.timedelta(minutes=3, seconds=1), S, False)
        self.assertEqual(d2["kind"], "missing")
        slow = attendance.merge_settings({"repeat_min": 3, "escalate_unacknowledged": False})
        self.store.add_alert(rec, "missing", d2["title"], T0 + dt.timedelta(minutes=3, seconds=1))
        self.assertIsNone(attendance.alert_decision(rec, EVENT, P("absent"), T0 + dt.timedelta(minutes=5), slow, False))
        self.assertIsNotNone(attendance.alert_decision(rec, EVENT, P("absent"), T0 + dt.timedelta(minutes=6, seconds=2), slow, False))

    def test_escalation_ladder_until_interaction(self):
        rec = self.store.record(EVENT)
        t = T0 + dt.timedelta(minutes=2)
        fire = lambda at: self.store.add_alert(rec, "missing", "m", at)  # noqa: E731
        due = lambda at: attendance.alert_decision(rec, EVENT, P("absent"), at, S, False) is not None  # noqa: E731
        self.assertTrue(due(t)); fire(t)                       # 1st warning, level 1
        self.assertFalse(due(t + dt.timedelta(seconds=59)))
        self.assertTrue(due(t + dt.timedelta(seconds=60))); fire(t + dt.timedelta(seconds=60))   # 2nd, level 2
        self.assertFalse(due(t + dt.timedelta(seconds=89)))
        self.assertTrue(due(t + dt.timedelta(seconds=90))); fire(t + dt.timedelta(seconds=90))   # 3rd, level 3
        self.assertFalse(due(t + dt.timedelta(seconds=104)))
        self.assertTrue(due(t + dt.timedelta(seconds=105))); fire(t + dt.timedelta(seconds=105))  # 4th: every 15 s
        self.assertTrue(due(t + dt.timedelta(seconds=120)))
        self.assertEqual(rec["nag_level"], 4)
        # the user touches a warning: back to one reminder a minute, counted from the interaction
        self.store.ack(rec, t + dt.timedelta(seconds=110))
        self.assertEqual(rec["nag_level"], 0)
        self.assertFalse(due(t + dt.timedelta(seconds=169)))
        self.assertTrue(due(t + dt.timedelta(seconds=170)))
        fire(t + dt.timedelta(seconds=170))
        self.assertEqual(rec["nag_level"], 1)
        self.assertFalse(due(t + dt.timedelta(seconds=200)))
        self.assertTrue(due(t + dt.timedelta(seconds=230)))

    def test_escalation_can_be_turned_off(self):
        rec = self.store.record(EVENT)
        flat = attendance.merge_settings({"escalate_unacknowledged": False})
        t = T0 + dt.timedelta(minutes=2)
        for i in range(3):
            self.store.add_alert(rec, "missing", "m", t + dt.timedelta(seconds=60 * i))
        self.assertIsNone(attendance.alert_decision(rec, EVENT, P("absent"), t + dt.timedelta(seconds=150), flat, False))
        self.assertIsNotNone(attendance.alert_decision(rec, EVENT, P("absent"), t + dt.timedelta(seconds=180), flat, False))
        self.assertEqual(attendance.next_interval_s(rec, S), 15.0)
        self.assertEqual(attendance.next_interval_s(rec, flat), 60.0)

    def test_heads_up_does_not_count_as_nag(self):
        rec = self.store.record(EVENT)
        self.store.add_alert(rec, "heads_up", "h", T0 - dt.timedelta(minutes=2))
        self.assertEqual(rec["nag_level"], 0)

    def test_lobby_kind_and_snooze_skip(self):
        rec = self.store.record(EVENT)
        d = attendance.alert_decision(rec, EVENT, P("lobby"), T0 + dt.timedelta(minutes=3), S, False)
        self.assertEqual(d["kind"], "lobby")
        self.store.set_flags(rec, snoozed_until="2026-09-16T14:10:00Z")
        self.assertIsNone(attendance.alert_decision(rec, EVENT, P("absent"), T0 + dt.timedelta(minutes=5), S, False))
        self.assertIsNotNone(attendance.alert_decision(rec, EVENT, P("absent"), T0 + dt.timedelta(minutes=11), S, False))
        self.store.set_flags(rec, skipped=True)
        self.assertIsNone(attendance.alert_decision(rec, EVENT, P("absent"), T0 + dt.timedelta(minutes=12), S, False))

    def test_dropped(self):
        self.store.observe(EVENT, "in_call", T0 + dt.timedelta(minutes=1), True)
        rec = self.store.observe(EVENT, "absent", T0 + dt.timedelta(minutes=2), True)
        self.assertIsNone(attendance.alert_decision(rec, EVENT, P("absent"), T0 + dt.timedelta(minutes=2), S, False))
        d = attendance.alert_decision(rec, EVENT, P("absent"), T0 + dt.timedelta(minutes=3, seconds=30), S, False)
        self.assertEqual(d["kind"], "dropped")

    def test_in_call_and_other_meeting_suppress(self):
        rec = self.store.record(EVENT)
        self.assertIsNone(attendance.alert_decision(rec, EVENT, P("in_call"), T0 + dt.timedelta(minutes=5), S, False))
        self.assertIsNone(attendance.alert_decision(rec, EVENT, P("absent"), T0 + dt.timedelta(minutes=5), S, True))
        self.assertIsNone(attendance.alert_decision(rec, EVENT, P("absent"), T0 + dt.timedelta(minutes=40), S, False))


class SharedCalls(unittest.TestCase):
    def test_low_confidence_loses_to_exact_match(self):
        pres = {
            "a": {"state": "in_call", "confidence": "high", "via": "meet tab", "app": "Google Chrome", "other_calls": []},
            "b": {"state": "in_call", "confidence": "low", "via": "no link; a call is active", "app": "Google Chrome", "other_calls": []},
            "c": {"state": "in_call", "confidence": "low", "via": "no link; a call is active", "app": "Slack", "other_calls": []},
        }
        confident = attendance.attribute_shared_calls(pres, {"a": "Standup", "b": "test", "c": "huddle"})
        self.assertEqual(confident, {"a"})
        self.assertEqual(pres["b"]["state"], "absent")
        self.assertIn("Standup", pres["b"]["via"])
        self.assertEqual(pres["b"]["other_calls"], ["Google Chrome"])
        self.assertEqual(pres["c"]["state"], "in_call")  # different app, nobody claimed it

    def test_nothing_claimed_keeps_guess(self):
        pres = {"b": {"state": "in_call", "confidence": "low", "via": "", "app": "Google Chrome", "other_calls": []}}
        self.assertEqual(attendance.attribute_shared_calls(pres, {}), set())
        self.assertEqual(pres["b"]["state"], "in_call")


class Kinds(unittest.TestCase):
    ME = "me@x.io"
    OTHERS = [{"email": "me@x.io", "partstat": "ACCEPTED", "name": ""}, {"email": "bob@x.io", "partstat": "ACCEPTED", "name": ""}]

    def test_classify(self):
        self.assertEqual(attendance.classify_kind(EVENT, self.ME), ("call", 0))
        self.assertEqual(attendance.classify_kind(dict(EVENT, links=[], attendees=self.OTHERS), self.ME), ("needs_link", 1))
        self.assertEqual(attendance.classify_kind(dict(EVENT, links=[], attendees=self.OTHERS, location="Room 4B"), self.ME), ("in_person", 1))
        self.assertEqual(attendance.classify_kind(dict(EVENT, links=[], attendees=[]), self.ME), ("personal", 0))
        self.assertEqual(attendance.classify_kind(dict(EVENT, links=[], attendees=self.OTHERS[:1]), self.ME), ("personal", 0))

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = attendance.Store(self.tmp, local_tz=UTC)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_needs_link_gets_one_clear_heads_up_and_no_alarms(self):
        ev = dict(EVENT, links=[], attendees=self.OTHERS)
        rec = self.store.record(ev)
        early = attendance.alert_decision(rec, ev, P("absent"), T0 - dt.timedelta(minutes=5), S, False, kind="needs_link", n_others=1)
        self.assertIsNone(early)
        d = attendance.alert_decision(rec, ev, P("absent"), T0 - dt.timedelta(minutes=2), S, False, kind="needs_link", n_others=1)
        self.assertEqual(d["kind"], "needs_link")
        self.assertTrue(d["soft"])
        self.assertIn("Add a meeting link", d["title"])
        self.assertIn("1 other invitee but has no Meet, Zoom or Teams link", d["message"])
        self.store.add_alert(rec, "needs_link", d["title"], T0 - dt.timedelta(minutes=2))
        for m in (0, 3, 10):
            self.assertIsNone(attendance.alert_decision(rec, ev, P("absent"), T0 + dt.timedelta(minutes=m), S, False, kind="needs_link", n_others=1))
        self.assertEqual(rec["nag_level"], 0)

    def test_needs_link_discovered_late_fires_once(self):
        ev = dict(EVENT, links=[], attendees=self.OTHERS)
        rec = self.store.record(ev)
        d = attendance.alert_decision(rec, ev, P("absent"), T0 + dt.timedelta(minutes=4), S, False, kind="needs_link", n_others=2)
        self.assertEqual(d["kind"], "needs_link")
        self.assertIn("Started at", d["message"])
        self.assertIn("2 other invitees", d["message"])

    def test_in_person_heads_up_only(self):
        ev = dict(EVENT, links=[], attendees=self.OTHERS, location="Room 4B")
        rec = self.store.record(ev)
        d = attendance.alert_decision(rec, ev, P("absent"), T0 - dt.timedelta(minutes=1), S, False, kind="in_person", n_others=1)
        self.assertEqual(d["kind"], "heads_up")
        self.assertIn("Room 4B", d["message"])
        self.store.add_alert(rec, "heads_up", d["title"], T0 - dt.timedelta(minutes=1))
        self.assertIsNone(attendance.alert_decision(rec, ev, P("absent"), T0 + dt.timedelta(minutes=5), S, False, kind="in_person", n_others=1))
        # no heads-up configured: nothing at all
        quiet = attendance.merge_settings({"heads_up_min": 0})
        rec2 = self.store.record(dict(ev, key="k2::x"))
        self.assertIsNone(attendance.alert_decision(rec2, ev, P("absent"), T0 - dt.timedelta(minutes=1), quiet, False, kind="in_person", n_others=1))

    def test_personal_is_silent(self):
        ev = dict(EVENT, links=[], attendees=[])
        rec = self.store.record(ev)
        for m in (-2, 0, 5):
            self.assertIsNone(attendance.alert_decision(rec, ev, P("absent"), T0 + dt.timedelta(minutes=m), S, False, kind="personal"))

    def test_summary_unverifiable_never_missing(self):
        base = {"start": "2026-09-16T14:00:00Z", "end": "2026-09-16T14:30:00Z", "tracked": True, "record": {}, "phase": "live",
                "presence": {"state": "unknown", "confidence": "none", "via": "", "app": "", "other_calls": []}}
        ip = dict(base, key="a", title="Lunch", kind="in_person", location="Cafe", n_others=2)
        s = attendance.live_summary([ip], T0 + dt.timedelta(minutes=5), S)
        self.assertEqual((s["status"], s["kind"]), ("unverifiable", "in_person"))
        self.assertIn("Cafe", s["detail"])
        nl = dict(base, key="b", title="Sync", kind="needs_link", n_others=3)
        s = attendance.live_summary([nl], T0 + dt.timedelta(minutes=5), S)
        self.assertEqual(s["status"], "unverifiable")
        self.assertIn("no meeting link", s["detail"])
        # a real missing call still wins over an unverifiable entry
        missing = dict(base, key="m", title="Real", kind="call", presence=P("absent"))
        s = attendance.live_summary([ip, missing], T0 + dt.timedelta(minutes=5), S)
        self.assertEqual((s["status"], s["title"]), ("missing", "Real"))


class Summary(unittest.TestCase):
    def test_missing_beats_unverified_call(self):
        base = {"start": "2026-09-16T14:00:00Z", "end": "2026-09-16T14:30:00Z", "tracked": True, "record": {}, "phase": "live"}
        guess = dict(base, key="g", title="test", presence={"state": "in_call", "confidence": "low", "via": "", "app": "Google Chrome", "other_calls": []})
        missing = dict(base, key="m", title="Real", presence=P("absent"))
        s = attendance.live_summary([guess, missing], T0 + dt.timedelta(minutes=3), S)
        self.assertEqual((s["status"], s["title"]), ("missing", "Real"))
        s = attendance.live_summary([guess], T0 + dt.timedelta(minutes=3), S)
        self.assertEqual((s["status"], s["confidence"]), ("in_meeting", "low"))
        sure = dict(base, key="s", title="Sure", presence={"state": "in_call", "confidence": "high", "via": "", "app": "Google Chrome", "other_calls": []})
        s = attendance.live_summary([guess, missing, sure], T0 + dt.timedelta(minutes=3), S)
        self.assertEqual((s["status"], s["title"]), ("in_meeting", "Sure"))

    def test_states(self):
        base = {"key": "k", "title": "T", "start": "2026-09-16T14:00:00Z", "end": "2026-09-16T14:30:00Z", "tracked": True, "record": {}}
        ev = dict(base, phase="live", presence=P("absent"))
        self.assertEqual(attendance.live_summary([ev], T0 + dt.timedelta(minutes=3), S)["status"], "missing")
        self.assertEqual(attendance.live_summary([ev], T0 + dt.timedelta(minutes=1), S)["status"], "starting")
        ev2 = dict(base, phase="live", presence=P("in_call"))
        self.assertEqual(attendance.live_summary([ev2, ev], T0 + dt.timedelta(minutes=3), S)["status"], "in_meeting")
        ev3 = dict(base, phase="upcoming", presence=P("absent"))
        self.assertEqual(attendance.live_summary([ev3], T0 - dt.timedelta(hours=1), S)["status"], "free")
        ev4 = dict(base, phase="live", presence=P("absent"), tracked=False)
        self.assertEqual(attendance.live_summary([ev4], T0 + dt.timedelta(minutes=3), S)["status"], "untracked")


if __name__ == "__main__":
    unittest.main()
