# Changelog

The version is shown on the status page (`/plexcache/`) and by
`python3 /opt/plex-precache/plex_precache.py --version`.

## 1.1.0 (10 Oct 2026)

- Status page grades each stream and the server as Good, Borderline or
  Struggling, updates live, and has a Live/Paused toggle.
- Settings can be changed from the status page, including one-click
  recommended settings worked out from the server's disk, RAM and CPU.
- Fresh installs are tuned to the server automatically.
- Updates: `sudo plex-cache-update`, an Update button on the page, or
  nightly automatic updates. The updater never installs an older version than
  the one running.
- Switch from SSHFS to a cached rclone SFTP mount with
  `install.sh --sftp ... --replace-sshfs`.
- Pre-caching pauses on mounts that can't cache (such as SSHFS) instead of
  competing with Plex for bandwidth.
- Downloads stop once nobody has watched the stream for 10 minutes
  (adjustable).
- `plex-cache-resize --max-age` sets how long unwatched files stay cached.

## 1.0.0 (10 Oct 2026)

- Cached rclone mount, Plex-aware prefetcher, Swizzin installer, panel tile
  and status page, adjustable cache size.
