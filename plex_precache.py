#!/usr/bin/env python3
"""plex-precache: pull media into the local cache before Plex needs it.

Polls Plex for active sessions and reads the playing file (from the current
playhead onwards) plus the next few episodes through the cached mount. Reading
a file through an rclone mount in `--vfs-cache-mode full` (or an NFS/CIFS mount
with fscache) copies it to local disk, so Plex then plays it from the cache
instead of streaming it across the WAN.

Standard library only, so it runs on any Swizzin box with python3.
"""
from __future__ import annotations

import argparse
import collections
import configparser
import heapq
import html
import http.server
import io
import itertools
import json
import logging
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

__version__ = "1.1.0"
__released__ = "2026-10-10"

log = logging.getLogger("plex-precache")

DEFAULTS = {
    "plex_url": "http://127.0.0.1:32400",
    "plex_token": "",
    "poll_interval": "10",
    "next_episodes": "2",
    "next_episode_head_mb": "0",
    "on_deck": "false",
    "on_deck_limit": "5",
    "on_deck_head_mb": "512",
    "workers": "2",
    "read_block_mb": "8",
    "remember_hours": "12",
    "seek_margin_mb": "64",
    "cache_dir": "/var/cache/plex-cache",
    "min_free_gb": "10",
    "status_bind": "127.0.0.1",
    "status_port": "8789",
    "mount_env": "/etc/plex-precache/rclone-mount.env",
    "require_cache_mount": "true",
    "auto_update": "false",
    "stop_after_minutes": "10",
}

# Lower number runs first.
PRIO_PLAYING = 0
PRIO_NEXT = 1
PRIO_ON_DECK = 2
PRIO_MANUAL = 3

MB = 1024 * 1024


class Config:
    def __init__(self, path: str | None):
        cp = configparser.ConfigParser()
        cp.optionxform = str  # keep path_map keys case-sensitive
        cp.read_dict({"plex": DEFAULTS, "path_map": {}})
        self.path = path
        if path:
            if not cp.read(path):
                raise SystemExit(f"config file not readable: {path}")
        s = cp["plex"]
        self.plex_url = s.get("plex_url").rstrip("/")
        self.plex_token = os.environ.get("PLEX_TOKEN") or s.get("plex_token")
        self.poll_interval = s.getfloat("poll_interval")
        self.next_episodes = s.getint("next_episodes")
        self.next_episode_head = s.getint("next_episode_head_mb") * MB
        self.on_deck = s.getboolean("on_deck")
        self.on_deck_limit = s.getint("on_deck_limit")
        self.on_deck_head = s.getint("on_deck_head_mb") * MB
        self.workers = max(1, s.getint("workers"))
        self.read_block = max(1, s.getint("read_block_mb")) * MB
        self.remember = s.getfloat("remember_hours") * 3600
        self.seek_margin = s.getint("seek_margin_mb") * MB
        self.cache_dir = s.get("cache_dir")
        self.min_free = int(s.getfloat("min_free_gb") * 1024 * MB)
        self.status_bind = s.get("status_bind")
        self.status_port = s.getint("status_port")
        self.mount_env = s.get("mount_env")
        self.require_cache_mount = s.getboolean("require_cache_mount")
        self.auto_update = s.getboolean("auto_update")
        self.stop_after = s.getfloat("stop_after_minutes") * 60
        # Longest prefix first so nested mappings win.
        self.path_map = sorted(cp["path_map"].items(), key=lambda kv: -len(kv[0]))

    def local_path(self, plex_path: str) -> str:
        for plex_prefix, local_prefix in self.path_map:
            if plex_path.startswith(plex_prefix):
                return local_prefix + plex_path[len(plex_prefix):]
        return plex_path


# --------------------------------------------------------------------------
# Plex API


class Plex:
    def __init__(self, url: str, token: str, timeout: float = 15):
        self.url = url
        self.token = token
        self.timeout = timeout

    def get(self, path: str) -> ET.Element:
        sep = "&" if "?" in path else "?"
        url = f"{self.url}{path}"
        if self.token:
            url += f"{sep}X-Plex-Token={urllib.parse.quote(self.token)}"
        req = urllib.request.Request(url, headers={"Accept": "application/xml"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return ET.fromstring(resp.read())


def first_part(video: ET.Element) -> ET.Element | None:
    return video.find("./Media/Part")


def parse_sessions(root: ET.Element) -> list[dict]:
    """Active sessions with the file being played and how far in it is."""
    out = []
    for video in root.findall("./Video"):
        part = first_part(video)
        if part is None or not part.get("file"):
            continue
        out.append({
            "rating_key": video.get("ratingKey"),
            "type": video.get("type"),
            "show_key": video.get("grandparentRatingKey"),
            "title": video.get("grandparentTitle") or video.get("title"),
            "file": part.get("file"),
            "size": int(part.get("size") or 0),
            "view_offset": int(video.get("viewOffset") or 0),
            "duration": int(video.get("duration") or part.get("duration") or 0),
            "state": (video.find("./Player").get("state")
                      if video.find("./Player") is not None else None),
        })
    return out


def next_episode_files(all_leaves: ET.Element, rating_key: str, count: int) -> list[str]:
    """Files of the `count` episodes after `rating_key` in show order."""
    videos = all_leaves.findall("./Video")
    keys = [v.get("ratingKey") for v in videos]
    if rating_key not in keys:
        return []
    files = []
    for video in videos[keys.index(rating_key) + 1:]:
        part = first_part(video)
        if part is not None and part.get("file"):
            files.append(part.get("file"))
        if len(files) >= count:
            break
    return files


def on_deck_files(root: ET.Element, limit: int) -> list[str]:
    files = []
    for video in root.findall("./Video"):
        part = first_part(video)
        if part is not None and part.get("file"):
            files.append(part.get("file"))
        if len(files) >= limit:
            break
    return files


def byte_offset(view_offset_ms: int, duration_ms: int, size: int) -> int:
    """Approximate byte position of the playhead (assumes roughly constant bitrate)."""
    if duration_ms <= 0 or size <= 0:
        return 0
    return int(size * min(1.0, max(0.0, view_offset_ms / duration_ms)))


# --------------------------------------------------------------------------
# Prefetching


class Job:
    def __init__(self, path: str, start: int, end: int | None, prio: int,
                 owner: str | None = None):
        self.path = path
        # The file being watched that this download is for (itself, or the
        # episode before it). None for downloads nobody is watching for.
        self.owner = owner
        self.created = time.time()
        self.cancelled = False
        self.pos = start
        self.end = end  # None means read to EOF
        self.prio = prio
        self.start = start
        self.size = _size_or_zero(path)
        self.started_at: float | None = None
        self.rate = 0.0  # smoothed read speed, bytes/s
        self.lock = threading.Lock()

    def jump_to(self, offset: int) -> None:
        """Move the read pointer forward, e.g. after the viewer seeks ahead."""
        with self.lock:
            if offset > self.pos:
                self.pos = offset


def read_through(job: Job, block: int, stop: threading.Event) -> int:
    """Read `job.path` from `job.pos` to `job.end` and discard the data.

    The point is the side effect: the mount stores what we read in its cache.
    Returns bytes read.
    """
    total = 0
    with open(job.path, "rb", buffering=0) as f:
        while not stop.is_set() and not job.cancelled:
            with job.lock:
                pos = job.pos
                end = job.end
            if end is not None and pos >= end:
                break
            want = block if end is None else min(block, end - pos)
            t0 = time.monotonic()
            f.seek(pos)
            data = f.read(want)
            if not data:
                break
            speed = len(data) / max(time.monotonic() - t0, 1e-6)
            with job.lock:
                # A seek-ahead may have moved pos while we were reading.
                if job.pos == pos:
                    job.pos = pos + len(data)
                job.rate = speed if not job.rate else 0.8 * job.rate + 0.2 * speed
            total += len(data)
    return total


class Prefetcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop = threading.Event()
        self.cv = threading.Condition()
        self.queue: list[tuple[int, int, Job]] = []
        self.counter = itertools.count()
        self.active: dict[str, Job] = {}  # queued or running, by local path
        self.done: dict[str, float] = {}  # path -> time finished
        # For the status page.
        self.recent: collections.deque = collections.deque(maxlen=25)
        self.sessions: list[dict] = []
        self.last_poll: tuple[float, str | None] = (0.0, None)
        self.warned: set[str] = set()
        self.n_workers = 0
        self.last_poll_secs = 0.0
        self.last_seen: dict[str, float] = {}  # playing file -> last time Plex listed it
        self.blocked: dict[str, str] = {}  # mountpoint -> fstype we won't prefetch on

    def submit(self, path: str, start: int = 0, end: int | None = None,
               prio: int = PRIO_PLAYING, owner: str | None = None) -> None:
        with self.cv:
            job = self.active.get(path)
            if job is not None:
                job.jump_to(start)
                return
            finished = self.done.get(path)
            if finished and time.time() - finished < self.cfg.remember:
                return
        if self.cfg.require_cache_mount and self.uncached_mount(path):
            return
        # File checks happen outside the lock: on a busy network mount a stat
        # can take seconds, and holding the lock would stall polling and the
        # status page.
        if not os.path.isfile(path):
            if path not in self.warned:
                self.warned.add(path)
                log.warning("not found locally (check [path_map]): %s", path)
            return
        if prio != PRIO_PLAYING and not self.has_room(path, start, end):
            return
        job = Job(path, start, end, prio, owner)
        with self.cv:
            if path in self.active:
                self.active[path].jump_to(start)
                return
            self.active[path] = job
            heapq.heappush(self.queue, (prio, next(self.counter), job))
            self.cv.notify()
        log.info("queued %s from %d MB (prio %d)", path, start // MB, prio)

    def uncached_mount(self, path: str) -> str | None:
        """The mountpoint if `path` is on a filesystem that can't cache to disk.

        Reading ahead there doesn't keep anything locally; it only competes with
        Plex for the same link, so it's skipped.
        """
        mnt, fstype, opts = mount_info(path)
        if caches_to_disk(fstype, opts):
            return None
        if mnt not in self.blocked:
            self.blocked[mnt] = fstype
            if fstype in LOCAL_FSTYPES:
                log.info("not pre-caching under %s: it's a local disk (%s)", mnt, fstype)
                return mnt
            log.warning("not pre-caching under %s: %s can't keep a disk cache, so reading "
                        "ahead would only compete with Plex. Use the cached rclone mount "
                        "(install.sh --sftp ...), or set require_cache_mount = false.",
                        mnt, fstype)
        return mnt

    def has_room(self, path: str, start: int, end: int | None) -> bool:
        """Whether caching this file would leave `min_free_gb` on the cache disk.

        Only applied to speculative reads (next episodes, On Deck); the file
        that is playing is always read, as Plex will pull it into cache anyway.
        """
        if not self.cfg.cache_dir or not os.path.isdir(self.cfg.cache_dir):
            return True
        size = _size_or_zero(path)
        need = max(0, (size if end is None else min(end, size)) - start)
        free = shutil.disk_usage(self.cfg.cache_dir).free
        if free - need < self.cfg.min_free:
            log.info("skipping %s: %d MB would leave less than %d MB free in %s",
                     path, need // MB, self.cfg.min_free // MB, self.cfg.cache_dir)
            return False
        return True

    def ensure_workers(self) -> None:
        """Start or retire worker threads to match cfg.workers (settable live)."""
        with self.cv:
            missing = max(0, self.cfg.workers - self.n_workers)
            self.n_workers += missing
            self.cv.notify_all()  # lets surplus workers notice and exit
        for _ in range(missing):
            threading.Thread(target=self.worker, daemon=True).start()

    def worker(self) -> None:
        while not self.stop.is_set():
            with self.cv:
                while not self.queue and not self.stop.is_set():
                    if self.n_workers > self.cfg.workers:
                        self.n_workers -= 1
                        return
                    self.cv.wait(timeout=1)
                if self.stop.is_set():
                    return
                _, _, job = heapq.heappop(self.queue)
            if job.cancelled:
                continue
            started = job.started_at = time.time()
            ok = False
            try:
                n = read_through(job, self.cfg.read_block, self.stop)
                if job.cancelled:
                    continue  # finally still runs; not marked done, so it resumes if replayed
                secs = max(time.time() - started, 0.001)
                log.info("cached %s: %d MB in %.0fs (%.1f MB/s)",
                         job.path, n // MB, secs, n / MB / secs)
                self.recent.appendleft({"path": job.path, "mb": n // MB,
                                        "secs": round(secs), "at": time.time()})
                ok = True
            except OSError as e:
                log.warning("read failed for %s: %s", job.path, e)
            finally:
                with self.cv:
                    if self.active.get(job.path) is job:
                        del self.active[job.path]
                    # Failed reads are retried on the next poll.
                    if ok and not self.stop.is_set():
                        self.done[job.path] = time.time()

    def saw_playing(self, paths: list[str]) -> None:
        now = time.time()
        for p in paths:
            self.last_seen[p] = now

    def stop_abandoned(self) -> None:
        """Stop downloading for streams nobody has had open for stop_after_minutes.

        A paused stream still shows in Plex, so it keeps its downloads; this only
        catches viewers who closed the player.
        """
        if self.cfg.stop_after <= 0:
            return
        now = time.time()
        with self.cv:
            jobs = [j for j in self.active.values() if j.owner and not j.cancelled]
            for job in jobs:
                seen = self.last_seen.get(job.owner, job.created)
                if now - seen >= self.cfg.stop_after:
                    job.cancelled = True
                    del self.active[job.path]
                    log.info("stopped caching %s: nothing has played it for %.0f min",
                             job.path, (now - seen) / 60)
                    self.recent.appendleft({"path": job.path, "mb": 0, "secs": 0,
                                            "at": now, "stopped": True})
            stale = now - max(self.cfg.stop_after, 3600)
            for p in [p for p, t in self.last_seen.items() if t < stale]:
                del self.last_seen[p]

    def forget_expired(self) -> None:
        cutoff = time.time() - self.cfg.remember
        with self.cv:
            for path in [p for p, t in self.done.items() if t < cutoff]:
                del self.done[path]

    def snapshot(self) -> dict:
        """State for the status page."""
        with self.cv:
            jobs = list(self.active.values())
        rows = []
        for job in sorted(jobs, key=lambda j: (j.started_at is None, j.prio)):
            with job.lock:
                pos, end = job.pos, job.end
            last = min(end, job.size) if end is not None and job.size else (end or job.size)
            span = max(1, (last or 0) - job.start)
            rows.append({
                "path": job.path,
                "pos": pos,
                "rate": job.rate,
                "kind": {PRIO_PLAYING: "playing", PRIO_NEXT: "next episode",
                         PRIO_ON_DECK: "on deck", PRIO_MANUAL: "requested"}.get(
                             job.prio, str(job.prio)),
                "state": "reading" if job.started_at else "queued",
                "percent": round(100 * min(1.0, max(0, pos - job.start) / span)),
                "mb_s": (round((pos - job.start) / MB / max(1, time.time() - job.started_at), 1)
                         if job.started_at else None),
            })
        polled, err = self.last_poll
        with self.cv:
            done = dict(self.done)
        return {"jobs": rows, "done": done, "poll_secs": self.last_poll_secs, "warned": sorted(self.warned),
                "blocked": dict(self.blocked), "recent": list(self.recent), "sessions": self.sessions,
                "last_poll": polled, "poll_error": err}

    def shutdown(self, *_args) -> None:
        self.stop.set()
        with self.cv:
            self.cv.notify_all()


def poll_once(plex: Plex, cfg: Config, pf: Prefetcher) -> None:
    sessions = parse_sessions(plex.get("/status/sessions"))
    for s in sessions:
        s["path"] = cfg.local_path(s["file"])
        s["size"] = s["size"] or _size_or_zero(s["path"])
        s["seen_at"] = time.time()
    pf.sessions = sessions
    pf.saw_playing([s["path"] for s in sessions])
    for s in sessions:
        path, size = s["path"], s["size"]
        start = max(0, byte_offset(s["view_offset"], s["duration"], size) - cfg.seek_margin)
        pf.submit(path, start=start, prio=PRIO_PLAYING, owner=path)

        if s["type"] == "episode" and s["show_key"] and cfg.next_episodes > 0:
            try:
                leaves = plex.get(f"/library/metadata/{s['show_key']}/allLeaves")
            except OSError as e:
                log.warning("could not list episodes for %s: %s", s["title"], e)
                continue
            for f in next_episode_files(leaves, s["rating_key"], cfg.next_episodes):
                pf.submit(cfg.local_path(f), end=cfg.next_episode_head or None,
                          prio=PRIO_NEXT, owner=path)

    if cfg.on_deck:
        for f in on_deck_files(plex.get("/library/onDeck"), cfg.on_deck_limit):
            pf.submit(cfg.local_path(f), end=cfg.on_deck_head or None, prio=PRIO_ON_DECK)


# Filesystems where reading a file leaves a copy on local disk.
DISK_CACHING_FSTYPES = {"fuse.rclone"}
# Local disks: nothing to pre-cache, the media is already here.
LOCAL_FSTYPES = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "overlay", "tmpfs",
                 "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "fuse.mergerfs"}
# Network filesystems that do, but only when mounted with the `fsc` option.
FSCACHE_FSTYPES = {"nfs", "nfs4", "cifs", "smb3"}


def _unescape_mount(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def mount_info(path: str, mounts_text: str | None = None) -> tuple[str, str, str]:
    """(mountpoint, fstype, options) for the mount holding `path`, without touching it."""
    if mounts_text is None:
        try:
            with open("/proc/mounts") as f:
                mounts_text = f.read()
        except OSError:
            return "/", "unknown", ""
    path = os.path.abspath(path)
    best = ("/", "unknown", "")
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mnt = _unescape_mount(parts[1])
        inside = path == mnt or path.startswith(mnt.rstrip("/") + "/")
        if inside and len(mnt) >= len(best[0]):
            best = (mnt, parts[2], parts[3])
    return best


def caches_to_disk(fstype: str, opts: str) -> bool:
    return fstype in DISK_CACHING_FSTYPES or (
        fstype in FSCACHE_FSTYPES and "fsc" in opts.split(","))


def _size_or_zero(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def run_daemon(cfg: Config) -> None:
    if not cfg.plex_token:
        log.warning("no plex_token set; Plex will reject requests unless it allows "
                    "unauthenticated local access")
    plex = Plex(cfg.plex_url, cfg.plex_token)
    pf = Prefetcher(cfg)
    signal.signal(signal.SIGTERM, pf.shutdown)
    signal.signal(signal.SIGINT, pf.shutdown)
    pf.ensure_workers()
    if cfg.status_port:
        start_status_server(cfg, pf)
    log.info("watching %s every %.0fs with %d workers",
             cfg.plex_url, cfg.poll_interval, cfg.workers)
    while not pf.stop.is_set():
        t0 = time.time()
        try:
            poll_once(plex, cfg, pf)
            pf.last_poll = (time.time(), None)
            pf.stop_abandoned()  # only after a good poll: if Plex is down, keep going
            pf.last_poll_secs = time.time() - t0
            if pf.last_poll_secs > 10:
                log.warning("checking Plex took %.0fs; the media mount is slow to respond",
                            pf.last_poll_secs)
        except (OSError, ET.ParseError) as e:
            log.warning("poll failed: %s", e)
            pf.last_poll = (time.time(), str(e))
        pf.forget_expired()
        pf.stop.wait(cfg.poll_interval)


# --------------------------------------------------------------------------
# Status page (proxied by nginx at /plexcache/ on Swizzin)


def read_env_file(path: str) -> dict:
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return out


_usage_cache: dict = {}


def dir_usage(path: str, max_age: float = 30) -> int | None:
    """Bytes actually stored under `path` (rclone cache files are sparse)."""
    hit = _usage_cache.get(path)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    if not os.path.isdir(path):
        return None
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_blocks * 512
            except OSError:
                pass
    _usage_cache[path] = (time.time(), total)
    return total


def status_data(cfg: Config, pf: Prefetcher) -> dict:
    data = pf.snapshot()
    disk = None
    if cfg.cache_dir and os.path.isdir(cfg.cache_dir):
        du = shutil.disk_usage(cfg.cache_dir)
        disk = {"total": du.total, "free": du.free}
    env = read_env_file(cfg.mount_env)
    data["cache"] = {
        "dir": cfg.cache_dir,
        "used": dir_usage(cfg.cache_dir) if cfg.cache_dir else None,
        "disk": disk,
        "min_free": cfg.min_free,
        "mount": {k: env.get(k) for k in ("REMOTE", "MOUNTPOINT", "CACHE_MAX_SIZE",
                                          "CACHE_MIN_FREE", "CACHE_MAX_AGE",
                                          "READ_AHEAD", "BUFFER_SIZE")},
    }
    data["mem_available"] = mem_available()
    data["poll_interval"] = cfg.poll_interval
    data["streams"] = [stream_health(s, data) for s in data["sessions"]]
    data["checks"] = system_checks(data)
    levels = [x["level"] for x in data["streams"] + data["checks"]]
    data["overall"] = max(levels, key=LEVELS.index) if levels else GOOD
    return data


# --------------------------------------------------------------------------
# Health grading for the status page

GOOD, INFO, WARN, BAD = "good", "info", "warn", "bad"
LEVELS = [INFO, GOOD, WARN, BAD]  # ascending severity

# A stream is healthy when the cache is this many seconds ahead of playback...
AHEAD_GOOD_S = 120
AHEAD_WARN_S = 30
# ...and the link downloads at least this many times faster than the video plays.
SPEED_GOOD_RATIO = 1.5


def parse_size(v: str | None) -> int | None:
    """'16G' -> bytes. Accepts K/M/G/T suffixes, as rclone does."""
    if not v:
        return None
    v = v.strip().upper().rstrip("B")
    mult = {"K": 1024, "M": MB, "G": 1024 * MB, "T": 1024 ** 4}.get(v[-1:], 1)
    try:
        return int(float(v.rstrip("KMGT")) * mult)
    except ValueError:
        return None


def mem_available() -> int | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def _mbps(bytes_per_s: float) -> str:
    return f"{bytes_per_s * 8 / 1e6:.0f} Mbps"


def _dur(secs: float) -> str:
    secs = max(0, int(secs))
    return f"{secs // 60} min" if secs >= 120 else f"{secs}s"


def stream_health(s: dict, d: dict, now: float | None = None) -> dict:
    """How far the cache is ahead of one Plex stream, and whether it can stay ahead."""
    now = now or time.time()
    path = s.get("path") or s["file"]
    size, dur_ms = s.get("size") or 0, s.get("duration") or 0
    bitrate = size / (dur_ms / 1000) if size and dur_ms else 0  # bytes/s of video
    playhead = byte_offset(s.get("view_offset", 0), dur_ms, size)
    if s.get("state") == "playing" and bitrate:
        # Plex's offset is as old as our last poll; playback has moved on since.
        playhead = min(size, playhead + int(bitrate * (now - s.get("seen_at", now))))
    job = next((j for j in d["jobs"] if j["path"] == path), None)
    out = {"title": s.get("title") or os.path.basename(path), "state": s.get("state"),
           "bitrate": bitrate, "file": os.path.basename(path), "ahead_s": None}

    if job is None and path in d.get("done", {}):
        level, msg = GOOD, "Fully cached: playing from local disk."
        out["ahead_s"] = (size - playhead) / bitrate if bitrate else None
    elif job is not None and job["state"] == "queued":
        level, msg = WARN, ("Waiting for a free worker; other downloads are using them. "
                            "Plex is reading from remote meanwhile.")
    elif job is not None:
        ahead = job["pos"] - playhead
        rate = job["rate"]
        ahead_s = ahead / bitrate if bitrate else None
        out["ahead_s"] = ahead_s
        ratio = rate / bitrate if bitrate else None
        speed = (f"downloading at {_mbps(rate)} for a {_mbps(bitrate)} video"
                 if bitrate else f"downloading at {_mbps(rate)}")
        if ahead < 0:
            level, msg = BAD, ("Playback has overtaken the cache, so Plex is streaming "
                               f"straight from remote ({speed}).")
        elif ratio is not None and ratio < 1:
            left = ahead / (bitrate - rate) if bitrate > rate else 0
            level, msg = BAD, (f"Link is slower than the video ({speed}). "
                               f"{_dur(ahead_s or 0)} cached ahead, which runs out in about "
                               f"{_dur(left)}; expect buffering after that.")
        elif ahead_s is not None and ahead_s < AHEAD_WARN_S:
            level, msg = WARN, f"Only {_dur(ahead_s)} cached ahead so far ({speed})."
        elif ratio is not None and ratio < SPEED_GOOD_RATIO:
            level, msg = WARN, (f"{_dur(ahead_s or 0)} cached ahead, but the link is only "
                                f"just faster than the video ({speed}).")
        elif ahead_s is not None and ahead_s < AHEAD_GOOD_S:
            level, msg = GOOD, (f"Building a head start: {_dur(ahead_s)} cached ahead, "
                                f"{speed} ({ratio:.1f}x real time).")
        else:
            level, msg = GOOD, (f"{_dur(ahead_s or 0)} cached ahead, {speed}"
                                + (f" ({ratio:.1f}x real time)." if ratio else "."))
    elif blocked_fs := next((fs for m, fs in d.get("blocked", {}).items()
                             if path == m or path.startswith(m.rstrip("/") + "/")), None):
        if blocked_fs in LOCAL_FSTYPES:
            level, msg = GOOD, "On a local disk, so it doesn't need pre-caching."
        else:
            level, msg = BAD, ("Not pre-cached: the media folder is a mount that can't cache "
                               "to disk, so this plays straight over the network (see Health).")
    elif path in d.get("warned", []):
        level, msg = BAD, ("File isn't visible on this box, so it can't be cached. "
                           "Check the mount and [path_map].")
    else:
        level, msg = INFO, "Just started; caching begins on the next check."

    if s.get("state") == "buffering":
        level, msg = BAD, "Plex reports this stream is buffering. " + msg
    out.update(level=level, detail=msg)
    return out


def system_checks(d: dict) -> list[dict]:
    checks = []
    c, m = d["cache"], d["cache"]["mount"]

    if d.get("poll_error"):
        checks.append({"level": BAD, "title": "Plex",
                       "detail": f"Can't reach Plex: {d['poll_error']}"})
    elif not d.get("last_poll"):
        checks.append({"level": INFO, "title": "Plex", "detail": "Not checked yet."})
    elif time.time() - d["last_poll"] > 3 * d.get("poll_interval", 10) + 5:
        checks.append({"level": WARN, "title": "Plex",
                       "detail": "Last successful check was a while ago."})
    elif d.get("poll_secs", 0) > 10:
        checks.append({"level": WARN, "title": "Plex", "detail":
            f"Each check is taking {d['poll_secs']:.0f}s because the media folder is slow to "
            "respond, so new streams show up late."})
    else:
        checks.append({"level": GOOD, "title": "Plex", "detail": "Connected."})

    for mnt, fstype in d.get("blocked", {}).items():
        if fstype in LOCAL_FSTYPES:
            continue
        hint = (" Switch to the cached rclone mount with install.sh --sftp (see the README's "
                "\"Coming from SSHFS\")." if fstype == "fuse.sshfs" else
                " Mount it through the cached rclone mount instead.")
        checks.append({"level": BAD, "title": "Media mount", "detail":
            f"{mnt} is {fstype}, which can't keep a disk cache. Pre-caching is paused there "
            f"because it would only compete with Plex for the same link.{hint}"})

    disk = c.get("disk")
    if disk:
        free, floor = disk["free"], c["min_free"]
        if free < floor:
            checks.append({"level": BAD, "title": "Disk space", "detail":
                f"{_gb(free)} free, below the {_gb(floor)} floor. Next-episode caching is "
                "paused and rclone is evicting files. Free up space or run "
                "plex-cache-resize --min-free with a lower value."})
        elif free < 2 * floor:
            checks.append({"level": WARN, "title": "Disk space", "detail":
                f"{_gb(free)} free; getting close to the {_gb(floor)} floor."})
        else:
            checks.append({"level": GOOD, "title": "Disk space",
                           "detail": f"{_gb(free)} free."})

    cap = parse_size(m.get("CACHE_MAX_SIZE"))
    if cap and c.get("used") is not None:
        pct = 100 * c["used"] / cap
        if pct >= 95:
            checks.append({"level": WARN, "title": "Cache size", "detail":
                f"Full ({_gb(c['used'])} of {m['CACHE_MAX_SIZE']}). rclone is evicting the "
                "least recently watched files, so rewatches may download again. "
                "Normal on a small disk; plex-cache-resize can make it bigger."})
        else:
            checks.append({"level": GOOD, "title": "Cache size", "detail":
                f"{_gb(c['used'])} of {m['CACHE_MAX_SIZE']} used ({pct:.0f}%)."})

    mem = d.get("mem_available")
    if mem is not None:
        per_stream = parse_size(m.get("BUFFER_SIZE")) or 0
        need = per_stream * max(1, len(d["sessions"]))
        if mem < 512 * MB or mem < need:
            checks.append({"level": BAD, "title": "Memory", "detail":
                f"Only {_gb(mem)} available. rclone's {m.get('BUFFER_SIZE')} per-stream "
                "buffers and Plex transcodes may be starved; lower BUFFER_SIZE."})
        elif mem < 1024 * MB:
            checks.append({"level": WARN, "title": "Memory",
                           "detail": f"{_gb(mem)} available; running low."})
        else:
            checks.append({"level": GOOD, "title": "Memory",
                           "detail": f"{_gb(mem)} available."})

    rates = [j["rate"] for j in d["jobs"] if j["state"] == "reading" and j["rate"]]
    rates += [r["mb"] * MB / r["secs"] for r in d["recent"][:5] if r["secs"] >= 5]
    if rates:
        best = max(rates)
        top = max((s["bitrate"] for s in d.get("streams", []) if s["bitrate"]), default=0)
        msg = f"Recent downloads from remote reached {_mbps(best)}."
        level = INFO
        if top and best < top:
            level = WARN
            msg += f" That's below the {_mbps(top)} needed to keep up with what's playing."
        checks.append({"level": level, "title": "Link speed", "detail": msg})
    return checks


def _gb(n: int | None) -> str:
    return "?" if n is None else f"{n / 1024 / MB:.1f} GB"


LEVEL_LABEL = {GOOD: "Good", INFO: "Info", WARN: "Borderline", BAD: "Struggling"}
VERDICT = {
    GOOD: "All good: streams are playing from cache.",
    INFO: "Nothing to report.",
    WARN: "Worth keeping an eye on.",
    BAD: "Something is likely to cause buffering.",
}


def render_status(d: dict, settings: str = "") -> str:
    e = html.escape
    c = d["cache"]
    m = c["mount"]

    def badge(level: str) -> str:
        return f"<span class='badge {level}'>{LEVEL_LABEL[level]}</span>"

    def bar(ahead_s: float | None) -> str:
        if ahead_s is None:
            return ""
        pct = max(0, min(100, 100 * ahead_s / (AHEAD_GOOD_S * 5)))
        return (f"<div class=bar title='cached ahead of playback'>"
                f"<i style='width:{pct:.0f}%'></i></div>")

    streams = "".join(
        f"<div class='card {s['level']}'><div class=row><b>{e(s['title'])}</b>"
        f"{badge(s['level'])}</div><div class=muted>{e(s['file'])} · "
        f"{e(s['state'] or 'unknown')}"
        f"{' · ' + _mbps(s['bitrate']) + ' video' if s['bitrate'] else ''}</div>"
        f"{bar(s['ahead_s'])}<p>{e(s['detail'])}</p></div>"
        for s in d["streams"]) or "<p class=muted>Nothing playing in Plex.</p>"
    checks = "".join(
        f"<tr><td class=k>{e(x['title'])}</td><td>{badge(x['level'])}</td><td>{e(x['detail'])}</td></tr>"
        for x in d["checks"])
    rows = "".join(
        f"<tr><td>{e(os.path.basename(j['path']))}</td><td>{e(j['kind'])}</td>"
        f"<td>{e(j['state'])}</td><td><progress max=100 value={j['percent']}></progress> "
        f"{j['percent']}%</td><td>{_mbps(j['rate']) if j['rate'] else ''}</td></tr>"
        for j in d["jobs"]) or "<tr><td colspan=5 class=muted>Nothing being cached right now</td></tr>"
    recent = "".join(
        f"<tr><td>{e(os.path.basename(r['path']))}</td>"
        f"<td>{'stopped (not watched)' if r.get('stopped') else str(r['mb']) + ' MB'}</td>"
        f"<td>{_mbps(r['mb'] * MB / r['secs']) if r['secs'] else ''}</td>"
        f"<td>{time.strftime('%H:%M', time.localtime(r['at']))}</td></tr>"
        for r in d["recent"]) or "<tr><td colspan=4 class=muted>None yet</td></tr>"
    disk = c["disk"] or {}
    limits = " · ".join(f"{k.replace('_', ' ').lower()}: {e(v)}" for k, v in m.items() if v)
    overall = d["overall"]
    verdict = VERDICT[overall]
    if not d["streams"] and overall in (GOOD, INFO):
        verdict = "Nothing playing right now, and everything looks healthy."
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Plex-Cache</title><style>
:root{{color-scheme:light dark;--bg:#fff;--fg:#1c1c1c;--muted:#666;--line:#ddd;--card:#f6f6f7;
--good:#1f8a4c;--warn:#b7791f;--bad:#c53030;--info:#4a6fa5}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181d;--fg:#e6e6e6;--muted:#9a9a9a;
--line:#333;--card:#1f2228;--good:#3fb871;--warn:#e0a43a;--bad:#ef5b5b;--info:#7aa2e3}}}}
body{{background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif;margin:0 auto;
max-width:900px;padding:16px}}h1{{font-size:22px;margin:0}}h2{{font-size:16px;margin-top:28px}}
table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);overflow-wrap:anywhere;vertical-align:top}}
.muted{{color:var(--muted);font-size:13px}}.row{{display:flex;justify-content:space-between;
gap:8px;align-items:center}}.stats{{display:flex;gap:24px;flex-wrap:wrap;margin-top:12px}}
.stats b{{display:block;font-size:20px}}.badge{{font-size:12px;font-weight:600;padding:2px 8px;
border-radius:10px;color:#fff;white-space:nowrap}}.badge.good{{background:var(--good)}}
.badge.warn{{background:var(--warn)}}.badge.bad{{background:var(--bad)}}
.badge.info{{background:var(--info)}}.card{{background:var(--card);border-left:4px solid
var(--info);border-radius:6px;padding:10px 12px;margin:8px 0}}.card p{{margin:6px 0 0}}
.card.good{{border-color:var(--good)}}.card.warn{{border-color:var(--warn)}}
.card.bad{{border-color:var(--bad)}}.verdict{{font-size:17px;font-weight:600;margin-top:12px}}
.verdict.good{{color:var(--good)}}.verdict.warn{{color:var(--warn)}}
.verdict.bad{{color:var(--bad)}}.verdict.info{{color:var(--info)}}
.bar{{height:6px;background:var(--line);border-radius:3px;margin-top:8px;overflow:hidden}}
.bar i{{display:block;height:100%;background:var(--good)}}.card.warn .bar i{{background:var(--warn)}}
.card.bad .bar i{{background:var(--bad)}}button{{font:inherit;font-size:13px;padding:3px 10px}}
details{{margin-top:24px}}
.live{{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);
background:var(--card);color:var(--fg);border-radius:999px;padding:5px 12px 5px 10px;
cursor:pointer;font-size:13px;line-height:1.2}}.live:hover{{border-color:var(--muted)}}
.live:focus-visible{{outline:2px solid var(--info);outline-offset:2px}}
.live .dot{{width:9px;height:9px;border-radius:50%;background:var(--good);flex:none}}
.live.on .dot{{animation:pulse 2s ease-in-out infinite}}
.live.paused .dot{{background:var(--muted)}}.live.offline .dot{{background:var(--bad)}}
.live .sub{{color:var(--muted)}}.ver{{font-size:13px;font-weight:500;color:var(--muted);
border:1px solid var(--line);border-radius:6px;padding:1px 6px;vertical-align:middle}}
@keyframes pulse{{0%,100%{{box-shadow:0 0 0 0 rgba(63,184,113,.6)}}
50%{{box-shadow:0 0 0 5px rgba(63,184,113,0)}}}}
@media (prefers-reduced-motion:reduce){{.live.on .dot{{animation:none}}}}
.scroll{{overflow-x:auto}}td.k{{white-space:nowrap}}
td.k+td{{width:1%}}progress{{width:90px}}html,body{{overflow-x:hidden}}{SETTINGS_CSS}</style></head><body>
<div class=row><h1>Plex-Cache <span class=ver>v{__version__}</span></h1>
<button id=toggle class='live on' type=button aria-pressed=false
title="Pause or resume live updates"><span class=dot></span><b id=livetext>Live</b>
<span class=sub id=updated></span></button></div>
<div id=main>
<div class='verdict {overall}'>{e(verdict)}</div>
<div class=stats><div><b>{_gb(c['used'])}</b><span class=muted>cached
{('of ' + e(m['CACHE_MAX_SIZE'])) if m.get('CACHE_MAX_SIZE') else ''}</span></div>
<div><b>{_gb(disk.get('free'))}</b><span class=muted>free on cache disk</span></div>
<div><b>{_gb(d.get('mem_available'))}</b><span class=muted>RAM available</span></div>
<div><b>{len(d['sessions'])}</b><span class=muted>playing in Plex</span></div></div>
<h2>Streams</h2>{streams}
<h2>Health</h2><div class=scroll><table>{checks}</table></div>
<h2>Caching now</h2><div class=scroll><table><tr><th>File</th><th>Why</th><th>State</th><th>Progress</th>
<th>Speed</th></tr>{rows}</table></div>
<h2>Recently cached</h2><div class=scroll><table><tr><th>File</th><th>Read</th><th>Speed</th>
<th>At</th></tr>{recent}</table></div>
<p class=muted>{limits or 'Mount settings not readable'}</p>
</div>
{settings}
<details><summary>What's normal?</summary>
<p><b>Good</b>: the cache is at least {AHEAD_GOOD_S // 60} minutes ahead of playback and
the link downloads at least {SPEED_GOOD_RATIO}x faster than the video plays, or the file is
fully cached. A new stream starts amber for a minute while it builds a head start.</p>
<p><b>Borderline</b>: under {AHEAD_WARN_S}s cached ahead, a link only just faster than the
video, the cache full and evicting, or disk or RAM getting low. Playback usually holds, but
there's little slack.</p>
<p><b>Struggling</b>: Plex says it's buffering, playback has overtaken the cache, the link is
slower than the video's bitrate, or the disk is below its free-space floor. Fixes: pre-warm
with --warm before watching, lower the quality in Plex, or give the cache more disk.</p>
</details>
<script>
(function () {{
  var paused = false, last = Date.now(), failed = false;
  try {{ paused = localStorage.getItem("plexcache-paused") === "1"; }} catch (e) {{}}
  var btn = document.getElementById("toggle"), upd = document.getElementById("updated"),
      txt = document.getElementById("livetext");
  function label() {{
    var s = Math.round((Date.now() - last) / 1000);
    var ago = s < 2 ? "just now" : s < 120 ? s + "s ago" : Math.round(s / 60) + " min ago";
    var state = paused ? "paused" : failed ? "offline" : "on";
    btn.className = "live " + state;
    btn.setAttribute("aria-pressed", paused ? "true" : "false");
    txt.textContent = paused ? "Paused" : failed ? "Reconnecting" : "Live";
    upd.textContent = paused ? "tap to resume" : "updated " + ago;
  }}
  btn.onclick = function () {{
    paused = !paused;
    try {{ localStorage.setItem("plexcache-paused", paused ? "1" : "0"); }} catch (e) {{}}
    label(); if (!paused) refresh();
  }};
  function refresh() {{
    if (paused) return;
    fetch(location.href, {{cache: "no-store"}}).then(function (r) {{
      if (!r.ok) throw new Error(r.status); return r.text();
    }}).then(function (t) {{
      var doc = new DOMParser().parseFromString(t, "text/html");
      var next = doc.getElementById("main");
      if (next) document.getElementById("main").innerHTML = next.innerHTML;
      last = Date.now(); failed = false; label();
    }}).catch(function () {{ failed = true; label(); }});
  }}
  setInterval(refresh, 5000); setInterval(label, 1000); label();
}})();
</script></body></html>"""


# --------------------------------------------------------------------------
# Settings editable from the status page

# (key, label, kind, min, max, help). Applied live, no restart needed.
PREFETCH_FIELDS = [
    ("next_episodes", "Upcoming episodes to pre-cache", "int", 0, 10,
     "After the episode that's playing. 0 turns it off."),
    ("next_episode_head_mb", "Only the first N MB of each", "int", 0, 100000,
     "0 caches whole episodes. A few hundred MB covers the start on a slow link."),
    ("on_deck", "Pre-cache Continue Watching", "bool", 0, 1,
     "Warm the start of what you're likely to watch next."),
    ("on_deck_limit", "Continue Watching items", "int", 0, 50, ""),
    ("on_deck_head_mb", "MB from each Continue Watching item", "int", 0, 100000, ""),
    ("workers", "Downloads at once", "int", 1, 8,
     "1 on a slow link, so the playing file always comes first."),
    ("stop_after_minutes", "Stop downloading after nobody's watching for (min)", "int", 0, 240,
     "Once a stream is closed (not just paused) for this long, its download and upcoming "
     "episodes stop. 0 never stops."),
    ("poll_interval", "Seconds between checks of Plex", "int", 2, 300, ""),
    ("remember_hours", "Hours before re-checking a cached file", "int", 0, 720,
     "Keep at or below the expiry below."),
]
# rclone mount settings; saving restarts the mount.
MOUNT_FIELDS = [
    ("CACHE_MAX_SIZE", "Cache size", "size",
     "Total disk the cache may use, e.g. 20G. Leave blank to use the percentage."),
    ("CACHE_PERCENT", "...or % of usable disk", "percent",
     "Sizes the cache to this share of free disk after the floor below. Blank to use a fixed size."),
    ("CACHE_MIN_FREE", "Always keep free on disk", "size",
     "rclone evicts files to keep this free, and pre-caching pauses below it."),
    ("CACHE_MAX_AGE", "Drop files unwatched for", "duration", "e.g. 72h, 7d, 2w."),
    ("READ_AHEAD", "Read ahead per stream", "size", "How far rclone downloads ahead of Plex."),
    ("BUFFER_SIZE", "RAM buffer per stream", "size", "Memory per open file, e.g. 128M."),
]
# More rclone mount settings, shown under "Advanced" in the same form.
RCLONE_FIELDS = [
    ("VFS_CHUNK_SIZE", "Chunk size", "size",
     "Size of each ranged request to the server; it doubles up to 2G on long reads. "
     "Larger suits fast, high-latency links."),
    ("DIR_CACHE_TIME", "Keep folder listings for", "duration",
     "How long rclone trusts its folder listings before asking the server again. "
     "New files on the server can take this long to appear."),
    ("EXTRA_ARGS", "Extra rclone flags", "flags",
     "Any other rclone mount flags, e.g. --vfs-read-chunk-streams 4 (rclone 1.68+, "
     "parallel downloads per file) or --bwlimit 80M. If the mount won't start with "
     "them, the previous flags are put back."),
]
# What a blank advanced field means (and what the mount unit used before they existed).
RCLONE_DEFAULTS = {"VFS_CHUNK_SIZE": "64M", "DIR_CACHE_TIME": "1h"}
# Flags that would point rclone somewhere else or expose it; set elsewhere or not at all.
BLOCKED_FLAGS = ("--config", "--cache-dir", "--rc", "--daemon", "--allow-root",
                 "--log-file", "--password-command", "--user-agent", "--ask-password")
FLAG_TOKEN_RE = re.compile(r"^(--?[A-Za-z0-9][A-Za-z0-9-]*(=[A-Za-z0-9._:/,+@%-]*)?"
                           r"|[A-Za-z0-9._:/,+@%-]+)$")
SIZE_RE = re.compile(r"^\d+(\.\d+)?[KMGT]$")
DURATION_RE = re.compile(r"^\d+[smhdwMy]$")


class SettingsError(ValueError):
    pass


GB = 1024 * MB


def mem_total() -> int | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def _size_str(n: int) -> str:
    mb = max(1, n // MB)
    return f"{mb // 1024}G" if mb % 1024 == 0 else f"{mb}M"


def _nearest_pow2_mb(n: float, lo: int, hi: int) -> int:
    """Clamp to [lo, hi] bytes and snap to the nearest power-of-two MB."""
    mb = max(lo, min(hi, n)) / MB
    return int(2 ** round(math.log2(max(mb, 1)))) * MB


def recommend_settings(cache_dir: str, mem: int | None = None, cpus: int | None = None,
                       disk: tuple[int, int] | None = None, cached: int | None = None) -> dict:
    """Settings that suit this server's disk, RAM and CPU.

    disk is (total, free) bytes for the cache disk; cached is what the cache
    already holds (it counts as usable, since rclone can reuse it).
    """
    if disk is None:
        du = shutil.disk_usage(cache_dir)
        disk = (du.total, du.free)
    if cached is None:
        cached = dir_usage(cache_dir) or 0
    mem = mem or mem_total() or 4 * GB
    cpus = cpus or os.cpu_count() or 2
    total, free = disk

    # Keep 10% of the disk free, between 5 and 20 GB, for the OS, Plex and transcodes.
    floor = round(max(5 * GB, min(20 * GB, total * 0.10)) / GB) * GB
    cache = max(5 * GB, int((free + cached - floor) * 0.60) // GB * GB)
    # About 1/64 of RAM per stream: ~128M on 8 GB, ~512M on 32 GB.
    buffer = _nearest_pow2_mb(mem / 64, 32 * MB, 512 * MB)
    # Read ahead up to 1/16 of the cache, so a few streams can't fill it.
    read_ahead = _nearest_pow2_mb(cache / 16, 256 * MB, 4 * GB)
    max_age = "72h" if cache < 50 * GB else "7d" if cache < 200 * GB else "14d"
    return {
        "mount": {
            "CACHE_PERCENT": "60",
            "CACHE_MAX_SIZE": _size_str(cache),
            "CACHE_MIN_FREE": _size_str(floor),
            "CACHE_MAX_AGE": max_age,
            "READ_AHEAD": _size_str(read_ahead),
            "BUFFER_SIZE": _size_str(buffer),
        },
        "prefetch": {
            # Each upcoming episode can be several GB; keep them within the cache.
            "next_episodes": "1" if cache < 30 * GB else "2" if cache < 80 * GB else "3",
            "workers": "1" if cpus <= 2 else "2",
            "min_free_gb": str(floor // GB),
        },
        "basis": (f"{_gb(total)} disk with {_gb(free)} free, {_gb(mem)} RAM, "
                  f"{cpus} CPU cores"),
    }


def validate_prefetch(form: dict) -> dict:
    out = {}
    for key, label, kind, lo, hi, _help in PREFETCH_FIELDS:
        if kind == "bool":
            out[key] = "true" if form.get(key) in ("on", "true", "1") else "false"
            continue
        raw = (form.get(key) or "").strip()
        if not raw.isdigit() or not lo <= int(raw) <= hi:
            raise SettingsError(f"{label} must be a whole number from {lo} to {hi}.")
        out[key] = raw
    return out


def validate_flags(raw: str) -> str:
    tokens = raw.split()
    for t in tokens:
        if not FLAG_TOKEN_RE.match(t):
            raise SettingsError(f"Extra rclone flags: can't use {t!r}. Use plain flags "
                                "like --bwlimit 80M.")
        if t.startswith("-") and t.split("=", 1)[0].startswith(BLOCKED_FLAGS):
            raise SettingsError(f"Extra rclone flags: {t.split('=', 1)[0]} can't be set here.")
    if tokens and not tokens[0].startswith("-"):
        raise SettingsError("Extra rclone flags must start with a flag such as --bwlimit.")
    return " ".join(tokens)


def validate_mount(form: dict) -> dict:
    out = {}
    for key, label, kind, _help in MOUNT_FIELDS + RCLONE_FIELDS:
        if kind == "flags":
            out[key] = validate_flags(form.get(key) or "")
            continue
        raw = (form.get(key) or "").strip() or RCLONE_DEFAULTS.get(key, "")
        raw = raw.upper() if kind == "size" else raw
        if kind == "percent":
            if raw and (not raw.isdigit() or not 1 <= int(raw) <= 100):
                raise SettingsError(f"{label} must be from 1 to 100, or blank.")
        elif kind == "size" and not SIZE_RE.match(raw):
            if key == "CACHE_MAX_SIZE" and not raw and form.get("CACHE_PERCENT", "").strip():
                pass
            else:
                raise SettingsError(f"{label} must be a size like 512M or 20G.")
        elif kind == "duration" and not DURATION_RE.match(raw):
            raise SettingsError(f"{label} must be a duration like 72h, 7d or 2w.")
        out[key] = raw
    return out


def size_from_percent(cache_dir: str, percent: int, min_free: int) -> str:
    """Same rule as plex-cache-resize: % of (free + already cached - floor)."""
    free = shutil.disk_usage(cache_dir).free
    usable = free + (dir_usage(cache_dir, max_age=0) or 0) - min_free
    return f"{max(5, int(usable * percent / 100 / 1024 / MB))}G"


def update_ini(path: str, values: dict) -> None:
    """Set keys in the [plex] section, keeping comments and layout."""
    with open(path) as f:
        lines = f.read().splitlines()
    section, seen = None, set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*\[([^]]+)\]", line)
        if m:
            section = m.group(1)
            continue
        m = re.match(r"^\s*([A-Za-z_]+)\s*=", line)
        if section == "plex" and m and m.group(1) in values:
            lines[i] = f"{m.group(1)} = {values[m.group(1)]}"
            seen.add(m.group(1))
    missing = [k for k in values if k not in seen]
    if missing:
        at = next((i for i, l in enumerate(lines) if l.strip() == "[plex]"), None)
        new = [f"{k} = {values[k]}" for k in missing]
        if at is None:
            lines = ["[plex]"] + new + lines
        else:
            lines[at + 1:at + 1] = new
    _write_in_place(path, "\n".join(lines) + "\n")


def env_quote(value: str) -> str:
    # The file is read by systemd and sourced by bash; unquoted spaces would
    # make bash run the rest as a command. Values never contain " $ ` or \.
    return f'"{value}"' if re.search(r"\s", value) else value


def update_env(path: str, values: dict) -> None:
    """Set KEY=value lines in an env file; a blank value removes the key."""
    with open(path) as f:
        lines = f.read().splitlines()
    out, seen = [], set()
    for line in lines:
        m = re.match(r"^([A-Z_]+)=", line)
        if m and m.group(1) in values:
            seen.add(m.group(1))
            if values[m.group(1)] == "":
                continue
            line = f"{m.group(1)}={env_quote(values[m.group(1)])}"
        out.append(line)
    out += [f"{k}={env_quote(v)}" for k, v in values.items() if k not in seen and v != ""]
    _write_in_place(path, "\n".join(out) + "\n")


def _write_in_place(path: str, text: str) -> None:
    # In place rather than rename: the service user owns these files but not
    # /etc/plex-precache itself.
    with open(path, "r+") as f:
        f.write(text)
        f.truncate()


def reload_config(cfg: Config) -> None:
    fresh = Config(cfg.path)
    cfg.__dict__.update(fresh.__dict__)


def systemctl_restart(unit: str) -> bool:
    """Needs the sudo rule install.sh adds; never prompts for a password."""
    try:
        return subprocess.run(["sudo", "-n", "systemctl", "restart", unit],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def restart_mount_or_revert(backups: dict) -> str:
    """Restart the mount; if it won't come up, restore the files and restart again.

    backups maps each changed file to its previous contents. Returns "ok",
    "reverted" or "no-restart" (no sudo rule).
    """
    try:
        res = subprocess.run(["sudo", "-n", "systemctl", "restart", "plex-cache-mount"],
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return "no-restart"
    if res.returncode == 0:
        return "ok"
    if res.stderr.lstrip().startswith("sudo"):
        return "no-restart"
    log.warning("plex-cache-mount failed to start with the new settings; putting back the old ones")
    for path, text in backups.items():
        _write_in_place(path, text)
    systemctl_restart("plex-cache-mount")
    return "reverted"


SFTP_SECTION = "plexcache-sftp"  # the remote install.sh --sftp writes
CONF_DIR = "/etc/plex-precache"
HOST_RE = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?|[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*)$")
USER_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,63}$")
# Safe both in rclone and when root scripts source the env file.
REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9 ._/@%+,:~-]*$")


def sftp_settings(cfg: Config) -> dict | None:
    """The SFTP connection, when it's the one install.sh --sftp set up."""
    env = read_env_file(cfg.mount_env) if cfg.mount_env else {}
    remote, conf = env.get("REMOTE", ""), env.get("RCLONE_CONFIG", "")
    if not remote.startswith(SFTP_SECTION + ":") or not conf:
        return None
    cp = configparser.RawConfigParser()
    try:
        cp.read(conf)
    except configparser.Error:
        return None
    if not cp.has_section(SFTP_SECTION):
        return None
    sec = cp[SFTP_SECTION]
    return {"host": sec.get("host", ""), "user": sec.get("user", ""),
            "port": sec.get("port", "22"), "path": remote.split(":", 1)[1],
            "mountpoint": env.get("MOUNTPOINT", ""), "conf": conf,
            "key_file": sec.get("key_file", ""), "parser": cp}


def check_mountpoint(new: str, current: str) -> None:
    if new == current:
        return
    if not new.startswith("/") or not REMOTE_PATH_RE.match(new) or new.rstrip("/") == "":
        raise SettingsError("Mount at must be a full folder path such as /data.")
    if not os.path.isdir(new):
        raise SettingsError(f"{new} doesn't exist. Create it first: sudo mkdir {new} && "
                            f"sudo chown $(whoami): {new}")
    if os.path.ismount(new) or os.listdir(new):
        raise SettingsError(f"{new} isn't empty or already has something mounted on it.")
    if not os.access(new, os.W_OK):
        raise SettingsError(f"Plex-Cache can't mount on {new}; give its user ownership: "
                            f"sudo chown <user>: {new}")


def validate_connection(form: dict, cur: dict) -> dict:
    v = {k: (form.get(k) or "").strip() for k in ("host", "user", "port", "path", "mountpoint")}
    if not HOST_RE.match(v["host"]):
        raise SettingsError("Server must be a hostname or IP address.")
    if not USER_RE.match(v["user"]):
        raise SettingsError("User must be a plain user name.")
    if not v["port"].isdigit() or not 1 <= int(v["port"]) <= 65535:
        raise SettingsError("Port must be a number from 1 to 65535.")
    if not REMOTE_PATH_RE.match(v["path"]):
        raise SettingsError("Folder on the server must be a full path such as /home/me/data.")
    v["mountpoint"] = v["mountpoint"].rstrip("/") or "/"
    check_mountpoint(v["mountpoint"], cur["mountpoint"])
    key = (form.get("key") or "").replace("\r\n", "\n").strip()
    if key and not (key.startswith("-----BEGIN") and "PRIVATE KEY-----" in key):
        raise SettingsError("The SSH key must be a private key, starting with -----BEGIN.")
    v["key"] = key + "\n" if key else ""
    return v


def known_hosts_has(path: str, host: str, port: str) -> bool:
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return False
    if re.search(rf"(^|,|\[){re.escape(host)}[\],\s]", text, re.M):
        return True
    name = host if port == "22" else f"[{host}]:{port}"
    try:  # hashed entries
        return subprocess.run(["ssh-keygen", "-F", name, "-f", path], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def connection_conf(cur: dict, v: dict, key_file: str) -> tuple[str, bool]:
    """rclone.conf text for the new settings, and whether the host key is checked."""
    cp = configparser.RawConfigParser()
    cp.read_dict({s: dict(cur["parser"][s]) for s in cur["parser"].sections()})
    sec = cp[SFTP_SECTION]
    sec["host"], sec["user"], sec["port"], sec["key_file"] = v["host"], v["user"], v["port"], key_file
    kh = sec.get("known_hosts_file") or os.path.join(CONF_DIR, "known_hosts")
    verified = known_hosts_has(kh, v["host"], v["port"])
    if verified:
        sec["known_hosts_file"] = kh
    else:
        sec.pop("known_hosts_file", None)
    out = io.StringIO()
    cp.write(out)
    return out.getvalue(), verified


def rclone_bin() -> str:
    return shutil.which("rclone") or "/usr/bin/rclone"


def test_connection(cur: dict, v: dict) -> tuple[bool, str, bool]:
    """List the remote folder with the new settings without touching the live ones."""
    remote = f"{SFTP_SECTION}:{v['path']}"
    with tempfile.TemporaryDirectory(prefix="plexcache-test-") as tmp:
        key_file = cur["key_file"]
        if v["key"]:
            key_file = os.path.join(tmp, "key")
            with open(os.open(key_file, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
                f.write(v["key"])
        text, verified = connection_conf(cur, v, key_file)
        conf = os.path.join(tmp, "rclone.conf")
        with open(os.open(conf, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write(text)
        try:
            res = subprocess.run([rclone_bin(), "--config", conf, "lsf", "--max-depth", "1",
                                  "--contimeout", "15s", "--timeout", "30s", "--retries", "1",
                                  "--low-level-retries", "1", remote],
                                 capture_output=True, text=True, timeout=90)
        except (OSError, subprocess.TimeoutExpired) as err:
            return False, f"Couldn't connect to {v['host']}: {err}.", verified
    if res.returncode != 0:
        lines = [l for l in res.stderr.splitlines() if l.strip()]
        why = re.sub(r"^\S+ \S+ (ERROR|NOTICE|Failed)\s*:\s*", "", lines[-1] if lines else "")
        return False, f"Couldn't connect to {v['host']}: {why[:300] or 'rclone failed'}.", verified
    n = len([l for l in res.stdout.splitlines() if l.strip()])
    return True, f"Connected to {v['host']}: {n} item(s) in {v['path']}.", verified


def apply_connection(cfg: Config, form: dict) -> str:
    cur = sftp_settings(cfg)
    if not cur:
        raise SettingsError("The connection wasn't set up by Plex-Cache, so it can't be "
                            "changed here. Run install.sh --sftp to set it up.")
    v = validate_connection(form, cur)
    ok, msg, verified = test_connection(cur, v)
    if not verified:
        msg += (f" Note: there's no known_hosts entry for {v['host']}, so its host key "
                "isn't checked.")
    if not ok:
        raise SettingsError(msg + (" Nothing was changed." if form.get("do") != "test" else ""))
    if form.get("do") == "test":
        return msg
    key_file = cur["key_file"]
    if v["key"] and os.path.dirname(key_file) != CONF_DIR:
        raise SettingsError("A new key can only be saved when the key lives in "
                            f"{CONF_DIR}. Re-run install.sh --sftp with --ssh-key instead.")
    backups = {}
    for path in [cur["conf"], cfg.mount_env] + ([key_file] if v["key"] else []):
        with open(path) as f:
            backups[path] = f.read()
    if v["key"]:
        _write_in_place(key_file, v["key"])
    _write_in_place(cur["conf"], connection_conf(cur, v, key_file)[0])
    update_env(cfg.mount_env, {"REMOTE": f"{SFTP_SECTION}:{v['path']}",
                               "MOUNTPOINT": v["mountpoint"]})
    result = restart_mount_or_revert(backups)
    if result == "reverted":
        raise SettingsError("The connection test passed, but the mount wouldn't start with "
                            "the new settings, so the previous ones were put back. See: "
                            "journalctl -u plex-cache-mount")
    if v["mountpoint"] != cur["mountpoint"] and result == "ok":
        try:  # rclone normally unmounts the old folder itself when it stops
            subprocess.run(["fusermount", "-uz", cur["mountpoint"]], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
        msg += (f" Media is now at {v['mountpoint']}; point your Plex libraries there.")
    if result == "no-restart":
        return (msg + " Saved, but the mount couldn't be restarted from here. Run: "
                "sudo systemctl restart plex-cache-mount")
    return msg + " Saved and the mount restarted."


def apply_prefetch_settings(cfg: Config, pf: "Prefetcher", form: dict) -> str:
    values = validate_prefetch(form)
    update_ini(cfg.path, values)
    reload_config(cfg)
    pf.ensure_workers()
    return "Pre-cache settings saved and applied."


def apply_mount_settings(cfg: Config, form: dict) -> str:
    values = validate_mount(form)
    if values["CACHE_PERCENT"]:
        floor = parse_size(values["CACHE_MIN_FREE"]) or 0
        values["CACHE_MAX_SIZE"] = size_from_percent(cfg.cache_dir, int(values["CACHE_PERCENT"]),
                                                     floor)
    with open(cfg.mount_env) as f:
        before = f.read()
    update_env(cfg.mount_env, values)
    # The prefetcher's pre-cache floor follows the mount's.
    floor_gb = max(0, round((parse_size(values["CACHE_MIN_FREE"]) or 0) / 1024 / MB))
    update_ini(cfg.path, {"min_free_gb": str(floor_gb)})
    reload_config(cfg)
    if form.get("restart") == "now":
        result = restart_mount_or_revert({cfg.mount_env: before})
        if result == "ok":
            return f"Cache settings saved (size {values['CACHE_MAX_SIZE']}) and the mount restarted."
        if result == "reverted":
            raise SettingsError("The mount wouldn't start with those settings, so the previous "
                                "ones were put back. Check the extra rclone flags.")
        return ("Cache settings saved, but the mount couldn't be restarted from here. "
                "Run: sudo systemctl restart plex-cache-mount")
    return (f"Cache settings saved (size {values['CACHE_MAX_SIZE']}). They apply the next time "
            "the mount restarts.")


def apply_recommended(cfg: Config, pf: "Prefetcher", form: dict) -> str:
    rec = recommend_settings(cfg.cache_dir)
    pre = {key: str(getattr_current(cfg, key)) for key, *_ in PREFETCH_FIELDS}
    pre.update({k: v for k, v in rec["prefetch"].items() if k in pre})
    apply_prefetch_settings(cfg, pf, pre)
    if read_env_file(cfg.mount_env):
        current = read_env_file(cfg.mount_env)
        return apply_mount_settings(cfg, {**current, **rec["mount"],
                                          "restart": form.get("restart", "")}
                                    ).replace("Cache settings", "Recommended settings")
    update_ini(cfg.path, {"min_free_gb": rec["prefetch"]["min_free_gb"]})
    reload_config(cfg)
    return "Recommended settings saved and applied."


def getattr_current(cfg: Config, key: str):
    """A [plex] setting's current value, in the units the config file uses."""
    return {
        "next_episodes": cfg.next_episodes, "next_episode_head_mb": cfg.next_episode_head // MB,
        "on_deck": "true" if cfg.on_deck else "", "on_deck_limit": cfg.on_deck_limit,
        "on_deck_head_mb": cfg.on_deck_head // MB, "workers": cfg.workers,
        "poll_interval": int(cfg.poll_interval), "remember_hours": int(cfg.remember / 3600),
        "stop_after_minutes": int(cfg.stop_after / 60),
    }[key]


UPDATE_REPO = "https://github.com/Maffewem/Plex-Cache"
INSTALL_DIR = "/opt/plex-precache"


def installed_version() -> str:
    try:
        with open(os.path.join(INSTALL_DIR, "VERSION")) as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def last_update_note() -> str:
    try:
        with open(os.path.join(INSTALL_DIR, "last-update")) as f:
            return f.read().strip()
    except OSError:
        return ""


def version_label(version: str = __version__, released: str = __released__) -> str:
    """'1.1.0 (10 Oct 2026)'."""
    try:
        day = time.strftime("%-d %b %Y", time.strptime(released, "%Y-%m-%d"))
    except ValueError:
        return version
    return f"{version} ({day})"


def latest_release() -> tuple[str, str] | None:
    """(version, released) of the newest version on GitHub, read from its source."""
    url = UPDATE_REPO.replace("github.com", "raw.githubusercontent.com") + \
        "/main/plex_precache.py"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            head = resp.read(4096).decode("utf-8", "replace")
    except OSError:
        return None
    v = re.search(r'^__version__ = "([^"]+)"', head, re.M)
    r = re.search(r'^__released__ = "([^"]+)"', head, re.M)
    return (v.group(1), r.group(1) if r else "") if v else None


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def check_for_update() -> str:
    try:
        out = subprocess.run(["git", "ls-remote", UPDATE_REPO, "refs/heads/main"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    latest = out[:7]
    if not latest:
        raise SettingsError("Couldn't reach GitHub to check for updates.")
    if latest == installed_version():
        return f"You're on the latest version, {version_label()}."
    rel = latest_release()
    if rel and version_tuple(rel[0]) < version_tuple(__version__):
        return (f"You're on {version_label()}, which is newer than the released "
                f"version ({rel[0]}). Nothing to update.")
    name = f"Version {version_label(*rel)}" if rel and rel[0] != __version__ else \
        f"A newer build ({latest})"
    return f"{name} is available; you have {version_label()}. Use Update now to install it."


def start_update() -> str:
    try:
        ok = subprocess.run(["sudo", "-n", "systemctl", "start", "--no-block",
                             "plex-cache-update"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        raise SettingsError("Couldn't start the update from here. Run: sudo plex-cache-update")
    return ("Updating in the background. This page will say Reconnecting for a few seconds "
            "while Plex-Cache restarts; reload it afterwards to see the new version.")


SWITCH_SCRIPT = "/usr/local/sbin/plex-cache-switch"
DISABLED_MARK = "# disabled by Plex-Cache: "


def media_mount(cfg: Config, mounts_text: str | None = None) -> dict:
    """What the media folder is mounted with, and which switch makes sense."""
    if mounts_text is None:
        try:
            with open("/proc/mounts") as f:
                mounts_text = f.read()
        except OSError:
            mounts_text = ""
    env = read_env_file(cfg.mount_env) if cfg.mount_env else {}
    where = (env.get("MOUNTPOINT") or "").rstrip("/")
    mnt, fstype, _ = mount_info(where, mounts_text) if where else ("", "", "")
    if mnt != where:
        fstype = ""  # nothing mounted there; maybe the SSHFS mount is somewhere else
        sshfs = [_unescape_mount(l.split()[1]) for l in mounts_text.splitlines()
                 if len(l.split()) > 2 and l.split()[2] == "fuse.sshfs"]
        if len(sshfs) == 1:
            where, fstype = sshfs[0], "fuse.sshfs"
    try:
        with open("/etc/fstab") as f:
            undo = any(l.startswith(DISABLED_MARK) for l in f)
    except OSError:
        undo = False
    try:
        with open(os.path.join(INSTALL_DIR, "last-switch")) as f:
            note = f.read().strip()
    except OSError:
        note = ""
    return {"where": where, "fstype": fstype, "undo": undo or fstype == "fuse.rclone",
            "note": note}


def switch_check() -> str:
    """Run the switch's read-only test (sudo rule allows exactly this)."""
    try:
        res = subprocess.run(["sudo", "-n", SWITCH_SCRIPT, "--check"], capture_output=True,
                             text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise SettingsError("The test took too long; the server may be unreachable.")
    except OSError:
        res = None
    if res is None or res.stderr.lstrip().startswith("sudo"):
        raise SettingsError("Couldn't run the test from here. Run: sudo plex-cache-switch --check")
    lines = [l for l in (res.stdout + "\n" + res.stderr).splitlines() if l.strip()]
    if res.returncode != 0:
        raise SettingsError(lines[-1] if lines else "The test failed.")
    return lines[-1] if lines else "Test passed."


def start_switch(back: bool) -> str:
    unit = "plex-cache-switch-back" if back else "plex-cache-switch"
    try:
        ok = subprocess.run(["sudo", "-n", "systemctl", "start", "--no-block", unit],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        raise SettingsError("Couldn't start it from here. Run: sudo plex-cache-switch"
                            + (" --back" if back else ""))
    return ("Going back to SSHFS" if back else "Switching to the cached mount") + \
        (". Plex pauses for a moment. Reload this page in a minute to see how it went.")


def set_auto_update(cfg: Config, form: dict) -> str:
    on = form.get("auto_update") in ("on", "true", "1")
    update_ini(cfg.path, {"auto_update": "true" if on else "false"})
    reload_config(cfg)
    return ("Automatic updates on: new versions install around 04:30 each night."
            if on else "Automatic updates off.")


def warm_path(cfg: Config, pf: "Prefetcher", rel: str) -> str:
    """Queue a file or folder under the mount for pre-caching."""
    root = read_env_file(cfg.mount_env).get("MOUNTPOINT") or ""
    if not root:
        raise SettingsError("Pre-caching a folder needs the cached mount set up.")
    target = os.path.realpath(os.path.join(root, rel.strip().lstrip("/")))
    if os.path.commonpath([target, os.path.realpath(root)]) != os.path.realpath(root):
        raise SettingsError("That path is outside the media folder.")
    if not os.path.exists(target):
        raise SettingsError("Nothing found at that path.")
    files = [target] if os.path.isfile(target) else list(itertools.islice(
        (os.path.join(d, n) for d, _s, ns in os.walk(target) for n in sorted(ns)), 500))
    for f in files:
        pf.submit(f, prio=PRIO_MANUAL)
    return f"Queued {len(files)} file(s) from {rel or '/'} for pre-caching."


def render_settings(cfg: Config, token: str, msg: str | None, ok: bool,
                    prefill: dict | None = None) -> str:
    e = html.escape
    env = read_env_file(cfg.mount_env) if cfg.mount_env else {}
    has_mount = bool(env)

    try:
        rec = recommend_settings(cfg.cache_dir) if cfg.cache_dir and os.path.isdir(
            cfg.cache_dir) else None
    except OSError:
        rec = None
    rec_all = {**rec["mount"], **rec["prefetch"]} if rec else {}

    def row(label: str, field: str, help_: str, key: str = "") -> str:
        hint = f"<div class=muted>{e(help_)}</div>" if help_ else ""
        if key in rec_all and key != "CACHE_PERCENT":
            hint += f"<div class=rec>Recommended for this server: {e(rec_all[key])}</div>"
        return f"<label class=field><span>{e(label)}</span>{field}{hint}</label>"

    current = {key: getattr_current(cfg, key) for key, *_ in PREFETCH_FIELDS}
    pre = ""
    for key, label, kind, lo, hi, help_ in PREFETCH_FIELDS:
        if kind == "bool":
            field = (f"<input type=checkbox name={key}"
                     f"{' checked' if current[key] else ''}>")
        else:
            field = (f"<input type=number name={key} min={lo} max={hi} required "
                     f"value='{current[key]}'>")
        pre += row(label, field, help_, key)
    mount = ""
    if has_mount:
        for key, label, kind, help_ in MOUNT_FIELDS:
            field = (f"<input name={key} value='{e(env.get(key, ''))}' "
                     f"{'inputmode=numeric ' if kind == 'percent' else ''}"
                     f"autocomplete=off spellcheck=false>")
            mount += row(label, field, help_, key)
        mount += "<h4>Advanced rclone settings</h4>"
        for key, label, kind, help_ in RCLONE_FIELDS:
            value = env.get(key) or RCLONE_DEFAULTS.get(key, "")
            field = (f"<input name={key} value='{e(value)}' "
                     f"autocomplete=off spellcheck=false>")
            mount += row(label, field, help_, key)
    banner = (f"<p class='notice {'good' if ok else 'bad'}'>{e(msg)}</p>" if msg else "")
    hidden = f"<input type=hidden name=token value='{token}'>"
    mount_form = f"""
<form method=post action=settings/mount class=panel>{hidden}
<h3>Cache</h3>{mount}
<label class=check><input type=checkbox name=restart value=now checked>
Restart the mount now so changes apply (anything playing pauses for a few seconds)</label>
<button>Save cache settings</button></form>
<form method=post action=restart-mount class=inline>{hidden}
<button>Restart mount only</button></form>""" if has_mount else ""
    conn_form = ""
    sftp = sftp_settings(cfg) if has_mount else None
    if sftp:
        vals = {**sftp, **(prefill or {})}

        def inp(name: str, label: str, help_: str, extra: str = "") -> str:
            return row(label, f"<input name={name} value='{e(str(vals.get(name, '')))}' "
                       f"autocomplete=off spellcheck=false {extra}>", help_)
        conn_form = f"""
<form method=post action=settings/connection class=panel>{hidden}
<h3>Connection to your media server</h3>
{inp("host", "Server", "Hostname or IP address of the server holding your media.")}
{inp("user", "User", "")}
{inp("port", "SSH port", "", "inputmode=numeric")}
{inp("path", "Folder on the server", "Full path, e.g. /home/me/data.")}
{inp("mountpoint", "Mount at", "Where the media appears on this box. Your Plex libraries "
     "point here, so only change it if you'll update them too.")}
{row("New SSH private key", "<textarea name=key rows=3 placeholder='Leave blank to keep the current key' spellcheck=false></textarea>",
     "Only needed if the server's key changed. Paste it again when saving after a test.")}
<button name=do value=test>Test connection</button>
<button name=do value=save>Test and save</button>
<p class=muted>Saving tests the connection first and changes nothing if it fails. It then
restarts the mount, so anything playing pauses for a few seconds; if the mount won't start,
the previous settings are put back.</p></form>"""
    elif has_mount and env.get("REMOTE"):
        conn_form = f"""<div class=panel><h3>Connection to your media server</h3>
<p>rclone remote <code>{e(env["REMOTE"])}</code>, mounted at <code>{e(env.get("MOUNTPOINT", ""))}</code>.</p>
<p class=muted>This remote was set up outside Plex-Cache, so it's changed with
<code>rclone config</code>. To manage an SFTP connection here, run
<code>sudo ./install.sh --sftp user@host:/path --ssh-key KEY</code>.</p></div>"""
    switch_form = ""
    mm = media_mount(cfg) if has_mount else None
    note_html = (f"<p class=muted>Last switch: {e(mm['note'])}</p>" if mm and mm["note"] else "")
    if mm and mm["fstype"] == "fuse.sshfs":
        switch_form = f"""
<div class='panel badbox'><h3>Switch {e(mm["where"])} from SSHFS to the cached mount</h3>
<p>Your media is on SSHFS, which can't keep anything on local disk, so pre-caching is paused.
The switch reads the server, folder and SSH key from the SSHFS mount, checks rclone sees the
same folders, then swaps the mounts. Plex paths don't change. Plex pauses for a moment, and if
the new mount doesn't show your files, SSHFS is put back.</p>
<form method=post action=switch/check class=inline>{hidden}<button>Test first</button></form>
<form method=post action=switch/run class=inline>{hidden}<button>Switch now</button></form>
{note_html}</div>"""
    elif mm and mm["undo"]:
        switch_form = f"""
<div class=panel><h3>Media mount</h3>
<p>{e(mm["where"])} {"is the cached rclone mount." if mm["fstype"] == "fuse.rclone" else
  "should be the cached rclone mount, but it isn't running, so Plex sees no media. Check "
  "<code>journalctl -u plex-cache-mount</code>, or go back to SSHFS."}</p>
<form method=post action=switch/back>{hidden}
<button>Go back to SSHFS</button>
<span class=muted>Plex pauses for a moment, and pre-caching stops until you switch again.</span>
</form>{note_html}</div>"""
    warm_form = f"""
<form method=post action=warm class=panel>{hidden}<h3>Pre-cache now</h3>
{row("Folder or file", "<input name=path placeholder='TV/Some Show/Season 1' autocomplete=off>",
     "Path inside " + (env.get("MOUNTPOINT") or "the media folder") + ". Up to 500 files are queued.")}
<button>Pre-cache</button></form>""" if has_mount else ""
    rec_form = f"""
<form method=post action=settings/recommended class='panel recbox'>{hidden}
<h3>Recommended settings</h3>
<p class=muted>Worked out from this server: {e(rec["basis"])}. Cache
{e(rec["mount"]["CACHE_MAX_SIZE"])} (60% of usable disk), keep {e(rec["mount"]["CACHE_MIN_FREE"])}
free, {e(rec["mount"]["BUFFER_SIZE"])} RAM and {e(rec["mount"]["READ_AHEAD"])} read-ahead per
stream, drop files after {e(rec["mount"]["CACHE_MAX_AGE"])} unwatched,
{e(rec["prefetch"]["next_episodes"])} upcoming episode(s), {e(rec["prefetch"]["workers"])}
download(s) at once.</p>
{'<label class=check><input type=checkbox name=restart value=now checked> Restart the mount now (anything playing pauses for a few seconds)</label>' if has_mount else ''}
<button>Use recommended settings</button></form>""" if rec else ""
    note = last_update_note()
    update_form = f"""
<div class=panel><h3>Updates</h3>
<p>Version <b>{e(version_label())}</b>
<span class=muted>· build {e(installed_version())}</span>
{('<br><span class=muted>Last update: ' + e(note) + '</span>') if note else ''}</p>
<form method=post action=update/check class=inline>{hidden}<button>Check for updates</button></form>
<form method=post action=update/run class=inline>{hidden}<button>Update now</button></form>
<form method=post action=update/auto>{hidden}
<label class=check><input type=checkbox name=auto_update{' checked' if cfg.auto_update else ''}
onchange="this.form.submit()"> Install updates automatically each night</label>
<noscript><button>Save</button></noscript></form>
<p class=muted>Updates keep all your settings. Over SSH: <code>sudo plex-cache-update</code>.
<a href="{UPDATE_REPO}/blob/main/CHANGELOG.md" target=_blank rel=noopener>What's new</a></p></div>"""
    return f"""<section id=settings><h2>Settings</h2>{banner}{rec_form}
<form method=post action=settings/prefetch class=panel>{hidden}<h3>Pre-caching</h3>{pre}
<button>Save pre-cache settings</button>
<span class=muted>Applies immediately.</span></form>
{switch_form}{conn_form}{mount_form}{warm_form}{update_form}</section>"""


SETTINGS_CSS = """
#settings .panel{background:var(--card);border-radius:6px;padding:12px;margin:12px 0}
#settings h3{font-size:15px;margin:0 0 8px}.field{display:block;margin:10px 0}
.field span{display:block;font-weight:600;font-size:14px;margin-bottom:3px}
#settings h4{font-size:14px;margin:16px 0 4px}
.field textarea{font:12px monospace;width:100%;max-width:520px;box-sizing:border-box;
background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:4px}
.field input:not([type=checkbox]){font:inherit;padding:5px 8px;width:100%;max-width:260px;
box-sizing:border-box;background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:4px}.check{display:block;margin:10px 0;font-size:14px}
#settings button{margin-top:6px}.rec{color:var(--info);font-size:13px}
.recbox{border-left:4px solid var(--info)}.badbox{border-left:4px solid var(--warn)}.inline{display:inline}
.notice{padding:8px 12px;border-radius:6px;color:#fff}.notice.good{background:var(--good)}
.notice.bad{background:var(--bad)}"""


def start_status_server(cfg: Config, pf: Prefetcher) -> None:
    token = secrets.token_urlsafe(24)  # CSRF token for the settings forms
    last_msg: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            data = status_data(cfg, pf)
            if self.path.rstrip("/").endswith("/api/status"):
                self._send(json.dumps(data).encode(), "application/json")
                return
            msg = last_msg.pop("text", None) if "saved=1" in self.path else None
            ok = last_msg.pop("ok", True)
            prefill = last_msg.pop("conn", None) if msg else None
            settings = render_settings(cfg, token, msg, ok, prefill) if cfg.path else ""
            self._send(render_status(data, settings).encode(), "text/html; charset=utf-8")

        def do_POST(self):
            length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
            form = {k: v[-1] for k, v in urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8", "replace")).items()}
            if not secrets.compare_digest(form.get("token", ""), token):
                self._send(b"Stale or missing form token; reload the page.", "text/plain", 403)
                return
            action = self.path.split("?")[0].rstrip("/").rsplit("/", 2)[-2:]
            try:
                if action == ["settings", "prefetch"]:
                    text = apply_prefetch_settings(cfg, pf, form)
                elif action == ["settings", "mount"]:
                    text = apply_mount_settings(cfg, form)
                elif action == ["settings", "connection"]:
                    # Keep what was typed (not the key) so a test can be followed by a save.
                    last_msg["conn"] = {k: form.get(k, "") for k in
                                        ("host", "user", "port", "path", "mountpoint")}
                    text = apply_connection(cfg, form)
                    if form.get("do") != "test":
                        last_msg.pop("conn", None)
                elif action == ["settings", "recommended"]:
                    text = apply_recommended(cfg, pf, form)
                elif action == ["switch", "check"]:
                    text = switch_check()
                elif action == ["switch", "run"]:
                    text = start_switch(back=False)
                elif action == ["switch", "back"]:
                    text = start_switch(back=True)
                elif action == ["update", "check"]:
                    text = check_for_update()
                elif action == ["update", "run"]:
                    text = start_update()
                elif action == ["update", "auto"]:
                    text = set_auto_update(cfg, form)
                elif action[-1] == "restart-mount":
                    text = ("Mount restarted." if systemctl_restart("plex-cache-mount") else
                            "Couldn't restart the mount from here. "
                            "Run: sudo systemctl restart plex-cache-mount")
                elif action[-1] == "warm":
                    text = warm_path(cfg, pf, form.get("path", ""))
                else:
                    self._send(b"Not found", "text/plain", 404)
                    return
                last_msg.update(text=text, ok=not text.startswith("Couldn't")
                                and "couldn't" not in text)
                log.info("web: %s", text)
            except (SettingsError, OSError) as err:
                last_msg.update(text=str(err), ok=False)
            # Back to the page; relative so it works behind nginx's /plexcache/ prefix.
            depth = self.path.split("?")[0].strip("/").count("/")
            self.send_response(303)
            self.send_header("Location", "../" * depth + "./?saved=1#settings")
            self.end_headers()

        def log_message(self, *_args):
            pass

    try:
        srv = http.server.ThreadingHTTPServer((cfg.status_bind, cfg.status_port), Handler)
    except OSError as e:
        log.warning("status page disabled: %s", e)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("status page on http://%s:%d/", cfg.status_bind, cfg.status_port)


def warm(cfg: Config, paths: list[str]) -> None:
    """One-shot: cache the given files, or every file under the given directories."""
    pf = Prefetcher(cfg)
    signal.signal(signal.SIGINT, pf.shutdown)
    for p in paths:
        if os.path.isdir(p):
            for dirpath, _dirs, files in os.walk(p):
                for name in sorted(files):
                    pf.submit(os.path.join(dirpath, name))
        else:
            pf.submit(p)
    threads = [threading.Thread(target=pf.worker, daemon=True) for _ in range(cfg.workers)]
    for t in threads:
        t.start()
    while not pf.stop.is_set():
        with pf.cv:
            if not pf.active:
                break
        time.sleep(0.5)
    pf.shutdown()
    for t in threads:
        t.join(timeout=5)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-c", "--config", default="/etc/plex-precache/config.ini")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--warm", nargs="+", metavar="PATH",
                    help="cache these files/directories now and exit")
    ap.add_argument("--version", action="version",
                    version=f"Plex-Cache {version_label()}")
    ap.add_argument("--recommend", metavar="CACHE_DIR",
                    help="print settings suited to this server as KEY=VALUE lines and exit")
    args = ap.parse_args(argv)
    if args.recommend:
        rec = recommend_settings(args.recommend)
        for k, v in {**rec["mount"], **rec["prefetch"]}.items():
            print(f"{k}={v}")
        return
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # The default config path is optional; an explicit -c must exist.
    explicit = args.config != ap.get_default("config")
    cfg = Config(args.config if explicit or os.path.exists(args.config) else None)
    if args.warm:
        warm(cfg, args.warm)
    else:
        run_daemon(cfg)


if __name__ == "__main__":
    main(sys.argv[1:])
