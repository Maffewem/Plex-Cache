#!/usr/bin/env bash
# Install plex-precache (and optionally the cached rclone mount) on a Swizzin box.
#
#   sudo ./install.sh                 # prefetch daemon only
#   sudo ./install.sh --with-mount    # also install the cached rclone mount unit
#   sudo ./install.sh --user bob      # run services as this user (default: Swizzin master user)
set -euo pipefail

cd "$(dirname "$0")"
WITH_MOUNT=0
RUN_USER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --with-mount) WITH_MOUNT=1 ;;
    --user) RUN_USER="$2"; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
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
install -m 755 plex_precache.py /opt/plex-precache/plex_precache.py

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
  if [ ! -f /etc/plex-precache/rclone-mount.env ]; then
    sed "s|/home/USER/|/home/$RUN_USER/|" systemd/rclone-mount.env.example > /etc/plex-precache/rclone-mount.env
    echo "Edit /etc/plex-precache/rclone-mount.env (REMOTE, MOUNTPOINT, cache size) before starting the mount."
  fi
  # shellcheck disable=SC1091
  . /etc/plex-precache/rclone-mount.env
  install -d -o "$RUN_USER" -g "$RUN_USER" "$CACHE_DIR" "$MOUNTPOINT"
  grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null || echo user_allow_other >> /etc/fuse.conf
  sed "s/@USER@/$RUN_USER/g" systemd/plex-cache-mount.service > /etc/systemd/system/plex-cache-mount.service
fi

systemctl daemon-reload
echo
echo "Installed. Next:"
[ "$WITH_MOUNT" -eq 1 ] && echo "  systemctl enable --now plex-cache-mount   # after editing rclone-mount.env"
echo "  systemctl enable --now plex-precache"
echo "  journalctl -fu plex-precache             # watch it work"
