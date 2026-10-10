#!/usr/bin/env bash
# Remove Plex-Cache. Keeps /etc/plex-precache and the cache contents unless --purge.
#   sudo /opt/plex-precache/uninstall.sh [--purge]     (or: sudo box remove plexcache)
set -uo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

CACHE_DIR=""
if [ -f /etc/plex-precache/rclone-mount.env ]; then
  CACHE_DIR="$(sed -n 's/^CACHE_DIR=//p' /etc/plex-precache/rclone-mount.env)"
fi

for unit in plex-precache plex-cache-mount; do
  systemctl disable --now "$unit" 2>/dev/null
  rm -f "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload

if [ -f /etc/nginx/apps/plexcache.conf ]; then
  rm -f /etc/nginx/apps/plexcache.conf
  systemctl reload nginx 2>/dev/null
fi

PROFILES=/opt/swizzin/core/custom/profiles.py
if [ -f "$PROFILES" ] && grep -q "BEGIN plex-cache" "$PROFILES"; then
  sed -i '/# BEGIN plex-cache/,/# END plex-cache/d' "$PROFILES"
  systemctl restart panel 2>/dev/null
fi
rm -f /install/.plexcache.lock /etc/swizzin/scripts/remove/plexcache.sh
rm -f /usr/local/bin/plex-cache-resize
rm -rf /opt/plex-precache

if [ "$PURGE" -eq 1 ]; then
  rm -rf /etc/plex-precache
  [ -n "$CACHE_DIR" ] && rm -rf "$CACHE_DIR"
  echo "Removed Plex-Cache, its config and cached files."
else
  echo "Removed Plex-Cache. Config kept in /etc/plex-precache${CACHE_DIR:+, cached files in $CACHE_DIR}."
fi
