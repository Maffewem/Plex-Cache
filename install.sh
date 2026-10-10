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
# Re-running is safe and keeps your config. Change the size later with plex-cache-resize.
set -euo pipefail

cd "$(dirname "$0")"
WITH_MOUNT=0
RUN_USER=""
CACHE_SIZE=""
CACHE_PERCENT=""
MIN_FREE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --with-mount) WITH_MOUNT=1 ;;
    --user) RUN_USER="$2"; shift ;;
    --cache-size) CACHE_SIZE="$2"; shift ;;
    --cache-percent) CACHE_PERCENT="$2"; shift ;;
    --min-free) MIN_FREE="$2"; shift ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
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

if [ ! -f /etc/plex-precache/config.ini ]; then
  install -m 640 -o root -g "$RUN_USER" config.example.ini /etc/plex-precache/config.ini
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
  # shellcheck disable=SC1091
  . /etc/plex-precache/rclone-mount.env
  install -d -o "$RUN_USER" -g "$RUN_USER" "$CACHE_DIR" "$MOUNTPOINT"
  grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null || echo user_allow_other >> /etc/fuse.conf
  sed "s/@USER@/$RUN_USER/g" systemd/plex-cache-mount.service > /etc/systemd/system/plex-cache-mount.service

  # Size the cache: explicit flags win; a fresh install defaults to 60% of free disk.
  RESIZE=()
  [ -n "$MIN_FREE" ] && RESIZE+=(--min-free "$MIN_FREE")
  if [ -n "$CACHE_SIZE" ]; then
    RESIZE+=("$CACHE_SIZE")
  elif [ -n "$CACHE_PERCENT" ]; then
    RESIZE+=(--percent "$CACHE_PERCENT")
  elif [ "$NEW_ENV" -eq 1 ]; then
    RESIZE+=(--percent 60)
  fi
  if [ ${#RESIZE[@]} -gt 0 ]; then
    PLEX_CACHE_ENV=/etc/plex-precache/rclone-mount.env /usr/local/bin/plex-cache-resize --no-restart "${RESIZE[@]}"
  fi
  [ "$NEW_ENV" -eq 1 ] && echo "Edit /etc/plex-precache/rclone-mount.env (REMOTE, MOUNTPOINT) before starting the mount."
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

# Let `box remove plexcache` uninstall it.
if [ -d /etc/swizzin/scripts/remove ]; then
  printf '#!/bin/bash\nexec /opt/plex-precache/uninstall.sh "$@"\n' > /etc/swizzin/scripts/remove/plexcache.sh
  chmod 755 /etc/swizzin/scripts/remove/plexcache.sh
fi

systemctl daemon-reload
echo
echo "Installed. Next:"
[ "$WITH_MOUNT" -eq 1 ] && echo "  systemctl enable --now plex-cache-mount   # after editing rclone-mount.env"
echo "  systemctl enable --now plex-precache"
echo "  journalctl -fu plex-precache             # watch it work"
echo "  plex-cache-resize --show                 # cache size and usage"
