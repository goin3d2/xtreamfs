#!/bin/sh
# If a stale FUSE mount from an unclean stop is sitting on the mount point,
# clear it. Only touch FUSE mounts: unmounting anything else would remove
# Docker's bind mount and hide the filesystem from the host.
if awk -v m="$MOUNT_POINT" '$5 == m && / - fuse/ { found = 1 } END { exit !found }' /proc/self/mountinfo; then
    fusermount3 -uz "$MOUNT_POINT" 2>/dev/null || true
fi
mkdir -p "$MOUNT_POINT" "$CACHE_DIR"
exec python /app/xtreamfs.py "$@"
