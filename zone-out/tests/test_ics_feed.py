import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ics_feed  # noqa: E402

UTC = dt.timezone.utc
NY = ics_feed._tz("America/New_York")

FEED = """BEGIN:VCALENDAR
PRODID:-//Google Inc//Google Calendar 70.9054//EN
VERSION:2.0
X-WR-CALNAME:sk2@fused.io
X-WR-TIMEZONE:America/New_York
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260914T100000
DTEND;TZID=America/New_York:20260914T101500
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
EXDATE;TZID=America/New_York:20260917T100000
DTSTAMP:20260916T120000Z
UID:standup@google.com
ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;CN=sk2@fus
 ed.io;X-NUM-GUESTS=0:mailto:sk2@fused.io
SUMMARY:Daily standup
DESCRIPTION:Join here: <a href="https://meet.google.com/abc-defg-hij">meet<
 /a>\\nAgenda\\, notes
X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij
STATUS:CONFIRMED
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260918T113000
DTEND;TZID=America/New_York:20260918T114500
RECURRENCE-ID;TZID=America/New_York:20260918T100000
UID:standup@google.com
SUMMARY:Daily standup (moved)
X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij
END:VEVENT
BEGIN:VEVENT
DTSTART:20260916T180000Z
DTEND:20260916T190000Z
UID:zoomcall@example.com
SUMMARY:Design review
LOCATION:https://us02web.zoom.us/j/81234567890?pwd=abcDEF
ATTENDEE;PARTSTAT=DECLINED:mailto:sk2@fused.io
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260916
DTEND;VALUE=DATE:20260917
UID:allday@example.com
SUMMARY:Company holiday
END:VEVENT
BEGIN:VEVENT
DTSTART:20260916T200000Z
DURATION:PT45M
UID:cancelled@example.com
SUMMARY:Cancelled thing
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
DTSTART:20260916T210000Z
DTEND:20260916T213000Z
UID:teams@example.com
SUMMARY:Teams sync
DESCRIPTION:Microsoft Teams meeting\\nJoin on your computer: https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%22Tid%22%3a%22x%22%7d
END:VEVENT
END:VCALENDAR
"""


class ExtractLinks(unittest.TestCase):
    def test_meet_code(self):
        links = ics_feed.extract_links("https://meet.google.com/abc-defg-hij")
        self.assertEqual(links, [{"provider": "meet", "url": "https://meet.google.com/abc-defg-hij", "code": "abc-defg-hij"}])

    def test_zoom_id_strips_query(self):
        links = ics_feed.extract_links("Join: https://us02web.zoom.us/j/81234567890?pwd=abcDEF now")
        self.assertEqual(links[0]["provider"], "zoom")
        self.assertEqual(links[0]["code"], "81234567890")
        self.assertTrue(links[0]["url"].startswith("https://us02web.zoom.us/j/81234567890"))

    def test_dedup_and_html(self):
        links = ics_feed.extract_links('<a href="https://meet.google.com/abc-defg-hij">x</a>', "https://meet.google.com/abc-defg-hij")
        self.assertEqual(len(links), 1)

    def test_generic_link_fallback(self):
        links = ics_feed.extract_links("Dial in at https://example.com/room/5")
        self.assertEqual(links[0]["provider"], "link")

    def test_teams(self):
        links = ics_feed.extract_links("https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=x")
        self.assertEqual(links[0]["provider"], "teams")


class Expansion(unittest.TestCase):
    def setUp(self):
        ws = dt.datetime(2026, 9, 16, 0, 0, tzinfo=NY)
        we = ws + dt.timedelta(days=3)
        self.instances, self.cal = ics_feed.expand_events(FEED, ws, we, local_tz=NY, source="test")
        self.by_title = {}
        for i in self.instances:
            self.by_title.setdefault(i["title"], []).append(i)

    def test_calendar_name(self):
        self.assertEqual(self.cal.get("X-WR-CALNAME"), "sk2@fused.io")

    def test_recurring_expanded_with_exdate_and_override(self):
        standups = self.by_title.get("Daily standup", [])
        # window Sep 16-18: 16th yes, 17th EXDATE, 18th moved (override)
        self.assertEqual([s["start"] for s in standups], ["2026-09-16T14:00:00Z"])
        moved = self.by_title["Daily standup (moved)"][0]
        self.assertEqual(moved["start"], "2026-09-18T15:30:00Z")
        self.assertEqual(moved["key"], "standup@google.com::2026-09-18T14:00:00Z")
        self.assertTrue(moved["recurring"])
        self.assertEqual(standups[0]["links"][0]["code"], "abc-defg-hij")

    def test_attendee_partstat(self):
        design = self.by_title["Design review"][0]
        self.assertEqual(design["attendees"][0], {"email": "sk2@fused.io", "partstat": "DECLINED", "name": ""})
        self.assertEqual(design["links"][0]["provider"], "zoom")

    def test_skips_allday_and_cancelled(self):
        self.assertNotIn("Company holiday", self.by_title)
        self.assertNotIn("Cancelled thing", self.by_title)

    def test_teams_description_link(self):
        teams = self.by_title["Teams sync"][0]
        self.assertEqual(teams["links"][0]["provider"], "teams")
        self.assertEqual(teams["end"], "2026-09-16T21:30:00Z")

    def test_sorted(self):
        starts = [i["start"] for i in self.instances]
        self.assertEqual(starts, sorted(starts))


class Helpers(unittest.TestCase):
    def test_unfold(self):
        self.assertEqual(ics_feed.unfold("A:1\r\n b\r\nB:2"), ["A:1b", "B:2"])

    def test_parse_property_params(self):
        name, params, value = ics_feed.parse_property('DTSTART;TZID="America/New_York";VALUE=DATE-TIME:20260916T100000')
        self.assertEqual((name, params["TZID"], value), ("DTSTART", "America/New_York", "20260916T100000"))

    def test_duration(self):
        self.assertEqual(ics_feed.parse_duration("PT1H30M"), dt.timedelta(hours=1, minutes=30))

    def test_fix_until_naive(self):
        start = dt.datetime(2026, 9, 1, 10, 0, tzinfo=NY)
        self.assertEqual(ics_feed._fix_until("FREQ=DAILY;UNTIL=20260930", start), "FREQ=DAILY;UNTIL=20261001T035959Z")

    def test_normalize_webcal(self):
        self.assertEqual(ics_feed.normalize_url(" webcal://x.y/z.ics "), "https://x.y/z.ics")


if __name__ == "__main__":
    unittest.main()
