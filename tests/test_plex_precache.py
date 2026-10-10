import os
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import plex_precache as pp  # noqa: E402

SESSIONS = """<MediaContainer size="2">
  <Video ratingKey="102" type="episode" grandparentRatingKey="100" grandparentTitle="Show"
         title="Ep 2" viewOffset="600000" duration="2400000">
    <Media><Part file="/data/tv/Show/S01E02.mkv" size="4000000000"/></Media>
    <Player state="playing"/>
  </Video>
  <Video ratingKey="7" type="movie" title="Film" viewOffset="0" duration="6000000">
    <Media><Part file="/data/movies/Film.mkv" size="20000000000"/></Media>
  </Video>
</MediaContainer>"""

LEAVES = """<MediaContainer>
  <Video ratingKey="101"><Media><Part file="/data/tv/Show/S01E01.mkv"/></Media></Video>
  <Video ratingKey="102"><Media><Part file="/data/tv/Show/S01E02.mkv"/></Media></Video>
  <Video ratingKey="103"><Media><Part file="/data/tv/Show/S01E03.mkv"/></Media></Video>
  <Video ratingKey="104"><Media><Part file="/data/tv/Show/S01E04.mkv"/></Media></Video>
  <Video ratingKey="105"><Media><Part file="/data/tv/Show/S01E05.mkv"/></Media></Video>
</MediaContainer>"""


class ParseTests(unittest.TestCase):
    def test_sessions(self):
        s = pp.parse_sessions(ET.fromstring(SESSIONS))
        self.assertEqual(len(s), 2)
        self.assertEqual(s[0]["file"], "/data/tv/Show/S01E02.mkv")
        self.assertEqual(s[0]["show_key"], "100")
        self.assertEqual(s[0]["state"], "playing")
        self.assertEqual(s[1]["type"], "movie")
        self.assertIsNone(s[1]["state"])

    def test_next_episodes(self):
        files = pp.next_episode_files(ET.fromstring(LEAVES), "102", 2)
        self.assertEqual(files, ["/data/tv/Show/S01E03.mkv", "/data/tv/Show/S01E04.mkv"])
        self.assertEqual(pp.next_episode_files(ET.fromstring(LEAVES), "105", 2), [])
        self.assertEqual(pp.next_episode_files(ET.fromstring(LEAVES), "999", 2), [])

    def test_byte_offset(self):
        self.assertEqual(pp.byte_offset(600000, 2400000, 4000), 1000)
        self.assertEqual(pp.byte_offset(0, 0, 4000), 0)
        self.assertEqual(pp.byte_offset(9999999, 100, 4000), 4000)


class ConfigTests(unittest.TestCase):
    def test_path_map_longest_prefix(self):
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
            f.write("[plex]\nplex_token = abc\n[path_map]\n/data = /mnt/a\n/data/TV = /mnt/tv\n")
        try:
            cfg = pp.Config(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(cfg.plex_token, "abc")
        self.assertEqual(cfg.local_path("/data/TV/x.mkv"), "/mnt/tv/x.mkv")
        self.assertEqual(cfg.local_path("/data/movies/y.mkv"), "/mnt/a/movies/y.mkv")
        self.assertEqual(cfg.local_path("/other/z.mkv"), "/other/z.mkv")


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "f.bin")
        with open(self.path, "wb") as f:
            f.write(os.urandom(10_000))

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_from_offset_to_end(self):
        job = pp.Job(self.path, 2_500, None, 0)
        self.assertEqual(pp.read_through(job, 1_000, threading.Event()), 7_500)

    def test_read_head_only(self):
        job = pp.Job(self.path, 0, 3_000, 0)
        self.assertEqual(pp.read_through(job, 1_024, threading.Event()), 3_000)

    def test_warm_directory_and_dedup(self):
        cfg = pp.Config(None)
        pf = pp.Prefetcher(cfg)
        pf.submit(self.path)
        pf.submit(self.path, start=5_000)  # same file: moves pointer, no second job
        self.assertEqual(len(pf.queue), 1)
        self.assertEqual(pf.queue[0][2].pos, 5_000)
        pp.warm(cfg, [self.tmp.name])  # completes without hanging
        pf.submit(os.path.join(self.tmp.name, "missing.mkv"))
        self.assertEqual(len(pf.queue), 1)


if __name__ == "__main__":
    unittest.main()
