#!/bin/bash
set -e

HOST_UID="${HOST_UID:?HOST_UID environment variable is required}"
HOST_GID="${HOST_GID:?HOST_GID environment variable is required}"
HOST_USER="${HOST_USER:?HOST_USER environment variable is required}"

# Create a group matching the host GID if none exists yet
if ! getent group "$HOST_GID" &>/dev/null; then
    groupadd -g "$HOST_GID" "$HOST_USER"
fi

# Create a user matching the host UID/GID/name if none exists yet
if ! getent passwd "$HOST_UID" &>/dev/null; then
    useradd -u "$HOST_UID" -g "$HOST_GID" -s /bin/bash "$HOST_USER"
fi

# Ensure the user owns their home directory.
# Docker may have pre-created it (root-owned) when setting up the bind mount.
USER_HOME=$(getent passwd "$HOST_UID" | cut -d: -f6)
chown "$HOST_UID:$HOST_GID" "$USER_HOME"

# Drop root privileges and run pi as the host user
exec runuser -u "$HOST_USER" -- pi "$@"
