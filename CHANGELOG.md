# Changelog

The version is shown on the status page (`/plexcache/`) and by
`python3 /opt/plex-precache/plex_precache.py --version`.

## 1.4.1 (23 Oct 2026)

Security fixes:

- The installer, `plex-cache-resize` and the uninstaller no longer run `rclone-mount.env` as root code. That file is writable by the service user, so a value like `$(command)` in it ran as root. They now read it as text and refuse system folders as `CACHE_DIR` or `MOUNTPOINT`; the switch script does the same for `MOUNTPOINT`.
- The status page now needs the key nginx sends after its login (`/etc/plex-precache/status.key`, created by the installer). Other users on the box can no longer use port 8789 directly. Without the key file the page behaves as before.
- "Extra rclone flags" only accepts mount-tuning flags (`--bwlimit`, `--vfs-*`, a few `--sftp-*` speed settings and similar). Flags that run programs, open ports or change the server are refused.
- The page can't be shown inside another site's frame, and a bad request length no longer ties up the server.

Fixes:

- A file you start watching part-way through is no longer shown as "Fully cached"; only the part from where you started counts, and watching it from the start still downloads it.
- The red "can't keep a disk cache" warning goes away after switching from SSHFS, without restarting.
- Plex restarting mid-reply no longer stops the service.
- A save message appears only on the page that saved, not on another open tab.
- `plex-cache-resize` accepts decimal sizes like 10.5G.

## 1.4.0 (17 Oct 2026)

- Restarts and updates no longer forget what has been cached, the recent downloads list or the graphs. They're saved in /var/lib/plex-precache/state.json.
- New "Last 24 hours" graphs on the status page: cache size, pre-cache download speed, and streams playing, marked in red wherever one was buffering.

## 1.3.1 (10 Oct 2026)

- Fixed: an episode whose start had already been pre-cached as "next episode" wasn't downloaded in full when you played it.
- Fixed: music wasn't picked up at all.

## 1.3.0 (10 Oct 2026)

- Each change now bumps the version number.
- Removed the "Go back to SSHFS" button. `sudo plex-cache-switch --back` still works from SSH.
- Each stream now shows how fast Plex is reading it, and whether it is transcoding.
- Pre-caching slows down once nobody is watching, then stops. Set the speed under "...and meanwhile slow it to (MB/s)".
- Refreshed the buttons and added a light/dark mode switch.
- The page reloads by itself once an update finishes.
- Added an icon for the browser tab and the Swizzin panel tile.

## 1.2.0 (10 Oct 2026)

- Switch from SSHFS to a cached rclone SFTP mount from the status page or with
  `sudo plex-cache-switch`. It checks rclone is new enough and sees the same
  folders, puts SSHFS back if the new mount doesn't show your files, and can
  switch back later.
- Status and Settings are separate tabs, and each settings panel folds away.
- Saving a setting keeps your place on the page and shows the result in a
  pop-up at the bottom instead of jumping to the top.
- The SFTP connection (server, user, port, folder, mount location, SSH key)
  and advanced rclone settings can be changed from the status page. Changes
  are tested first and rolled back if the mount won't start.
- Fix: an `EXTRA_ARGS` value with spaces broke the installer and
  `plex-cache-resize`; values are now quoted, and updates fix existing files.

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
