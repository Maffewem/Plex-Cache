#!/usr/bin/env bash
# Install plex-precache (and optionally the cached rclone mount) on a Swizzin box.
#
#   sudo ./install.sh                     # prefetch daemon only
#   sudo ./install.sh --with-mount        # also install the cached rclone mount unit
#   sudo ./install.sh --user bob          # run services as this user (default: Swizzin master user)
#   sudo ./install.sh --with-mount --cache-size 25G     # fixed cache size
#   sudo ./install.sh --with-mount --cache-percent 70   # % of free disk (default 60)
#   sudo ./install.sh --with-mount --min-free 15G       # disk to always keep free (default 10G)
#
# Coming from an SSHFS mount? Point rclone at the same server, key and path:
#   sudo ./install.sh --sftp user@host:/home/user/data --ssh-key /root/.ssh/hostname \
#        --mountpoint /data [--ssh-port 22] [--replace-sshfs]
# --replace-sshfs also unmounts the SSHFS mount, comments it out of /etc/fstab
# (backup kept) and starts the cached mount in its place, so Plex paths don't change.
#
# Re-running is safe and keeps your config. Change the size later with plex-cache-resize.
# Later versions install with: sudo plex-cache-update (or the status page's Update button).
set -euo pipefail

cd "$(dirname "$0")"
WITH_MOUNT=0
RUN_USER=""
OPT_CACHE_SIZE=""
OPT_CACHE_PERCENT=""
OPT_MIN_FREE=""
SFTP="" SSH_KEY="" SSH_PORT=22 MOUNT_AT="" REPLACE_SSHFS=0 UPDATE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --with-mount) WITH_MOUNT=1 ;;
    --user) RUN_USER="$2"; shift ;;
    --cache-size) OPT_CACHE_SIZE="$2"; shift ;;
    --cache-percent) OPT_CACHE_PERCENT="$2"; shift ;;
    --min-free) OPT_MIN_FREE="$2"; shift ;;
    --sftp) SFTP="$2"; WITH_MOUNT=1; shift ;;
    --ssh-key) SSH_KEY="$2"; shift ;;
    --ssh-port) SSH_PORT="$2"; shift ;;
    --mountpoint) MOUNT_AT="$2"; shift ;;
    --replace-sshfs) REPLACE_SSHFS=1 ;;
    # Used by plex-cache-update: refresh the installed files, keep every setting.
    --update) UPDATE=1 ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
if [ "$UPDATE" -eq 1 ]; then
  [ -f /etc/systemd/system/plex-cache-mount.service ] && WITH_MOUNT=1
  # Keep running as whoever the services already run as.
  RUN_USER="$(sed -n 's/^User=//p' /etc/systemd/system/plex-precache.service 2>/dev/null)"
fi
if [ -n "$SFTP" ]; then
  case "$SFTP" in *@*:/*) ;; *) echo "--sftp must look like user@host:/path" >&2; exit 1 ;; esac
  [ -n "$SSH_KEY" ] && [ -f "$SSH_KEY" ] || { echo "--sftp needs --ssh-key pointing at the key SSHFS uses" >&2; exit 1; }
fi
[ "$REPLACE_SSHFS" -eq 0 ] || [ -n "$SFTP" ] || { echo "--replace-sshfs needs --sftp" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

if [ -z "$RUN_USER" ]; then
  # Swizzin records its master user here.
  if [ -f /root/.master.info ]; then
    RUN_USER="$(cut -d: -f1 < /root/.master.info)"
  else
    RUN_USER="${SUDO_USER:-}"
  fi
fi
[ -n "$RUN_USER" ] && id "$RUN_USER" >/dev/null 2>&1 \
  || { echo "could not work out which user to run as; pass --user NAME" >&2; exit 1; }
echo "Running services as: $RUN_USER"

install -d -m 755 /opt/plex-precache /etc/plex-precache
install -m 755 plex_precache.py uninstall.sh /opt/plex-precache/
install -m 755 plex-cache-resize /usr/local/bin/plex-cache-resize
install -m 755 plex-cache-update /usr/local/sbin/plex-cache-update
for unit in plex-cache-update.service plex-cache-autoupdate.service plex-cache-autoupdate.timer; do
  install -m 644 "systemd/$unit" "/etc/systemd/system/$unit"
done
if git -C . rev-parse --git-dir >/dev/null 2>&1; then
  git -C . rev-parse --short=7 HEAD > /opt/plex-precache/VERSION
fi

NEW_CONFIG=0
if [ ! -f /etc/plex-precache/config.ini ]; then
  NEW_CONFIG=1
  install -m 640 -o "$RUN_USER" -g "$RUN_USER" config.example.ini /etc/plex-precache/config.ini
  PREFS="/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml"
  if [ -f "$PREFS" ]; then
    TOKEN="$(grep -o 'PlexOnlineToken="[^"]*"' "$PREFS" | cut -d'"' -f2 || true)"
    if [ -n "$TOKEN" ]; then
      sed -i "s|^plex_token =.*|plex_token = $TOKEN|" /etc/plex-precache/config.ini
      echo "Filled plex_token from Plex's Preferences.xml"
    fi
  fi
else
  echo "Keeping existing /etc/plex-precache/config.ini"
fi

sed "s/@USER@/$RUN_USER/g" systemd/plex-precache.service > /etc/systemd/system/plex-precache.service

if [ "$WITH_MOUNT" -eq 1 ]; then
  command -v rclone >/dev/null || { echo "rclone not found: install it first (box install rclone)" >&2; exit 1; }
  NEW_ENV=0
  if [ ! -f /etc/plex-precache/rclone-mount.env ]; then
    sed "s|/home/USER/|/home/$RUN_USER/|" systemd/rclone-mount.env.example > /etc/plex-precache/rclone-mount.env
    NEW_ENV=1
  fi
  set_env() {  # key value; values with spaces are quoted so bash can source the file
    local v="$2"
    case "$v" in *[[:space:]]*) v="\"$v\"" ;; esac
    if grep -q "^$1=" /etc/plex-precache/rclone-mount.env; then
      sed -i "s|^$1=.*|$1=$v|" /etc/plex-precache/rclone-mount.env
    else
      echo "$1=$v" >> /etc/plex-precache/rclone-mount.env
    fi
  }
  # Earlier versions wrote EXTRA_ARGS=--flag value unquoted, which breaks sourcing.
  sed -i -E 's/^([A-Z_]+)=([^"].*[[:space:]].*)$/\1="\2"/' /etc/plex-precache/rclone-mount.env
  # Settings added in later versions.
  grep -q '^VFS_CHUNK_SIZE=' /etc/plex-precache/rclone-mount.env || set_env VFS_CHUNK_SIZE 64M
  grep -q '^DIR_CACHE_TIME=' /etc/plex-precache/rclone-mount.env || set_env DIR_CACHE_TIME 1h
  if [ -n "$SFTP" ]; then
    # Same server, user, key and path as the SSHFS mount, but through rclone so
    # reads land in the local cache. Kept in its own config, not the user's.
    SFTP_USER="${SFTP%%@*}" REST="${SFTP#*@}"
    SFTP_HOST="${REST%%:*}" SFTP_PATH="${REST#*:}"
    install -m 600 -o "$RUN_USER" -g "$RUN_USER" "$SSH_KEY" /etc/plex-precache/ssh_key
    KNOWN_HOSTS=""
    for f in "$(dirname "$SSH_KEY")/known_hosts" /root/.ssh/known_hosts; do
      if [ -f "$f" ] && { ssh-keygen -F "$SFTP_HOST" -f "$f" >/dev/null 2>&1 \
                          || grep -qE "(^|,|\[)${SFTP_HOST}[],[:space:]]" "$f"; }; then
        install -m 644 "$f" /etc/plex-precache/known_hosts
        KNOWN_HOSTS="known_hosts_file = /etc/plex-precache/known_hosts"
        break
      fi
    done
    cat > /etc/plex-precache/rclone.conf <<RCONF
[plexcache-sftp]
type = sftp
host = $SFTP_HOST
user = $SFTP_USER
port = $SSH_PORT
key_file = /etc/plex-precache/ssh_key
$KNOWN_HOSTS
shell_type = unix
md5sum_command = none
sha1sum_command = none
disable_hashcheck = true
RCONF
    chown "$RUN_USER": /etc/plex-precache/rclone.conf
    chmod 600 /etc/plex-precache/rclone.conf
    set_env REMOTE "plexcache-sftp:$SFTP_PATH"
    set_env RCLONE_CONFIG /etc/plex-precache/rclone.conf
    echo "rclone SFTP remote: $SFTP_USER@$SFTP_HOST:$SFTP_PATH (key copied to /etc/plex-precache/ssh_key)"
    [ -n "$KNOWN_HOSTS" ] || echo "Note: no known_hosts entry for $SFTP_HOST found; rclone won't verify the host key." >&2
  fi
  [ -n "$MOUNT_AT" ] && set_env MOUNTPOINT "$MOUNT_AT"
  # rclone 1.68+ can fetch one file over several connections at once, which is
  # what a single 4K stream over a high-latency link needs.
  if [ "$NEW_ENV" -eq 1 ]; then
    RCLONE_VER="$(rclone version 2>/dev/null | sed -n 's/^rclone v\([0-9]*\)\.\([0-9]*\).*/\1 \2/p')"
    if [ -n "$RCLONE_VER" ] && [ "${RCLONE_VER% *}" -ge 1 ] && \
       { [ "${RCLONE_VER% *}" -gt 1 ] || [ "${RCLONE_VER#* }" -ge 68 ]; }; then
      set_env EXTRA_ARGS "--vfs-read-chunk-streams 4"
      echo "rclone ${RCLONE_VER/ /.}: downloading each file over 4 parallel streams."
    fi
  fi
  # shellcheck disable=SC1091
  . /etc/plex-precache/rclone-mount.env
  install -d -o "$RUN_USER" -g "$RUN_USER" "$CACHE_DIR"
  # Never chown an existing mountpoint: it may be the live SSHFS mount.
  [ -d "$MOUNTPOINT" ] || install -d -o "$RUN_USER" -g "$RUN_USER" "$MOUNTPOINT"
  grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null || echo user_allow_other >> /etc/fuse.conf
  sed "s/@USER@/$RUN_USER/g" systemd/plex-cache-mount.service > /etc/systemd/system/plex-cache-mount.service

  # A fresh install is tuned to this server's disk, RAM and CPU; explicit flags win.
  if [ "$NEW_ENV" -eq 1 ]; then
    echo "Tuned for this server:"
    while IFS='=' read -r k v; do
      case "$k" in
        CACHE_*|READ_AHEAD|BUFFER_SIZE) set_env "$k" "$v"; echo "  $k=$v" ;;
        next_episodes|workers|min_free_gb)
          if [ "$NEW_CONFIG" -eq 1 ]; then
            sed -i "s|^$k *=.*|$k = $v|" /etc/plex-precache/config.ini
            echo "  $k = $v"
          fi ;;
      esac
    done < <(python3 /opt/plex-precache/plex_precache.py --recommend "$CACHE_DIR")
  fi
  RESIZE=()
  [ -n "$OPT_MIN_FREE" ] && RESIZE+=(--min-free "$OPT_MIN_FREE")
  if [ -n "$OPT_CACHE_SIZE" ]; then
    RESIZE+=("$OPT_CACHE_SIZE")
  elif [ -n "$OPT_CACHE_PERCENT" ]; then
    RESIZE+=(--percent "$OPT_CACHE_PERCENT")
  fi
  if [ ${#RESIZE[@]} -gt 0 ]; then
    PLEX_CACHE_ENV=/etc/plex-precache/rclone-mount.env /usr/local/bin/plex-cache-resize --no-restart "${RESIZE[@]}"
  fi
  if [ -n "$SFTP" ] && [ "$NEW_ENV" -eq 1 ] && [ -z "$MOUNT_AT" ]; then
    echo "Set MOUNTPOINT in /etc/plex-precache/rclone-mount.env to where Plex expects media."
  elif [ -z "$SFTP" ] && [ "$NEW_ENV" -eq 1 ]; then
    echo "Edit /etc/plex-precache/rclone-mount.env (REMOTE, MOUNTPOINT) before starting the mount."
  fi

  if [ -n "$SFTP" ]; then
    systemctl daemon-reload
    # Check the remote works before touching the live SSHFS mount.
    if ! sudo -u "$RUN_USER" "$(command -v rclone)" --config /etc/plex-precache/rclone.conf lsf --max-depth 1 \
         "$REMOTE" >/dev/null; then
      echo "rclone can't list $REMOTE with that key; leaving SSHFS alone." >&2
      exit 1
    fi
    echo "rclone can reach $REMOTE."
    if [ "$(findmnt -n -o FSTYPE --target "$MOUNTPOINT" 2>/dev/null)" = "fuse.sshfs" ]; then
      if [ "$REPLACE_SSHFS" -eq 1 ]; then
        echo "Replacing the SSHFS mount at $MOUNTPOINT (Plex is paused for a moment)..."
        # An fstab line that is active, mentions sshfs and mounts at $MOUNTPOINT.
        AT="[[:space:]]${MOUNTPOINT%/}/?[[:space:]]"
        if grep -E "^[^#]" /etc/fstab | grep -E "$AT" | grep -q sshfs; then
          cp /etc/fstab "/etc/fstab.plex-cache.$(date +%Y%m%d%H%M%S)"
          sed -i -E "\\|^[^#]|{/sshfs/{\\|${AT}|s|^|# disabled by Plex-Cache: |}}" /etc/fstab
          echo "Commented the SSHFS line out of /etc/fstab (backup saved next to it)."
        else
          echo "Warning: no SSHFS line for $MOUNTPOINT in /etc/fstab. If a script or service" \
               "mounts it at boot, disable that too or it will fight the new mount." >&2
        fi
        systemctl stop plexmediaserver 2>/dev/null || true
        fusermount -uz "$MOUNTPOINT" 2>/dev/null || umount -l "$MOUNTPOINT"
        chown "$RUN_USER": "$MOUNTPOINT"
        systemctl enable --now plex-cache-mount
        systemctl start plexmediaserver 2>/dev/null || true
        echo "Cached mount is live at $MOUNTPOINT. To go back: systemctl disable --now" \
             "plex-cache-mount, restore /etc/fstab from the backup, and mount -a."
      else
        echo "SSHFS is still mounted at $MOUNTPOINT. Re-run with --replace-sshfs to swap it" \
             "for the cached mount, or unmount it yourself and run: systemctl enable --now plex-cache-mount"
      fi
    fi
  fi
fi

# ---- Swizzin integration ------------------------------------------------------
MASTER="$(cut -d: -f1 2>/dev/null < /root/.master.info || echo "$RUN_USER")"

# Status page at https://<server>/plexcache/, behind the same login as other apps.
if [ -f /install/.nginx.lock ] && [ -d /etc/nginx/apps ]; then
  PORT="$(awk -F' *= *' '/^status_port/ {print $2}' /etc/plex-precache/config.ini)"
  PORT="${PORT:-8789}"
  HTPASSWD="/etc/htpasswd.d/htpasswd.$MASTER"
  [ -f "$HTPASSWD" ] || HTPASSWD=/etc/htpasswd
  sed -e "s/@PORT@/$PORT/" -e "s|/etc/htpasswd.d/htpasswd.@USER@|$HTPASSWD|" \
    swizzin/plexcache.nginx.conf > /etc/nginx/apps/plexcache.conf
  if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx
    echo "Status page: https://<your server>/plexcache/"
  else
    rm -f /etc/nginx/apps/plexcache.conf
    echo "Warning: nginx rejected the plexcache config; status page not published." >&2
  fi
fi

# Tile on the Swizzin panel: status light, start/stop, link to the status page.
# core/custom/profiles.py is the panel's extension point and survives panel updates.
if [ -f /install/.panel.lock ] && [ -d /opt/swizzin/core ]; then
  PROFILES=/opt/swizzin/core/custom/profiles.py
  mkdir -p "$(dirname "$PROFILES")"
  [ -f "$PROFILES" ] || echo "from core.profiles import *" > "$PROFILES"
  if ! grep -q "BEGIN plex-cache" "$PROFILES"; then
    cat swizzin/plexcache_profile.py >> "$PROFILES"
  fi
  chown swizzin: "$PROFILES" 2>/dev/null || true
  touch /install/.plexcache.lock
  systemctl restart panel 2>/dev/null || true
  echo "Added Plex-Cache to the Swizzin panel."
fi

# The status page edits these, so the service user owns them (the directory
# stays root's). It may restart only these two units, without a password.
chown "$RUN_USER": /etc/plex-precache/config.ini
chmod 640 /etc/plex-precache/config.ini
[ -f /etc/plex-precache/rclone-mount.env ] && chown "$RUN_USER": /etc/plex-precache/rclone-mount.env
SUDOERS=/etc/sudoers.d/plex-cache
{
  echo "# Lets the Plex-Cache status page apply settings. Added by Plex-Cache install.sh."
  for bin in /usr/bin/systemctl /bin/systemctl; do
    for unit in plex-cache-mount plex-precache; do
      echo "$RUN_USER ALL=(root) NOPASSWD: $bin restart $unit"
    done
    # The Update button; the service runs the root-owned plex-cache-update.
    echo "$RUN_USER ALL=(root) NOPASSWD: $bin start --no-block plex-cache-update"
  done
} > "$SUDOERS.tmp"
chmod 440 "$SUDOERS.tmp"
if command -v visudo >/dev/null && visudo -cf "$SUDOERS.tmp" >/dev/null; then
  mv "$SUDOERS.tmp" "$SUDOERS"
else
  rm -f "$SUDOERS.tmp"
  echo "Note: couldn't add the sudo rule; the status page can save settings but not restart the mount." >&2
fi

# Let `box remove plexcache` uninstall it.
if [ -d /etc/swizzin/scripts/remove ]; then
  printf '#!/bin/bash\nexec /opt/plex-precache/uninstall.sh "$@"\n' > /etc/swizzin/scripts/remove/plexcache.sh
  chmod 755 /etc/swizzin/scripts/remove/plexcache.sh
fi

systemctl daemon-reload
# The timer runs nightly but only updates when auto_update = true.
systemctl enable --now plex-cache-autoupdate.timer >/dev/null 2>&1 || true
[ "$UPDATE" -eq 1 ] && { echo "Updated Plex-Cache files."; exit 0; }
echo
echo "Installed. Next:"
[ "$WITH_MOUNT" -eq 1 ] && [ "$REPLACE_SSHFS" -eq 0 ] && echo "  systemctl enable --now plex-cache-mount   # after editing rclone-mount.env"
echo "  systemctl enable --now plex-precache"
echo "  journalctl -fu plex-precache             # watch it work"
echo "  plex-cache-resize --show                 # cache size and usage"
