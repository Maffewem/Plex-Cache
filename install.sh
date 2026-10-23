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
# Coming from an SSHFS mount? This finds its server, path and key, checks rclone
# sees the same files, then swaps the mounts (Plex paths don't change):
#   sudo ./install.sh --replace-sshfs --mountpoint /data
# Or spell it out: --sftp user@host:/home/user/data --ssh-key /root/.ssh/key [--ssh-port 22]
# The SSHFS fstab line is commented out (backup kept). If the cached mount doesn't
# come up with your files, SSHFS is put back. Undo later: sudo plex-cache-switch --back
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
SFTP="" SSH_KEY="" SSH_PORT="" MOUNT_AT="" REPLACE_SSHFS=0 UPDATE=0 SWITCH_FAILED=0
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
    -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
fi
[ -z "$SSH_KEY" ] || [ -f "$SSH_KEY" ] || { echo "--ssh-key $SSH_KEY not found" >&2; exit 1; }
[ "$REPLACE_SSHFS" -eq 0 ] || WITH_MOUNT=1
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
install -m 755 plex-cache-switch /usr/local/sbin/plex-cache-switch
for unit in plex-cache-update.service plex-cache-autoupdate.service plex-cache-autoupdate.timer \
            plex-cache-switch.service; do
  install -m 644 "systemd/$unit" "/etc/systemd/system/$unit"
done
# Removed in 1.3.0: the status page no longer offers going back to SSHFS.
rm -f /etc/systemd/system/plex-cache-switch-back.service
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
  if ! rclone mount --help 2>/dev/null | grep -q -- '--vfs-cache-min-free-space'; then
    echo "Warning: $(rclone version 2>/dev/null | head -1) is too old for the cached mount." >&2
    echo "  Upgrade it with: curl https://rclone.org/install.sh | sudo bash" >&2
  fi
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
# The env file is writable by the service user, so root never sources it: values
# are read as text, and folders root creates or re-owns must be ordinary ones.
safe_dir() {  # path -> ok when it isn't a system folder
  local d="${1%/}"
  case "$d" in
    ""|/|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/var/lib|/var/cache) return 1 ;;
    /bin/*|/boot/*|/dev/*|/etc/*|/lib/*|/lib64/*|/proc/*|/root/*|/run/*|/sbin/*|/sys/*|/usr/*) return 1 ;;
  esac
  case "$d" in /*) ;; *) return 1 ;; esac
  case "$d" in *..*|*[!A-Za-z0-9._/\ -]*) return 1 ;; esac
  return 0
}
  env_get() { sed -n "s/^$1=//p" /etc/plex-precache/rclone-mount.env | tail -1 | sed 's/^"\(.*\)"$/\1/'; }
  CACHE_DIR="$(env_get CACHE_DIR)"
  MOUNTPOINT="$(env_get MOUNTPOINT)"
  safe_dir "$CACHE_DIR" || { echo "CACHE_DIR '$CACHE_DIR' in rclone-mount.env isn't a usable folder" >&2; exit 1; }
  safe_dir "$MOUNTPOINT" || { echo "MOUNTPOINT '$MOUNTPOINT' in rclone-mount.env isn't a usable folder" >&2; exit 1; }
  # Only create it; never take over a folder that already exists.
  [ -d "$CACHE_DIR" ] || install -d -o "$RUN_USER" -g "$RUN_USER" "$CACHE_DIR"
  # Never chown an existing mountpoint: it may be the live SSHFS mount.
  [ -d "$MOUNTPOINT" ] || install -d -o "$RUN_USER" -g "$RUN_USER" "$MOUNTPOINT"
  grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null || echo user_allow_other >> /etc/fuse.conf
  sed -e "s/@USER@/$RUN_USER/g" -e "s|@RCLONE@|$(command -v rclone)|" systemd/plex-cache-mount.service \
    > /etc/systemd/system/plex-cache-mount.service

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

  # Point rclone at the SSHFS server and, with --replace-sshfs, swap the mounts.
  # plex-cache-switch tests first and puts SSHFS back if the new mount fails.
  if [ -n "$SFTP" ] || [ "$REPLACE_SSHFS" -eq 1 ]; then
    systemctl daemon-reload
    SWITCH=(/usr/local/sbin/plex-cache-switch)
    [ -n "$SFTP" ] && SWITCH+=(--sftp "$SFTP")
    [ -n "$SSH_KEY" ] && SWITCH+=(--ssh-key "$SSH_KEY")
    [ -n "$SSH_PORT" ] && SWITCH+=(--ssh-port "$SSH_PORT")
    [ -n "$MOUNT_AT" ] && SWITCH+=(--mountpoint "$MOUNT_AT")
    [ "$REPLACE_SSHFS" -eq 1 ] || SWITCH+=(--configure-only)
    "${SWITCH[@]}" || SWITCH_FAILED=1
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
  # Shared secret: nginx sends it after the login, and the status page refuses
  # requests without it, so other users on this box can't use port $PORT directly.
  KEYFILE=/etc/plex-precache/status.key
  if [ ! -s "$KEYFILE" ]; then
    head -c 60 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' > "$KEYFILE"
  fi
  chown "$RUN_USER": "$KEYFILE"
  chmod 640 "$KEYFILE"
  (umask 077; sed -e "s/@PORT@/$PORT/" -e "s|/etc/htpasswd.d/htpasswd.@USER@|$HTPASSWD|" \
    -e "s/@KEY@/$(cat "$KEYFILE")/" \
    swizzin/plexcache.nginx.conf > /etc/nginx/apps/plexcache.conf)
  if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx
    echo "Status page: https://<your server>/plexcache/"
  else
    rm -f /etc/nginx/apps/plexcache.conf "$KEYFILE"
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
  sed -i '/# BEGIN plex-cache/,/# END plex-cache/s/img = "plex"$/img = "plexcache"/' "$PROFILES"
  if [ -d /opt/swizzin/core/static/img/apps ]; then
    install -m 644 swizzin/plexcache.png /opt/swizzin/core/static/img/apps/plexcache.png
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
    # The Update and SSHFS switch buttons; each service runs a fixed root-owned script.
    for unit in plex-cache-update plex-cache-switch; do
      echo "$RUN_USER ALL=(root) NOPASSWD: $bin start --no-block $unit"
    done
  done
  # The switch's Test button: a read-only check.
  echo "$RUN_USER ALL=(root) NOPASSWD: /usr/local/sbin/plex-cache-switch --check"
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
if [ "$SWITCH_FAILED" -eq 1 ]; then
  echo "Installed, but the mount wasn't switched (see above). Your SSHFS mount is unchanged" \
       "or was put back. Try again with: sudo plex-cache-switch"
fi
echo "Installed. Next:"
[ "$WITH_MOUNT" -eq 1 ] && [ "$REPLACE_SSHFS" -eq 0 ] && echo "  sudo plex-cache-switch                     # move an SSHFS mount to the cached mount"
echo "  systemctl enable --now plex-precache"
echo "  journalctl -fu plex-precache             # watch it work"
echo "  plex-cache-resize --show                 # cache size and usage"
