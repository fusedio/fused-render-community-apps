import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signals  # noqa: E402

PMSET = """2026-09-16 11:14:49 -0400
Assertion status system-wide:
   PreventUserIdleDisplaySleep    1
Listed by owning process:
   pid 647(Slack): [0x000026b200019599] 00:07:15 NoIdleSleepAssertion named: "WebRTC has active PeerConnections"
   pid 647(Slack): [0x00002836000595fd] 00:00:48 NoDisplaySleepAssertion named: "Video Wake Lock"
   pid 635(Google Chrome): [0x00002836000595ff] 00:00:48 NoIdleSleepAssertion named: "WebRTC has active PeerConnections"
   pid 426(coreaudiod): [0x000026c500018c2f] 00:06:57 PreventUserIdleSystemSleep named: "com.apple.audio.BuiltInMicrophoneDevice.context.preventuseridlesleep"
\tCreated for PID: 1379.
\tResources: audio-in BuiltInMicrophoneDevice
   pid 426(coreaudiod): [0x000026ae00018d0d] 00:07:19 PreventUserIdleSystemSleep named: "com.apple.audio.BuiltInSpeakerDevice.context.preventuseridlesleep"
\tCreated for PID: 1379.
\tResources: audio-out BuiltInSpeakerDevice
   pid 312(powerd): [0x000024f900019379] 00:14:36 PreventUserIdleSystemSleep named: "Powerd - Prevent sleep while display is on"
Kernel Assertions: 0x24=USB,THNDR
"""

PROCS = {
    647: {"ppid": 1, "comm": "/Applications/Slack.app/Contents/MacOS/Slack", "app": "Slack", "name": "Slack", "main": True},
    1379: {"ppid": 647, "comm": "/Applications/Slack.app/Contents/Frameworks/Slack Helper.app/Contents/MacOS/Slack Helper", "app": "Slack", "name": "Slack Helper", "main": False},
    635: {"ppid": 1, "comm": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "app": "Google Chrome", "name": "Google Chrome", "main": True},
    900: {"ppid": 635, "comm": "/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Versions/1/Helpers/Google Chrome Helper.app/Contents/MacOS/Google Chrome Helper", "app": "Google Chrome", "name": "Google Chrome Helper", "main": False},
    2000: {"ppid": 1, "comm": "/Applications/zoom.us.app/Contents/Frameworks/CptHost.app/Contents/MacOS/CptHost", "app": "zoom.us", "name": "CptHost", "main": False},
}


class Processes(unittest.TestCase):
    def test_app_from_path(self):
        self.assertEqual(signals.app_from_path(PROCS[900]["comm"]), "Google Chrome")
        self.assertEqual(signals.app_from_path(PROCS[2000]["comm"]), "zoom.us")
        self.assertEqual(signals.app_from_path("/usr/libexec/searchpartyd"), "searchpartyd")

    def test_main_binary(self):
        self.assertTrue(signals.is_main_binary("/Applications/Firefox.app/Contents/MacOS/firefox"))
        self.assertTrue(signals.is_main_binary("/Applications/Microsoft Teams.app/Contents/MacOS/MSTeams"))
        self.assertFalse(signals.is_main_binary(PROCS[900]["comm"]))
        self.assertFalse(signals.is_main_binary("/System/Library/PrivateFrameworks/SafariSafeBrowsing.framework/Versions/A/com.apple.Safari.SafeBrowsing.Service"))

    def test_running_apps_excludes_helpers(self):
        procs = {900: PROCS[900], 2000: PROCS[2000]}
        self.assertEqual(signals.running_apps(procs), set())
        self.assertTrue(signals.process_running(procs, "CptHost"))


class Assertions(unittest.TestCase):
    def test_parse(self):
        recs = signals.parse_assertions(PMSET)
        self.assertEqual(len(recs), 6)
        mic = [r for r in recs if "audio-in" in r["resources"]][0]
        self.assertEqual((mic["created_for"], mic["proc"]), (1379, "coreaudiod"))

    def test_summary_attributes_to_apps(self):
        summary = signals.summarize_assertions(signals.parse_assertions(PMSET), PROCS)
        self.assertEqual(summary["webrtc_apps"], ["Google Chrome", "Slack"])
        self.assertEqual(summary["mic_apps"], {"Slack": ["BuiltInMicrophoneDevice"]})
        self.assertEqual(summary["audio_out_apps"], ["Slack"])
        self.assertIn("Slack", summary["display_lock_apps"])


def _pickle_string(s: bytes) -> bytes:
    n = len(s)
    pad = (4 - n % 4) % 4
    return struct.pack("<I", n) + s + b"\0" * pad


def _pickle_string16(s: str) -> bytes:
    b = s.encode("utf-16-le")
    pad = (4 - len(b) % 4) % 4
    return struct.pack("<I", len(s)) + b + b"\0" * pad


def _cmd(cid: int, payload: bytes) -> bytes:
    return struct.pack("<H", len(payload) + 1) + bytes([cid]) + payload


def _nav(tab_id: int, idx: int, url: str, title: str) -> bytes:
    body = struct.pack("<II", tab_id, idx) + _pickle_string(url.encode()) + _pickle_string16(title)
    return _cmd(6, struct.pack("<I", len(body)) + body)


class Snss(unittest.TestCase):
    def test_parse_open_tabs_only(self):
        data = b"SNSS" + struct.pack("<I", 3)
        data += _cmd(0, struct.pack("<II", 1, 10))  # tab 10 in window 1
        data += _nav(10, 0, "https://example.com/", "Example")
        data += _nav(10, 1, "https://meet.google.com/abc-defg-hij?authuser=0", "Meet")
        data += _cmd(7, struct.pack("<II", 10, 1))  # current = index 1
        data += _cmd(0, struct.pack("<II", 1, 11))
        data += _nav(11, 0, "https://zoom.us/j/123", "closed later")
        data += _cmd(16, struct.pack("<I", 11))  # tab 11 closed
        data += _cmd(0, struct.pack("<II", 2, 12))
        data += _nav(12, 0, "https://teams.microsoft.com/l/meetup-join/x", "closed window")
        data += _cmd(17, struct.pack("<I", 2))  # window 2 closed
        tabs = signals.parse_snss(data)
        self.assertEqual(tabs, [{"url": "https://meet.google.com/abc-defg-hij?authuser=0", "title": "Meet"}])

    def test_rejects_other_files(self):
        self.assertEqual(signals.parse_snss(b"nope"), [])


class Lz4(unittest.TestCase):
    def test_literals_only(self):
        self.assertEqual(signals.lz4_block_decompress(b"\x50hello"), b"hello")

    def test_with_match(self):
        # 4 literals "abcd", then a match of length 4 at offset 4 -> "abcdabcd", then literal "!"
        block = b"\x40abcd\x04\x00" + b"\x10!"
        self.assertEqual(signals.lz4_block_decompress(block), b"abcdabcd!")


class MeetingTabs(unittest.TestCase):
    def test_only_meeting_urls_kept(self):
        tabs = signals.meeting_tabs_from_urls("Google Chrome", ["https://news.site/x", "https://meet.google.com/abc-defg-hij", "https://us02web.zoom.us/wc/81234567890/join?x=1"], "session")
        self.assertEqual([(t["provider"], t["code"]) for t in tabs], [("meet", "abc-defg-hij"), ("zoom", "81234567890")])


class Presence(unittest.TestCase):
    MEET = [{"provider": "meet", "url": "https://meet.google.com/abc-defg-hij", "code": "abc-defg-hij"}]
    ZOOM = [{"provider": "zoom", "url": "https://zoom.us/j/555", "code": "555"}]

    def sig(self, **kw):
        base = {"calls": [], "tabs": [], "zoom_in_meeting": False}
        base.update(kw)
        base.setdefault("webrtc_apps", [c["app"] for c in base["calls"] if "webrtc" in c["signals"]])
        return base

    def test_absent(self):
        p = signals.presence_for(self.MEET, self.sig())
        self.assertEqual(p["state"], "absent")

    def test_lobby_when_tab_without_media(self):
        p = signals.presence_for(self.MEET, self.sig(tabs=[{"app": "Google Chrome", "provider": "meet", "code": "abc-defg-hij", "url": "", "source": "session"}]))
        self.assertEqual((p["state"], p["app"]), ("lobby", "Google Chrome"))

    def test_in_call_when_tab_and_media(self):
        p = signals.presence_for(self.MEET, self.sig(
            tabs=[{"app": "Google Chrome", "provider": "meet", "code": "abc-defg-hij", "url": "", "source": "session"}],
            calls=[{"app": "Google Chrome", "signals": ["webrtc"], "provider": "browser"}, {"app": "Slack", "signals": ["mic"], "provider": "slack"}]))
        self.assertEqual((p["state"], p["confidence"]), ("in_call", "high"))
        self.assertEqual(p["other_calls"], ["Slack"])

    def test_mic_only_in_chromium_is_lobby(self):
        p = signals.presence_for(self.MEET, self.sig(
            tabs=[{"app": "Google Chrome", "provider": "meet", "code": "abc-defg-hij", "url": "", "source": "session"}],
            calls=[{"app": "Google Chrome", "signals": ["mic"], "provider": "browser"}]))
        self.assertEqual(p["state"], "lobby")
        self.assertIn("pre-join", p["via"])

    def test_mic_only_in_firefox_counts(self):
        p = signals.presence_for(self.MEET, self.sig(
            tabs=[{"app": "Firefox", "provider": "meet", "code": "abc-defg-hij", "url": "", "source": "session"}],
            calls=[{"app": "Firefox", "signals": ["mic"], "provider": "browser"}]))
        self.assertEqual(p["state"], "in_call")

    def test_other_meet_code_does_not_count(self):
        p = signals.presence_for(self.MEET, self.sig(
            tabs=[{"app": "Google Chrome", "provider": "meet", "code": "zzz-zzzz-zzz", "url": "", "source": "session"}],
            calls=[{"app": "Google Chrome", "signals": ["webrtc"], "provider": "browser"}]))
        self.assertEqual(p["state"], "absent")
        self.assertEqual(p["other_calls"], ["Google Chrome"])

    def test_zoom_cpthost(self):
        p = signals.presence_for(self.ZOOM, self.sig(zoom_in_meeting=True, calls=[{"app": "zoom.us", "signals": ["zoom-meeting"], "provider": "zoom"}]))
        self.assertEqual((p["state"], p["confidence"]), ("in_call", "high"))

    def test_teams_native_medium(self):
        links = [{"provider": "teams", "url": "https://teams.microsoft.com/l/meetup-join/x", "code": "x"}]
        p = signals.presence_for(links, self.sig(calls=[{"app": "Microsoft Teams", "signals": ["mic", "webrtc"], "provider": "teams"}]))
        self.assertEqual((p["state"], p["confidence"]), ("in_call", "medium"))

    def test_no_link_low_confidence(self):
        p = signals.presence_for([], self.sig(calls=[{"app": "Slack", "signals": ["mic"], "provider": "slack"}]))
        self.assertEqual((p["state"], p["confidence"]), ("in_call", "low"))


if __name__ == "__main__":
    unittest.main()
