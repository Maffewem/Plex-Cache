# Plex-Cache

Stop Plex buffering when your media lives on the other side of a WAN link.

Plex reads media as it plays, so every hiccup on the link between the Plex box
and the storage shows up as buffering. This repo puts a **local cache** between
Plex and the remote storage and **fills it ahead of time**, so Plex reads from
local disk (and RAM) instead of the network.

Two parts, which work best together:

1. **A cached mount** (`plex-cache-mount.service`): rclone mounts the remote
   storage with `--vfs-cache-mode full`. Anything read through the mount is
   kept on local disk, rclone reads ahead of the player (`--vfs-read-ahead`),
   and each open file gets an in-RAM buffer (`--buffer-size`).
2. **A prefetcher** (`plex-precache.service`): watches Plex's active sessions and
   reads the playing file from the playhead to the end, plus the next episodes,
   as fast as the link allows. Plex then plays from the cache, and the next
   episode is already local when it starts.

```
 Plex ──reads──▶ /mnt/remote (rclone mount) ──cache hit──▶ local SSD cache
                         ▲                    └─cache miss─▶ remote storage (WAN)
 plex-precache ──reads ahead──┘
```

## Install on Swizzin

```bash
git clone https://github.com/Maffewem/Plex-Cache && cd Plex-Cache

# If your remote isn't mounted with rclone yet:
sudo box install rclone          # Swizzin's rclone package
rclone config                    # as your user: add the remote (SFTP, SMB, S3, Drive, ...)

sudo ./install.sh --with-mount   # or just ./install.sh if you keep your own mount
sudo nano /etc/plex-precache/rclone-mount.env   # REMOTE, MOUNTPOINT, CACHE_DIR, CACHE_MAX_SIZE
sudo systemctl enable --now plex-cache-mount
sudo systemctl enable --now plex-precache
journalctl -fu plex-precache
```

Then point your Plex libraries at `MOUNTPOINT` (or keep their paths and set
`MOUNTPOINT` to where they already point). The installer reads the Plex token
from Plex's `Preferences.xml`; if it can't, put it in
`/etc/plex-precache/config.ini`.

You should see lines like:

```
queued /mnt/remote/tv/Show/S01E02.mkv from 812 MB (prio 0)
queued /mnt/remote/tv/Show/S01E03.mkv from 0 MB (prio 1)
cached /mnt/remote/tv/Show/S01E02.mkv: 3188 MB in 270s (11.8 MB/s)
```

### Already have an rclone mount?

Skip `--with-mount` and add these to your existing mount instead; they are what
matter:

```
--vfs-cache-mode full --cache-dir /path/on/ssd --vfs-cache-max-size 500G
--vfs-cache-max-age 336h --vfs-read-ahead 2G --buffer-size 256M
```

Without `--vfs-cache-mode full`, the prefetcher's reads aren't kept and it only
wastes bandwidth.

### Storage mounted over NFS or SMB instead?

Either mount it through rclone instead (rclone has `sftp`, `smb` and other
backends), or use the kernel's FS-Cache: `apt install cachefilesd`, set
`RUN=yes` in `/etc/default/cachefilesd`, and add `fsc` to the mount options
(NFS: `-o fsc`; CIFS: `-o fsc`). The prefetcher then works the same way, but
FS-Cache has no read-ahead or size controls, so rclone is the better choice.

## Configuration

`/etc/plex-precache/config.ini` (see [config.example.ini](config.example.ini)):

| Setting | Default | What it does |
|---|---|---|
| `next_episodes` | 2 | Episodes after the current one to cache |
| `next_episode_head_mb` | 0 | Cache only the first N MB of those (0 = whole file) |
| `on_deck` | false | Also warm the start of Continue Watching items |
| `workers` | 2 | Files read at once; use 1 on a slow link |
| `remember_hours` | 12 | Skip files fully cached this recently |
| `[path_map]` | | Translate Plex's paths if Plex sees a different path (Docker) |

To warm something by hand, e.g. a show you're about to binge:

```bash
python3 /opt/plex-precache/plex_precache.py --warm "/mnt/remote/tv/Some Show/Season 1"
```

## Tradeoffs

- **Disk space.** The cache holds whole files. Size it for what you watch in a
  couple of weeks (a 4K remux episode can be 10 GB+). rclone evicts the
  least-recently-used files when it hits `CACHE_MAX_SIZE` or `CACHE_MIN_FREE`.
- **Bandwidth.** Prefetching downloads files you may not finish. With
  `next_episodes = 2` you can pull two episodes you never watch. Lower it, or use
  `next_episode_head_mb` to cache just the start, if your link is metered.
- **It can't beat the link for the first minutes.** If the link is slower than
  the file's bitrate, the prefetcher only stays ahead if it started early.
  Pre-warm with `--warm`, or enable `on_deck`, for files you know you'll watch.
- **RAM vs disk.** `--buffer-size` is RAM per open file, and Linux also keeps
  recently read cache data in spare RAM automatically. A RAM-only cache (tmpfs
  `CACHE_DIR`) is possible but rarely worth it: an SSD is far faster than any
  WAN link and holds much more.
- **Library scans.** With a full cache, Plex jobs that read whole files (deep
  media analysis, intro/credits detection, video preview thumbnails) will pull
  entire files across the WAN and fill the cache. Consider turning those off in
  Settings → Library / Scheduled Tasks.
- **Remote changes.** With `--vfs-cache-mode full`, rclone notices remote
  changes on directory refresh (`--dir-cache-time 1h`). New files can take up to
  an hour to appear in the mount unless your backend supports change polling.

Other Plex settings that help: put the **transcoder temporary directory** on
local SSD (Settings → Transcoder), and raise **Transcoder default throttle
buffer** so Plex transcodes further ahead.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The prefetcher is a single file, standard library only (Python 3.8+).
