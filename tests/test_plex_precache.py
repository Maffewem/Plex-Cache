import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import plex_precache as pp  # noqa: E402

# Tests read local temp files, which are not a caching mount.
pp.DEFAULTS["require_cache_mount"] = "false"

SESSIONS = """<MediaContainer size="2">
  <Video ratingKey="102" type="episode" grandparentRatingKey="100" grandparentTitle="Show"
         title="Ep 2" viewOffset="600000" duration="2400000">
    <Media bitrate="13333"><Part file="/data/tv/Show/S01E02.mkv" size="4000000000"/></Media>
    <Player state="playing"/>
    <Session id="abc" bandwidth="14000" location="lan"/>
  </Video>
  <Video ratingKey="7" type="movie" title="Film" viewOffset="0" duration="6000000">
    <Media><Part file="/data/movies/Film.mkv" size="20000000000"/></Media>
    <TranscodeSession speed="1.5" videoDecision="transcode" throttled="0"/>
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

    def test_plex_delivery_rate(self):
        direct, trans = pp.parse_sessions(ET.fromstring(SESSIONS))
        self.assertEqual((direct["decision"], direct["send_kbps"]), ("direct play", 14000))
        self.assertEqual(pp.plex_rate_text(direct, 1_666_000),
                         "Plex: direct play, reading about 14 Mbps from the file, "
                         "sending 14 Mbps to the player.")
        self.assertEqual((trans["decision"], trans["transcode_speed"]), ("transcode", 1.5))
        # A 1.5x transcode of a 2 MB/s (16 Mbps) file reads 24 Mbps.
        self.assertEqual(pp.plex_rate_text(trans, 2_000_000),
                         "Plex: transcode at 1.5x, reading about 24 Mbps from the file.")

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

    def test_speculative_reads_respect_min_free(self):
        cfg = pp.Config(None)
        cfg.cache_dir = self.tmp.name
        free = pp.shutil.disk_usage(self.tmp.name).free
        cfg.min_free = free  # any read would dip below the floor
        pf = pp.Prefetcher(cfg)
        pf.submit(self.path, prio=pp.PRIO_NEXT)
        self.assertEqual(len(pf.queue), 0)
        pf.submit(self.path, prio=pp.PRIO_PLAYING)  # playing file is never skipped
        self.assertEqual(len(pf.queue), 1)
        cfg.min_free = 0
        self.assertTrue(pf.has_room(self.path, 0, None))


class StatusTests(unittest.TestCase):
    def test_status_page_renders_and_escapes(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = pp.Config(None)
            cfg.cache_dir = d
            cfg.mount_env = os.path.join(d, "env")
            with open(cfg.mount_env, "w") as f:
                f.write("CACHE_MAX_SIZE=16G\n# comment\nREMOTE=box:media\n")
            path = os.path.join(d, "<i>ep.mkv")
            with open(path, "wb") as f:
                f.write(b"x" * 1000)
            pf = pp.Prefetcher(cfg)
            pf.submit(path, start=500, prio=pp.PRIO_NEXT)
            pf.sessions = [{"title": "Show & Co", "state": "playing", "file": path}]
            data = pp.status_data(cfg, pf)
            self.assertEqual(data["cache"]["mount"]["CACHE_MAX_SIZE"], "16G")
            self.assertEqual(data["jobs"][0]["kind"], "next episode")
            self.assertEqual(data["jobs"][0]["state"], "queued")
            page = pp.render_status(data)
            self.assertIn("&lt;i&gt;ep.mkv", page)
            self.assertIn("Show &amp; Co", page)
            self.assertIn("of 16G", page)
            self.assertIn("<link rel=icon href=\"data:image/svg+xml,", page)
            self.assertIn("id=theme", page)
            self.assertIn("data-build=", page)


def _stream(**kw):
    s = {"title": "Ep", "file": "/m/e.mkv", "path": "/m/e.mkv", "state": "playing",
         "size": 3600 * 1_000_000, "duration": 3600 * 1000,  # 1 MB/s video
         "view_offset": 600 * 1000, "seen_at": 1000.0}
    s.update(kw)
    return s


def _data(jobs=(), done=None, warned=()):
    return {"jobs": list(jobs), "done": done or {}, "warned": list(warned)}


def _job(pos, rate, state="reading"):
    return {"path": "/m/e.mkv", "pos": pos, "rate": rate, "state": state}


class HealthTests(unittest.TestCase):
    def health(self, s, d):
        return pp.stream_health(s, d, now=1000.0)

    def test_fully_cached_is_good(self):
        h = self.health(_stream(), _data(done={"/m/e.mkv": 1}))
        self.assertEqual(h["level"], pp.GOOD)
        self.assertAlmostEqual(h["ahead_s"], 3000)

    def test_well_ahead_and_fast_is_good(self):
        h = self.health(_stream(), _data([_job(1200 * 1_000_000, 3_000_000)]))
        self.assertEqual(h["level"], pp.GOOD)
        self.assertIn("10 min cached ahead", h["detail"])

    def test_slow_link_is_bad_with_time_left(self):
        h = self.health(_stream(), _data([_job(900 * 1_000_000, 500_000)]))
        self.assertEqual(h["level"], pp.BAD)
        self.assertIn("runs out in about 10 min", h["detail"])

    def test_overtaken_is_bad(self):
        h = self.health(_stream(), _data([_job(100 * 1_000_000, 3_000_000)]))
        self.assertEqual(h["level"], pp.BAD)

    def test_marginal_link_and_short_lead_are_warn(self):
        self.assertEqual(self.health(_stream(), _data([_job(900 * 1_000_000, 1_200_000)]))["level"],
                         pp.WARN)
        self.assertEqual(self.health(_stream(), _data([_job(610 * 1_000_000, 3_000_000)]))["level"],
                         pp.WARN)
        self.assertEqual(self.health(_stream(), _data([_job(0, 0, "queued")]))["level"], pp.WARN)

    def test_buffering_and_missing_file_are_bad(self):
        h = self.health(_stream(state="buffering"), _data(done={"/m/e.mkv": 1}))
        self.assertEqual(h["level"], pp.BAD)
        self.assertEqual(self.health(_stream(), _data(warned=["/m/e.mkv"]))["level"], pp.BAD)

    def test_system_checks(self):
        gb = 1024 ** 3
        d = {"poll_error": None, "last_poll": pp.time.time(), "poll_interval": 10,
             "sessions": [{}], "jobs": [], "recent": [],
             "streams": [{"bitrate": 2_500_000}],
             "mem_available": 300 * 1024 ** 2,
             "cache": {"disk": {"free": 5 * gb}, "min_free": 10 * gb, "used": 16 * gb,
                       "mount": {"CACHE_MAX_SIZE": "16G", "BUFFER_SIZE": "128M"}}}
        d["recent"] = [{"mb": 600, "secs": 600, "path": "x", "at": 0}]  # 1 MB/s
        levels = {c["title"]: c["level"] for c in pp.system_checks(d)}
        self.assertEqual(levels, {"Plex": pp.GOOD, "Disk space": pp.BAD,
                                  "Cache size": pp.WARN, "Memory": pp.BAD,
                                  "Link speed": pp.WARN})

    def test_parse_size(self):
        self.assertEqual(pp.parse_size("16G"), 16 * 1024 ** 3)
        self.assertEqual(pp.parse_size("128M"), 128 * 1024 ** 2)
        self.assertIsNone(pp.parse_size("lots"))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ini = os.path.join(self.tmp.name, "config.ini")
        self.env = os.path.join(self.tmp.name, "env")
        with open(self.ini, "w") as f:
            f.write("[plex]\n# keep me\nnext_episodes = 1\n[path_map]\n/a = /b\n")
        with open(self.env, "w") as f:
            f.write("# comment\nCACHE_MAX_SIZE=16G\nCACHE_PERCENT=60\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_ini_keeps_comments_and_sections(self):
        pp.update_ini(self.ini, {"next_episodes": "3", "workers": "2"})
        text = open(self.ini).read()
        self.assertIn("# keep me", text)
        self.assertIn("next_episodes = 3", text)
        cfg = pp.Config(self.ini)
        self.assertEqual((cfg.next_episodes, cfg.workers), (3, 2))
        self.assertEqual(cfg.local_path("/a/x"), "/b/x")

    def test_update_env_sets_and_removes(self):
        pp.update_env(self.env, {"CACHE_MAX_SIZE": "20G", "CACHE_PERCENT": "", "READ_AHEAD": "1G"})
        self.assertEqual(open(self.env).read(),
                         "# comment\nCACHE_MAX_SIZE=20G\nREAD_AHEAD=1G\n")

    def test_validation_rejects_bad_and_injected_values(self):
        good = {"next_episodes": "2", "next_episode_head_mb": "0", "on_deck_limit": "5",
                "on_deck_head_mb": "512", "workers": "2", "poll_interval": "10",
                "remember_hours": "12", "stop_after_minutes": "10", "taper_mb_s": "5"}
        self.assertEqual(pp.validate_prefetch(good)["on_deck"], "false")
        with self.assertRaises(pp.SettingsError):
            pp.validate_prefetch(dict(good, workers="0"))
        mount = {"CACHE_MAX_SIZE": "20g", "CACHE_PERCENT": "", "CACHE_MIN_FREE": "10G",
                 "CACHE_MAX_AGE": "72h", "READ_AHEAD": "1G", "BUFFER_SIZE": "128M"}
        self.assertEqual(pp.validate_mount(mount)["CACHE_MAX_SIZE"], "20G")
        for bad in ({"CACHE_MAX_SIZE": "20G\nEVIL=1"}, {"CACHE_MAX_AGE": "3 days"},
                    {"CACHE_PERCENT": "150"}, {"CACHE_MAX_SIZE": ""}):
            with self.assertRaises(pp.SettingsError):
                pp.validate_mount(dict(mount, **bad))
        self.assertEqual(pp.validate_mount(dict(mount, CACHE_MAX_SIZE="", CACHE_PERCENT="50"))
                         ["CACHE_PERCENT"], "50")

    def test_worker_count_changes_live(self):
        cfg = pp.Config(None)
        cfg.workers = 3
        pf = pp.Prefetcher(cfg)
        pf.ensure_workers()
        self.assertEqual(pf.n_workers, 3)
        cfg.workers = 1
        pf.ensure_workers()
        for _ in range(40):
            if pf.n_workers == 1:
                break
            pp.time.sleep(0.05)
        self.assertEqual(pf.n_workers, 1)
        pf.shutdown()

    def test_warm_path_stays_inside_mount(self):
        media = os.path.join(self.tmp.name, "media", "Show")
        os.makedirs(media)
        open(os.path.join(media, "e1.mkv"), "wb").close()
        with open(self.env, "a") as f:
            f.write(f"MOUNTPOINT={os.path.dirname(media)}\n")
        cfg = pp.Config(None)
        cfg.mount_env, cfg.cache_dir = self.env, ""
        pf = pp.Prefetcher(cfg)
        self.assertIn("Queued 1", pp.warm_path(cfg, pf, "Show"))
        for bad in ("../", "/../../etc", "Show/../../.."):
            with self.assertRaises(pp.SettingsError):
                pp.warm_path(cfg, pf, bad)


MOUNTS = """/dev/sda1 / ext4 rw,relatime 0 0
user@1.2.3.4:/home/user/data /data fuse.sshfs rw,nosuid,user_id=0 0 0
box:media /mnt/remote fuse.rclone rw,nosuid,user_id=1000 0 0
srv:/x /mnt/my\\040nfs nfs4 rw,fsc,vers=4.2 0 0
srv:/y /mnt/nfsplain nfs4 rw,vers=4.2 0 0
"""


class MountTests(unittest.TestCase):
    def test_mount_detection(self):
        self.assertEqual(pp.mount_info("/data/Movies/x.mkv", MOUNTS)[:2], ("/data", "fuse.sshfs"))
        self.assertEqual(pp.mount_info("/database/x", MOUNTS)[:2], ("/", "ext4"))
        self.assertEqual(pp.mount_info("/mnt/my nfs/a.mkv", MOUNTS)[:2], ("/mnt/my nfs", "nfs4"))
        cached = {p: pp.caches_to_disk(*pp.mount_info(p, MOUNTS)[1:])
                  for p in ("/data/a", "/mnt/remote/a", "/mnt/my nfs/a", "/mnt/nfsplain/a", "/x")}
        self.assertEqual(cached, {"/data/a": False, "/mnt/remote/a": True, "/mnt/my nfs/a": True,
                                  "/mnt/nfsplain/a": False, "/x": False})

    def test_uncached_mount_blocks_prefetch_and_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.mkv")
            open(path, "wb").close()
            cfg = pp.Config(None)
            cfg.require_cache_mount = True
            pf = pp.Prefetcher(cfg)
            pf.submit(path)  # a temp dir is never an rclone mount
            self.assertEqual(len(pf.queue), 0)
            self.assertEqual(len(pf.blocked), 1)
        s = {"file": "/data/a.mkv", "path": "/data/a.mkv", "state": "playing"}
        d2 = {"jobs": [], "done": {}, "warned": [], "blocked": {"/data": "fuse.sshfs"}}
        h = pp.stream_health(s, d2)
        self.assertEqual(h["level"], pp.BAD)
        self.assertIn("can't cache", h["detail"])
        self.assertEqual(pp.stream_health(s, dict(d2, blocked={"/": "ext4"}))["level"], pp.GOOD)
        d2.update(poll_error=None, last_poll=0, sessions=[], recent=[], streams=[],
                  cache={"mount": {}})
        titles = [c["title"] for c in pp.system_checks(d2)]
        self.assertIn("Media mount", titles)


class StopAbandonedTests(unittest.TestCase):
    def test_closed_stream_stops_its_downloads_but_paused_keeps_them(self):
        with tempfile.TemporaryDirectory() as d:
            files = {}
            for name in ("ep1", "ep2", "film", "manual"):
                files[name] = os.path.join(d, name)
                with open(files[name], "wb") as f:
                    f.write(b"x" * 100)
            cfg = pp.Config(None)
            cfg.stop_after = 600
            pf = pp.Prefetcher(cfg)
            pf.submit(files["ep1"], owner=files["ep1"])
            pf.submit(files["ep2"], prio=pp.PRIO_NEXT, owner=files["ep1"])
            pf.submit(files["film"], owner=files["film"])
            pf.submit(files["manual"], prio=pp.PRIO_MANUAL)
            now = pp.time.time()
            pf.last_seen = {files["ep1"]: now - 700, files["film"]: now - 60}
            pf.stop_abandoned()
            self.assertEqual(sorted(os.path.basename(p) for p in pf.active),
                             ["film", "manual"])
            self.assertTrue(all(j.cancelled for _, _, j in pf.queue
                                if j.owner == files["ep1"]))
            # A cancelled read stops at once and isn't remembered as cached.
            job = pp.Job(files["ep1"], 0, None, 0)
            job.cancelled = True
            self.assertEqual(pp.read_through(job, 10, threading.Event()), 0)
            self.assertNotIn(files["ep1"], pf.done)
            cfg.stop_after = 0  # 0 = never stop
            pf.last_seen[files["film"]] = now - 99999
            pf.stop_abandoned()
            self.assertIn(files["film"], pf.active)

    def test_played_episode_reads_whole_file_after_head(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "ep2")
            with open(ep, "wb") as f:
                f.write(b"x" * 100)
            pf = pp.Prefetcher(pp.Config(None))
            # Queued as the next episode (start only), then it starts playing.
            pf.submit(ep, end=10, prio=pp.PRIO_NEXT, owner="ep1")
            pf.submit(ep, start=0, owner=ep)
            job = pf.active[ep]
            self.assertIsNone(job.end)
            self.assertEqual((job.prio, job.owner), (pp.PRIO_PLAYING, ep))
            # Start already cached and remembered: playing it still reads it all.
            del pf.active[ep]
            pf.queue.clear()
            pf.heads[ep] = pp.time.time()
            pf.submit(ep, end=10, prio=pp.PRIO_NEXT)
            self.assertNotIn(ep, pf.active)
            pf.submit(ep, owner=ep)
            self.assertIsNone(pf.active[ep].end)

    def test_music_sessions_are_picked_up(self):
        root = pp.ET.fromstring(
            '<MediaContainer><Track ratingKey="7" type="track" title="Song" '
            'grandparentTitle="Band" duration="200000" viewOffset="1000">'
            '<Media><Part file="/m/Band/song.flac" size="30000000"/></Media>'
            '<Player state="playing"/></Track></MediaContainer>')
        s = pp.parse_sessions(root)
        self.assertEqual([(x["file"], x["type"], x["title"]) for x in s],
                         [("/m/Band/song.flac", "track", "Band")])

    def test_unwatched_downloads_taper_off(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "film")
            with open(path, "wb") as f:
                f.write(b"x" * 4 * pp.MB)
            cfg = pp.Config(None)
            cfg.stop_after, cfg.taper, cfg.poll_interval = 600, 10 * pp.MB, 10
            pf = pp.Prefetcher(cfg)
            pf.submit(path, owner=path)
            job = pf.active[path]
            now = pp.time.time()
            pf.last_seen = {path: now}  # still on screen (or paused): full speed
            pf.stop_abandoned()
            self.assertEqual(job.limit, 0)
            pf.last_seen = {path: now - 60}  # closed a minute ago: 90% of 10 MB/s
            pf.stop_abandoned()
            self.assertAlmostEqual(job.limit / pp.MB, 9.0, places=1)
            pf.last_seen = {path: now - 540}  # nearly at the stop: 1 MB/s
            pf.stop_abandoned()
            self.assertAlmostEqual(job.limit / pp.MB, 1.0, places=1)
            pf.last_seen = {path: now}  # back on screen: full speed again
            pf.stop_abandoned()
            self.assertEqual(job.limit, 0)
            # The cap really slows reads: 4 MB at 8 MB/s takes about half a second.
            job.limit = 8 * pp.MB
            t0 = pp.time.monotonic()
            self.assertEqual(pp.read_through(job, pp.MB, threading.Event()), 4 * pp.MB)
            self.assertGreater(pp.time.monotonic() - t0, 0.4)


class RecommendTests(unittest.TestCase):
    def test_small_box(self):
        gb = 1024 ** 3
        rec = pp.recommend_settings("/", mem=int(7.75 * gb), cpus=4,
                                    disk=(int(67.66 * gb), int(38.12 * gb)), cached=0)
        self.assertEqual(rec["mount"], {"CACHE_PERCENT": "60", "CACHE_MAX_SIZE": "18G",
                                        "CACHE_MIN_FREE": "7G", "CACHE_MAX_AGE": "72h",
                                        "READ_AHEAD": "1G", "BUFFER_SIZE": "128M"})
        self.assertEqual(rec["prefetch"], {"next_episodes": "1", "workers": "2",
                                           "min_free_gb": "7"})

    def test_big_and_tiny_boxes_stay_in_bounds(self):
        gb = 1024 ** 3
        big = pp.recommend_settings("/", mem=64 * gb, cpus=16, disk=(4000 * gb, 3000 * gb),
                                    cached=0)["mount"]
        self.assertEqual((big["CACHE_MIN_FREE"], big["BUFFER_SIZE"], big["READ_AHEAD"],
                          big["CACHE_MAX_AGE"]), ("20G", "512M", "4G", "14d"))
        tiny = pp.recommend_settings("/", mem=1 * gb, cpus=1, disk=(10 * gb, 3 * gb),
                                     cached=0)
        self.assertEqual(tiny["mount"]["CACHE_MAX_SIZE"], "5G")
        self.assertEqual(tiny["mount"]["BUFFER_SIZE"], "32M")
        self.assertEqual(tiny["prefetch"]["workers"], "1")

    def test_settings_page_has_recommendations_and_updates(self):
        with tempfile.TemporaryDirectory() as d:
            ini = os.path.join(d, "config.ini")
            with open(ini, "w") as f:
                f.write("[plex]\nworkers = 2\n")
            cfg = pp.Config(ini)
            cfg.cache_dir, cfg.mount_env = d, os.path.join(d, "missing.env")
            page = pp.render_settings(cfg, "tok", None, True)
            self.assertIn("Use recommended settings", page)
            self.assertIn("Recommended for this server", page)
            self.assertIn("action=update/run", page)
            self.assertIn("Automatic updates on", pp.set_auto_update(cfg, {"auto_update": "on"}))
            self.assertTrue(pp.Config(ini).auto_update)


class RcloneSettingsTests(unittest.TestCase):
    """The connection and advanced rclone settings on the status page."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.mnt = os.path.join(d, "data")
        os.mkdir(self.mnt)
        self.key = os.path.join(d, "ssh_key")
        with open(self.key, "w") as f:
            f.write("old key\n")
        self.conf = os.path.join(d, "rclone.conf")
        with open(self.conf, "w") as f:
            f.write(f"[plexcache-sftp]\ntype = sftp\nhost = old.example\nuser = me\n"
                    f"port = 22\nkey_file = {self.key}\nshell_type = unix\n")
        self.env = os.path.join(d, "env")
        with open(self.env, "w") as f:
            f.write(f"REMOTE=plexcache-sftp:/home/me/data\nMOUNTPOINT={self.mnt}\n"
                    f"RCLONE_CONFIG={self.conf}\nEXTRA_ARGS=\"--vfs-read-chunk-streams 4\"\n")
        self.ini = os.path.join(d, "config.ini")
        with open(self.ini, "w") as f:
            f.write("[plex]\nworkers = 2\n")
        self.cfg = pp.Config(self.ini)
        self.cfg.mount_env, self.cfg.cache_dir = self.env, d
        # A stand-in rclone that records its config and fails for host "bad.example".
        bindir = os.path.join(d, "bin")
        os.mkdir(bindir)
        self.seen = os.path.join(d, "seen.conf")
        with open(os.path.join(bindir, "rclone"), "w") as f:
            f.write(f"#!/bin/sh\nconf=$2\ncp \"$conf\" {self.seen}\n"
                    "grep -q 'bad.example' \"$conf\" && { echo '2026/10/10 10:00:00 ERROR : "
                    "dial tcp: lookup bad.example: no such host' >&2; exit 1; }\n"
                    "printf 'Movies/\\nTV/\\n'\n")
        os.chmod(os.path.join(bindir, "rclone"), 0o755)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = bindir + os.pathsep + self.old_path
        self.restarts = []
        self.real_restart = pp.restart_mount_or_revert
        pp.restart_mount_or_revert = lambda backups: self.restarts.append(backups) or "ok"

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        pp.restart_mount_or_revert = self.real_restart
        self.tmp.cleanup()

    def form(self, **kw):
        return {"host": "new.example", "user": "me", "port": "2222", "path": "/srv/media",
                "mountpoint": self.mnt, "key": "", "do": "save", **kw}

    def test_page_shows_connection_and_advanced_fields(self):
        page = pp.render_settings(self.cfg, "tok", None, True)
        self.assertIn("action=settings/connection", page)
        self.assertIn("value='old.example'", page)
        self.assertIn("value='--vfs-read-chunk-streams 4'", page)

    def test_test_only_changes_nothing(self):
        before = open(self.conf).read(), open(self.env).read()
        msg = pp.apply_connection(self.cfg, self.form(do="test"))
        self.assertIn("2 item(s) in /srv/media", msg)
        self.assertIn("host = new.example", open(self.seen).read())
        self.assertEqual((open(self.conf).read(), open(self.env).read()), before)
        self.assertEqual(self.restarts, [])

    def test_failed_test_refuses_to_save(self):
        before = open(self.conf).read()
        with self.assertRaisesRegex(pp.SettingsError, "no such host.*Nothing was changed"):
            pp.apply_connection(self.cfg, self.form(host="bad.example"))
        self.assertEqual(open(self.conf).read(), before)

    def test_save_writes_conf_env_and_key_then_restarts(self):
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\r\nabc\r\n-----END OPENSSH PRIVATE KEY-----"
        with mock.patch.object(pp, "CONF_DIR", self.tmp.name):
            msg = pp.apply_connection(self.cfg, self.form(key=key))
        self.assertIn("Saved and the mount restarted", msg)
        conf = open(self.conf).read()
        self.assertIn("host = new.example", conf)
        self.assertIn("port = 2222", conf)
        self.assertIn(f"key_file = {self.key}", conf)
        self.assertIn("REMOTE=plexcache-sftp:/srv/media", open(self.env).read())
        self.assertEqual(open(self.key).read(), key.replace("\r\n", "\n") + "\n")
        # The restart got the old files to put back if the mount won't start.
        self.assertEqual(self.restarts[0][self.key], "old key\n")
        self.assertIn("old.example", self.restarts[0][self.conf])

    def test_rejects_unsafe_values(self):
        for bad in ({"host": "a.example; rm -rf /"}, {"user": "me me"}, {"port": "0"},
                    {"path": "relative"}, {"path": "/x/$(id)"}, {"path": "/x\nEVIL=1"},
                    {"mountpoint": "/does/not/exist"}, {"key": "not a key"}):
            with self.assertRaises(pp.SettingsError, msg=str(bad)):
                pp.validate_connection(self.form(**bad), pp.sftp_settings(self.cfg))
        with open(os.path.join(self.mnt, "file"), "w"):
            pass
        other = os.path.join(self.tmp.name, "other")
        os.mkdir(other)
        pp.validate_connection(self.form(mountpoint=other), pp.sftp_settings(self.cfg))
        with self.assertRaisesRegex(pp.SettingsError, "isn't empty"):
            pp.check_mountpoint(self.mnt, other)

    def test_unmanaged_remote_is_read_only(self):
        pp.update_env(self.env, {"REMOTE": "gdrive:media"})
        self.assertIsNone(pp.sftp_settings(self.cfg))
        self.assertIn("set up outside Plex-Cache", pp.render_settings(self.cfg, "t", None, True))
        with self.assertRaises(pp.SettingsError):
            pp.apply_connection(self.cfg, self.form())

    def test_extra_flags(self):
        self.assertEqual(pp.validate_flags("  --bwlimit 80M   --vfs-read-chunk-streams=4 "),
                         "--bwlimit 80M --vfs-read-chunk-streams=4")
        self.assertEqual(pp.validate_flags(""), "")
        for bad in ("--rc", "--rc-addr :5572", "--config /tmp/x", "--bwlimit $(id)",
                    "80M --bwlimit", "--x;reboot", "--x\nEVIL=1"):
            with self.assertRaises(pp.SettingsError, msg=bad):
                pp.validate_flags(bad)

    def test_env_values_with_spaces_are_quoted_for_bash(self):
        pp.update_env(self.env, {"EXTRA_ARGS": "--bwlimit 80M", "DIR_CACHE_TIME": "1h"})
        text = open(self.env).read()
        self.assertIn('EXTRA_ARGS="--bwlimit 80M"', text)
        self.assertEqual(pp.read_env_file(self.env)["EXTRA_ARGS"], "--bwlimit 80M")
        out = subprocess.run(["bash", "-ec", f'. "{self.env}"; echo "$EXTRA_ARGS"'],
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "--bwlimit 80M")

    def test_mount_form_keeps_advanced_values_and_defaults_blanks(self):
        mount = {"CACHE_MAX_SIZE": "20G", "CACHE_PERCENT": "", "CACHE_MIN_FREE": "10G",
                 "CACHE_MAX_AGE": "72h", "READ_AHEAD": "1G", "BUFFER_SIZE": "128M"}
        out = pp.validate_mount(mount)
        self.assertEqual((out["VFS_CHUNK_SIZE"], out["DIR_CACHE_TIME"], out["EXTRA_ARGS"]),
                         ("64M", "1h", ""))


    def test_media_mount_offers_the_right_switch(self):
        env = {"MOUNTPOINT": self.mnt}
        pp.update_env(self.env, env)
        sshfs = f"u@h:/data {self.mnt} fuse.sshfs rw 0 0\n"
        rclone = f"plexcache-sftp: {self.mnt} fuse.rclone rw 0 0\n"
        self.assertEqual(pp.media_mount(self.cfg, sshfs)["fstype"], "fuse.sshfs")
        self.assertTrue(pp.media_mount(self.cfg, rclone)["undo"])
        # The settings point elsewhere, but there's exactly one SSHFS mount.
        other = "u@h:/data /media/x fuse.sshfs rw 0 0\n"
        self.assertEqual(pp.media_mount(self.cfg, other)["where"], "/media/x")
        with mock.patch.object(pp, "media_mount", lambda cfg: {
                "where": self.mnt, "fstype": "fuse.sshfs", "undo": False,
                "note": "2026-10-10 11:00 Test passed"}):
            page = pp.render_settings(self.cfg, "t", None, True)
        self.assertIn("action=switch/run", page)
        self.assertIn("Last switch: 2026-10-10 11:00 Test passed", page)
        # On the cached mount there's nothing to do here; going back is CLI-only.
        with mock.patch.object(pp, "media_mount", lambda cfg: {
                "where": self.mnt, "fstype": "fuse.rclone", "undo": True, "note": ""}):
            page = pp.render_settings(self.cfg, "t", None, True)
        self.assertNotIn("id=s-switch", page)
        self.assertNotIn("switch/back", page)
        with mock.patch.object(pp, "media_mount", lambda cfg: {
                "where": self.mnt, "fstype": "", "undo": True, "note": ""}):
            self.assertIn("isn't running", pp.render_settings(self.cfg, "t", None, True))

    def test_failed_restart_puts_old_files_back(self):
        with open(self.env) as f:
            old = f.read()
        pp.update_env(self.env, {"EXTRA_ARGS": "--nonsense"})
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            rc = 1 if len(calls) == 1 else 0
            return subprocess.CompletedProcess(cmd, rc, "", "Job failed. See journalctl")
        with mock.patch.object(pp.subprocess, "run", fake_run):
            self.assertEqual(self.real_restart({self.env: old}), "reverted")
        self.assertEqual(open(self.env).read(), old)
        self.assertEqual(len(calls), 2)

        def no_sudo(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, "", "sudo: a password is required")
        pp.update_env(self.env, {"EXTRA_ARGS": "--nonsense"})
        with mock.patch.object(pp.subprocess, "run", no_sudo):
            self.assertEqual(self.real_restart({self.env: old}), "no-restart")
        self.assertIn("--nonsense", open(self.env).read())


class StateTests(unittest.TestCase):
    def test_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = pp.Config(None)
            cfg.state_file = os.path.join(d, "sub", "state.json")
            pf = pp.Prefetcher(cfg)
            now = pp.time.time()
            pf.done["/m/a.mkv"] = now
            pf.done["/m/old.mkv"] = now - cfg.remember - 10
            pf.heads["/m/b.mkv"] = now
            pf.recent.append({"path": "/m/a.mkv", "mb": 5, "secs": 1, "at": now})
            pf.history.append({"t": now - 90000, "cache": 1})  # older than a day
            pf.record_history({"cache": {"used": 2 * pp.MB}, "streams": [{"level": pp.BAD}],
                               "jobs": [{"rate": pp.MB, "state": "reading"}]})
            pf.save_state()
            pf2 = pp.Prefetcher(cfg)
            pf2.load_state()
            self.assertEqual(set(pf2.done), {"/m/a.mkv"})
            self.assertIn("/m/b.mkv", pf2.heads)
            self.assertEqual(len(pf2.recent), 1)
            self.assertEqual([(p["playing"], p["waiting"]) for p in pf2.history], [(1, 1)])
            # A broken file is ignored rather than stopping the service.
            with open(cfg.state_file, "w") as f:
                f.write("{not json")
            pp.Prefetcher(cfg).load_state()

    def test_history_graphs(self):
        now = 1_000_000.0
        self.assertIn("once Plex-Cache has been running", pp.render_history([], now))
        pts = [{"t": now - 600 + i * 60, "cache": i * pp.MB, "mbps": i, "playing": 1,
                "waiting": 1 if i == 3 else 0} for i in range(10)]
        html_ = pp.render_history(pts, now)
        self.assertEqual(html_.count("<svg"), 3)
        self.assertIn("class='g-wait'", html_)
        self.assertIn("buffering about 1 min", html_)


class VersionTests(unittest.TestCase):
    def test_changelog_matches_version(self):
        path = os.path.join(os.path.dirname(__file__), "..", "CHANGELOG.md")
        with open(path) as f:
            top = re.search(r"^## (\S+)", f.read(), re.M).group(1)
        self.assertEqual(top, pp.__version__)


if __name__ == "__main__":
    unittest.main()
