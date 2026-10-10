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
sudo nano /etc/plex-precache/rclone-mount.env   # REMOTE and MOUNTPOINT (cache size is pre-set)
sudo systemctl enable --now plex-cache-mount
sudo systemctl enable --now plex-precache
journalctl -fu plex-precache
```

Then point your Plex libraries at `MOUNTPOINT` (or keep their paths and set
`MOUNTPOINT` to where they already point). The installer sizes the cache to
about 60% of the free disk after keeping `CACHE_MIN_FREE` (10 GB) spare; on a
box with 38 GB free that's 16 GB (see [Cache size](#cache-size) to change it).
It also reads the Plex token
from Plex's `Preferences.xml`; if it can't, put it in
`/etc/plex-precache/config.ini`.

You should see lines like:

```
queued /mnt/remote/tv/Show/S01E02.mkv from 812 MB (prio 0)
queued /mnt/remote/tv/Show/S01E03.mkv from 0 MB (prio 1)
cached /mnt/remote/tv/Show/S01E02.mkv: 3188 MB in 270s (11.8 MB/s)
```

### Coming from SSHFS

SSHFS can't keep a disk cache. Its `cache=yes` and `kernel_cache` options only
cache directory listings and whatever happens to stay in RAM, so most reads
still cross the WAN. The installer can switch you to rclone's SFTP backend,
which uses the same server, SSH key and path, and mounts at the same place so
your Plex libraries don't change.

Take the values from your current SSHFS command. For
`sshfs ... -o IdentityFile=~/.ssh/hostname username@serverIP:/home/username/data/ /data`:

```bash
sudo box install rclone
sudo ./install.sh --sftp username@serverIP:/home/username/data \
     --ssh-key /root/.ssh/hostname --mountpoint /data --replace-sshfs
sudo systemctl enable --now plex-precache
```

Use the full path of the key (`~` above is the home folder of whoever ran
SSHFS, usually `/root`), and add `--ssh-port` if the server isn't on 22.

The installer copies the key to `/etc/plex-precache/ssh_key` and writes an
rclone config in `/etc/plex-precache/` without touching yours. It checks
that rclone can list the remote **before** changing anything. With
`--replace-sshfs` it then comments the SSHFS line out of `/etc/fstab` (keeping
a dated backup), unmounts SSHFS, and starts the cached mount at `/data`. Plex
is stopped for those few seconds. Leave `--replace-sshfs` off to set
everything up without swapping, and do the swap yourself later.

To go back: `sudo systemctl disable --now plex-cache-mount`, restore
`/etc/fstab` from the `/etc/fstab.plex-cache.*` backup, then `sudo mount -a`.

If SSHFS is mounted by a script or service rather than fstab, disable that
too, or it will try to mount over the new mount at boot.

The prefetcher refuses to run on SSHFS, and the status page says so. Reading
ahead there stores nothing locally and only competes with Plex for the same
SSH connection. SSHFS also funnels everything through one connection, so a
high-latency link tops out at a few MB/s, which is less than a 4K remux needs.
On rclone 1.68 and later the installer adds `--vfs-read-chunk-streams 4`, so
each file is fetched over several connections at once.

### Swizzin panel and status page

If the Swizzin panel is installed, the installer adds a **Plex-Cache** tile to it
(with the Plex icon), showing whether the prefetcher is running, with the same
start/stop/restart controls as other apps. It uses the panel's
`core/custom/profiles.py` extension point, which survives panel updates.

If nginx is installed, the tile links to a status page at
`https://<your server>/plexcache/`, behind the same login as your other apps.
It updates itself every 5 seconds (with a pause button) and grades everything
as **Good**, **Borderline** or **Struggling**, with a one-line reason:

- **Each stream:** how many minutes are cached ahead of the playhead, and how
  fast the link is downloading compared with the video's bitrate. If the link is
  slower than the video, it estimates when the head start runs out. It also
  flags Plex reporting a stream as buffering, or a file the box can't see.
- **Health:** whether Plex is reachable, free disk against the floor, whether
  the cache is full and evicting files, available RAM against the rclone
  buffers, and recent link speed.

"Good" means at least 2 minutes cached ahead and downloading 1.5x faster than
playback (or the file is fully cached). The page has a "What's normal?" section
explaining each level and what to do about it. The same data, including the
grades, is at `/plexcache/api/status` as JSON.

#### Settings from the web page

The bottom of the status page has a **Settings** section:

- **Pre-caching** (applies immediately): upcoming episodes, how much of each to
  cache, Continue Watching warm-up, downloads at once, and how often to check
  Plex.
- **Cache** (restarts the mount, so playback pauses for a few seconds): cache
  size as a fixed amount or a % of usable disk, disk to keep free, how long
  unwatched files are kept, read-ahead and RAM buffer per stream. There's
  also a restart-only button.
- **Pre-cache now**: queue a folder or file under the media folder, such as a
  season you're about to watch (up to 500 files at a time).

- **Recommended settings**: every field shows the value suggested for this
  server, and one button applies them all (see [Auto-tuning](#auto-tuning)).
- **Updates**: shows the installed version, checks GitHub, installs the latest
  version, and turns on nightly automatic updates.

The Plex token, remote and mount location stay command-line only. To make the
page work, the installer gives the service user ownership of
`/etc/plex-precache/config.ini` and `rclone-mount.env`. It also adds a sudo rule
(`/etc/sudoers.d/plex-cache`) that lets that user run exactly
`systemctl restart plex-cache-mount` and `systemctl restart plex-precache`, and
nothing else. Forms carry a per-run token, so other websites can't submit them
through your browser, and every value is checked before it's written.

`sudo box remove plexcache` (or `/opt/plex-precache/uninstall.sh`) removes it all;
add `--purge` to the uninstall script to also delete the config and cached files.

### Auto-tuning

A fresh install sizes everything to the server, and the status page can
re-apply this at any time with **Use recommended settings**:

| Setting | Rule | 8 GB RAM, 68 GB disk with 38 GB free |
|---|---|---|
| Keep free | 10% of the disk, between 5 and 20 GB | 7G |
| Cache size | 60% of free disk after that | 18G |
| RAM buffer per stream | about 1/64 of RAM, 32M to 512M | 128M |
| Read ahead | about 1/16 of the cache, 256M to 4G | 1G |
| Drop unwatched after | 72h, or 7d/14d for caches over 50/200 GB | 72h |
| Upcoming episodes | 1, 2 or 3 depending on cache size | 1 |
| Downloads at once | 1 on 1 to 2 CPU cores, else 2 | 2 |

`python3 /opt/plex-precache/plex_precache.py --recommend /var/cache/plex-cache`
prints the values without changing anything.

### Updating

```bash
sudo plex-cache-update           # install the latest version, keeping all settings
plex-cache-update --check        # just check
```

You can also use **Update now** on the status page. Ticking **Install updates
automatically each night** sets `auto_update = true`, and a timer then updates
around 04:30. The updater keeps its own root-owned copy of this repo in
`/opt/plex-precache/src` and re-runs the installer in update mode, which
refreshes the program files but leaves config, cache size and mount settings
alone. If an update changes the mount's own unit file, the change waits for the
next mount restart so playback isn't interrupted. Only commits on this repo's
`main` branch are ever installed.

### Cache size

Set it at install time, or change it whenever you like:

```bash
sudo ./install.sh --with-mount --cache-size 25G      # fixed size
sudo ./install.sh --with-mount --cache-percent 70    # % of free disk (default 60)
sudo ./install.sh --with-mount --min-free 15G        # disk always kept free (default 10G)

sudo plex-cache-resize 25G              # later: fixed size
sudo plex-cache-resize --percent 70     # later: re-measure the disk and size by %
sudo plex-cache-resize --min-free 15G   # later: change the floor
sudo plex-cache-resize --max-age 7d     # later: drop files unwatched for 7 days (default 72h)
plex-cache-resize --show                # current size, floor, expiry, disk free, cache usage
```

### How the cache clears itself

rclone handles this:

- **By age.** Files nobody has read for `CACHE_MAX_AGE` (72h by default) are
  deleted.
- **By space.** When the cache passes `CACHE_MAX_SIZE`, or free disk drops below
  `CACHE_MIN_FREE`, the least recently read files are deleted first, until
  it's back under the limits.
- **Streams are protected.** Files that are open are never evicted. That covers
  anything Plex is playing and anything the prefetcher is reading. A next
  episode that has just been cached was read recently, so it's among the last
  to go.
- **No pre-caching into a full disk.** The prefetcher won't start a
  next-episode or On Deck download that would take free disk below
  `min_free_gb`.

rclone checks these limits every minute, so the cache can briefly run over
them. It can also stay over while the open files alone exceed the limit.

Percent sizing counts what's already in the cache as available, so re-running
it doesn't shrink the cache just because the cache has filled up. Resizing
restarts the mount, which briefly interrupts playback; add `--no-restart` to
apply it at the next restart instead. `--min-free` also updates the
prefetcher's own floor (`min_free_gb`).

### Already have an rclone mount?

Skip `--with-mount` and add these to your existing mount instead; they are what
matter:

```
--vfs-cache-mode full --cache-dir /path/on/ssd --vfs-cache-max-size 20G
--vfs-cache-min-free-space 10G --vfs-cache-max-age 72h --vfs-read-ahead 1G
--buffer-size 128M
```

Without `--vfs-cache-mode full`, the prefetcher's reads aren't kept and it only
wastes bandwidth.

### Storage mounted over NFS or SMB?

Either mount it through rclone instead (rclone has `sftp`, `smb` and other
backends), or use the kernel's FS-Cache: `apt install cachefilesd`, set
`RUN=yes` in `/etc/default/cachefilesd`, and add `fsc` to the mount options
(NFS: `-o fsc`; CIFS: `-o fsc`). The prefetcher then works the same way, but
FS-Cache has no read-ahead or size controls, so rclone is the better choice.

## Configuration

`/etc/plex-precache/config.ini` (see [config.example.ini](config.example.ini)):

| Setting | Default | What it does |
|---|---|---|
| `next_episodes` | 1 | Episodes after the current one to cache |
| `next_episode_head_mb` | 0 | Cache only the first N MB of those (0 = whole file) |
| `on_deck` | false | Also warm the start of Continue Watching items |
| `workers` | 2 | Files read at once; use 1 on a slow link |
| `remember_hours` | 12 | Skip files fully cached this recently |
| `min_free_gb` | 10 | Skip next-episode/On Deck reads that would leave less free on `cache_dir`'s disk |
| `status_port` | 8789 | Status page port on 127.0.0.1 (0 disables) |
| `[path_map]` | | Translate Plex's paths if Plex sees a different path (Docker) |

To warm something by hand, e.g. a show you're about to binge:

```bash
python3 /opt/plex-precache/plex_precache.py --warm "/mnt/remote/tv/Some Show/Season 1"
```

## Tradeoffs

- **Disk space.** The cache holds whole files, so its size decides how much
  stays local: 16 GB is a few 1080p episodes or one or two films, while a 4K
  remux episode alone can be 10 GB+. rclone evicts the least-recently-used
  files when it hits `CACHE_MAX_SIZE` or `CACHE_MIN_FREE`, and the prefetcher
  won't start speculative downloads that would dip below `min_free_gb`. On a
  small root disk, a cheap extra SSD or volume for `CACHE_DIR` is the biggest
  upgrade.
- **Bandwidth.** Prefetching downloads files you may not finish. With
  `next_episodes = 1` you can pull an episode you never watch. Lower it, or use
  `next_episode_head_mb` to cache just the start, if your link is metered.
- **It can't beat the link for the first minutes.** If the link is slower than
  the file's bitrate, the prefetcher only stays ahead if it started early.
  Pre-warm with `--warm`, or enable `on_deck`, for files you know you'll watch.
- **RAM vs disk.** `--buffer-size` is RAM per open file (128 MB each by
  default), and Linux also keeps recently read cache data in spare RAM
  automatically. A RAM-only cache (tmpfs `CACHE_DIR`) isn't worth it on an
  8 GB box: it would hold about one film and compete with Plex itself, while
  local disk is already far faster than any WAN link.
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
