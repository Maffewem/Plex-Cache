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
import json
import itertools
import shutil
import logging
import os
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

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
}

# Lower number runs first.
PRIO_PLAYING = 0
PRIO_NEXT = 1
PRIO_ON_DECK = 2

MB = 1024 * 1024


class Config:
    def __init__(self, path: str | None):
        cp = configparser.ConfigParser()
        cp.optionxform = str  # keep path_map keys case-sensitive
        cp.read_dict({"plex": DEFAULTS, "path_map": {}})
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
    def __init__(self, path: str, start: int, end: int | None, prio: int):
        self.path = path
        self.pos = start
        self.end = end  # None means read to EOF
        self.prio = prio
        self.start = start
        self.size = _size_or_zero(path)
        self.started_at: float | None = None
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
        while not stop.is_set():
            with job.lock:
                pos = job.pos
                end = job.end
            if end is not None and pos >= end:
                break
            want = block if end is None else min(block, end - pos)
            f.seek(pos)
            data = f.read(want)
            if not data:
                break
            with job.lock:
                # A seek-ahead may have moved pos while we were reading.
                if job.pos == pos:
                    job.pos = pos + len(data)
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

    def submit(self, path: str, start: int = 0, end: int | None = None,
               prio: int = PRIO_PLAYING) -> None:
        with self.cv:
            job = self.active.get(path)
            if job is not None:
                job.jump_to(start)
                return
            finished = self.done.get(path)
            if finished and time.time() - finished < self.cfg.remember:
                return
            if not os.path.isfile(path):
                if path not in self.warned:
                    self.warned.add(path)
                    log.warning("not found locally (check [path_map]): %s", path)
                return
            if prio != PRIO_PLAYING and not self.has_room(path, start, end):
                return
            job = Job(path, start, end, prio)
            self.active[path] = job
            heapq.heappush(self.queue, (prio, next(self.counter), job))
            self.cv.notify()
            log.info("queued %s from %d MB (prio %d)", path, start // MB, prio)

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

    def worker(self) -> None:
        while not self.stop.is_set():
            with self.cv:
                while not self.queue and not self.stop.is_set():
                    self.cv.wait(timeout=1)
                if self.stop.is_set():
                    return
                _, _, job = heapq.heappop(self.queue)
            started = job.started_at = time.time()
            ok = False
            try:
                n = read_through(job, self.cfg.read_block, self.stop)
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
                    self.active.pop(job.path, None)
                    # Failed reads are retried on the next poll.
                    if ok and not self.stop.is_set():
                        self.done[job.path] = time.time()

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
                "kind": {PRIO_PLAYING: "playing", PRIO_NEXT: "next episode",
                         PRIO_ON_DECK: "on deck"}.get(job.prio, str(job.prio)),
                "state": "reading" if job.started_at else "queued",
                "percent": round(100 * min(1.0, max(0, pos - job.start) / span)),
                "mb_s": (round((pos - job.start) / MB / max(1, time.time() - job.started_at), 1)
                         if job.started_at else None),
            })
        polled, err = self.last_poll
        return {"jobs": rows, "recent": list(self.recent), "sessions": self.sessions,
                "last_poll": polled, "poll_error": err}

    def shutdown(self, *_args) -> None:
        self.stop.set()
        with self.cv:
            self.cv.notify_all()


def poll_once(plex: Plex, cfg: Config, pf: Prefetcher) -> None:
    sessions = parse_sessions(plex.get("/status/sessions"))
    pf.sessions = [{"title": s["title"], "state": s["state"], "file": s["file"]}
                   for s in sessions]
    for s in sessions:
        path = cfg.local_path(s["file"])
        size = s["size"] or _size_or_zero(path)
        start = max(0, byte_offset(s["view_offset"], s["duration"], size) - cfg.seek_margin)
        pf.submit(path, start=start, prio=PRIO_PLAYING)

        if s["type"] == "episode" and s["show_key"] and cfg.next_episodes > 0:
            try:
                leaves = plex.get(f"/library/metadata/{s['show_key']}/allLeaves")
            except OSError as e:
                log.warning("could not list episodes for %s: %s", s["title"], e)
                continue
            for f in next_episode_files(leaves, s["rating_key"], cfg.next_episodes):
                pf.submit(cfg.local_path(f), end=cfg.next_episode_head or None,
                          prio=PRIO_NEXT)

    if cfg.on_deck:
        for f in on_deck_files(plex.get("/library/onDeck"), cfg.on_deck_limit):
            pf.submit(cfg.local_path(f), end=cfg.on_deck_head or None, prio=PRIO_ON_DECK)


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
    threads = [threading.Thread(target=pf.worker, daemon=True) for _ in range(cfg.workers)]
    for t in threads:
        t.start()
    if cfg.status_port:
        start_status_server(cfg, pf)
    log.info("watching %s every %.0fs with %d workers",
             cfg.plex_url, cfg.poll_interval, cfg.workers)
    while not pf.stop.is_set():
        try:
            poll_once(plex, cfg, pf)
            pf.last_poll = (time.time(), None)
        except (OSError, ET.ParseError) as e:
            log.warning("poll failed: %s", e)
            pf.last_poll = (time.time(), str(e))
        pf.forget_expired()
        pf.stop.wait(cfg.poll_interval)
    for t in threads:
        t.join(timeout=5)


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
        "min_free_gb": cfg.min_free / 1024 / MB,
        "mount": {k: env.get(k) for k in ("REMOTE", "MOUNTPOINT", "CACHE_MAX_SIZE",
                                          "CACHE_MIN_FREE", "CACHE_MAX_AGE",
                                          "READ_AHEAD", "BUFFER_SIZE")},
    }
    return data


def _gb(n: int | None) -> str:
    return "?" if n is None else f"{n / 1024 / MB:.1f} GB"


def render_status(d: dict) -> str:
    e = html.escape
    c = d["cache"]
    m = c["mount"]
    rows = "".join(
        f"<tr><td>{e(os.path.basename(j['path']))}</td><td>{e(j['kind'])}</td>"
        f"<td>{e(j['state'])}</td><td><progress max=100 value={j['percent']}></progress> "
        f"{j['percent']}%</td><td>{'' if j['mb_s'] is None else j['mb_s']}</td></tr>"
        for j in d["jobs"]) or "<tr><td colspan=5>Nothing being cached right now</td></tr>"
    recent = "".join(
        f"<tr><td>{e(os.path.basename(r['path']))}</td><td>{r['mb']} MB</td>"
        f"<td>{r['secs']}s</td><td>{time.strftime('%H:%M', time.localtime(r['at']))}</td></tr>"
        for r in d["recent"]) or "<tr><td colspan=4>None yet</td></tr>"
    sessions = "".join(
        f"<li>{e(s['title'] or '?')} ({e(s['state'] or 'unknown')})</li>"
        for s in d["sessions"]) or "<li>Nothing playing</li>"
    poll = ("never" if not d["last_poll"] else
            time.strftime("%H:%M:%S", time.localtime(d["last_poll"])))
    err = f"<p class=err>Last Plex poll failed: {e(d['poll_error'])}</p>" if d["poll_error"] else ""
    disk = c["disk"] or {}
    limits = " · ".join(f"{k.replace('_', ' ').lower()}: {e(v)}" for k, v in m.items() if v)
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=10><title>Plex-Cache</title><style>
:root{{color-scheme:light dark;--bg:#fff;--fg:#1c1c1c;--muted:#666;--line:#ddd}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181d;--fg:#e6e6e6;--muted:#9a9a9a;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif;margin:0 auto;
max-width:900px;padding:16px}}h1{{font-size:22px}}h2{{font-size:16px;margin-top:28px}}
table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);overflow-wrap:anywhere}}.muted{{color:var(--muted)}}
.err{{color:#d33}}.stats{{display:flex;gap:24px;flex-wrap:wrap}}.stats b{{display:block;
font-size:20px}}</style></head><body>
<h1>Plex-Cache</h1>{err}
<div class=stats><div><b>{_gb(c['used'])}</b><span class=muted>cached
{('of ' + e(m['CACHE_MAX_SIZE'])) if m.get('CACHE_MAX_SIZE') else ''}</span></div>
<div><b>{_gb(disk.get('free'))}</b><span class=muted>free on cache disk</span></div>
<div><b>{len(d['sessions'])}</b><span class=muted>playing in Plex</span></div></div>
<p class=muted>{limits or 'Mount settings not readable'} · last checked Plex {poll}</p>
<h2>Playing</h2><ul>{sessions}</ul>
<h2>Caching now</h2><table><tr><th>File</th><th>Why</th><th>State</th><th>Progress</th>
<th>MB/s</th></tr>{rows}</table>
<h2>Recently cached</h2><table><tr><th>File</th><th>Read</th><th>Took</th><th>At</th></tr>
{recent}</table></body></html>"""


def start_status_server(cfg: Config, pf: Prefetcher) -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = status_data(cfg, pf)
            if self.path.rstrip("/").endswith("/api/status"):
                body, ctype = json.dumps(data).encode(), "application/json"
            else:
                body, ctype = render_status(data).encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
    args = ap.parse_args(argv)
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
