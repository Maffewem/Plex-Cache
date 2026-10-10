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
- Switch from SSHFS to a cached rclone SFTP mount from the status page or with
  `sudo plex-cache-switch`. It finds the server, folder and key itself, only
  switches when rclone sees the same folders, puts SSHFS back if the new mount
  doesn't show your files, and can switch back later.
- Pre-caching pauses on mounts that can't cache (such as SSHFS) instead of
  competing with Plex for bandwidth.
- Downloads stop once nobody has watched the stream for 10 minutes
  (adjustable).
- `plex-cache-resize --max-age` sets how long unwatched files stay cached.
- Status and Settings are separate tabs, and each settings panel folds away.
- Saving a setting keeps your place on the page and shows the result in a
  pop-up at the bottom instead of jumping to the top.
- The SFTP connection (server, user, port, folder, mount location, SSH key)
  and advanced rclone settings can be changed from the status page. Changes
  are tested first and rolled back if the mount won't start.
- Fix: an `EXTRA_ARGS` value with spaces broke the installer and
  `plex-cache-resize`; values are now quoted, and updates fix existing files.

## 1.0.0 (10 Oct 2026)

- Cached rclone mount, Plex-aware prefetcher, Swizzin installer, panel tile
  and status page, adjustable cache size.
