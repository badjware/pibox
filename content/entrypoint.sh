#!/bin/bash
set -e

HOST_UID="${HOST_UID:?HOST_UID environment variable is required}"
HOST_GID="${HOST_GID:?HOST_GID environment variable is required}"
HOST_USER="${HOST_USER:?HOST_USER environment variable is required}"
ENABLE_DOCKER="${ENABLE_DOCKER:-0}"
HARNESS="${HARNESS:-pi}"

# ---------------------------------------------------------------------------
# Mirror the host user inside the container so bind-mounted files keep
# consistent ownership on both sides.
# ---------------------------------------------------------------------------
getent group  "$HOST_GID" >/dev/null || groupadd -g "$HOST_GID" "$HOST_USER"
getent passwd "$HOST_UID" >/dev/null || useradd  -u "$HOST_UID" -g "$HOST_GID" -s /bin/bash "$HOST_USER" 2>/dev/null

# Docker may have pre-created HOME and bind-mount ancestor directories (root-owned)
# when setting up bind mounts. Fix ownership on HOME itself and any root-owned
# stub directories directly beneath it (.local, .local/share, etc.).
USER_HOME=$(getent passwd "$HOST_UID" | cut -d: -f6)
chown "$HOST_UID:$HOST_GID" "$USER_HOME"
for stub in "$USER_HOME/.local" "$USER_HOME/.local/share" "$USER_HOME/.claude"; do
    if [[ -d "$stub" ]] && [[ "$(stat -c '%u' "$stub")" == "0" ]]; then
        chown "$HOST_UID:$HOST_GID" "$stub"
    fi
done

# ---------------------------------------------------------------------------
# Optional: rootless Docker-in-Docker
#
# rootlesskit provides the user-namespace wrapper; slirp4netns handles
# networking; fuse-overlayfs is the storage driver (no kernel overlay needed).
# We export DOCKER_HOST / XDG_RUNTIME_DIR so the exec'd pi inherits them.
# ---------------------------------------------------------------------------
start_rootless_docker() {
    # Subordinate UID/GID mappings are required by newuidmap / newgidmap.
    echo "$HOST_USER:100000:65536" >> /etc/subuid
    echo "$HOST_USER:100000:65536" >> /etc/subgid

    # XDG_RUNTIME_DIR holds the docker socket. The bind-mounted socket file
    # lives here, so it survives rootlesskit's --copy-up=/run overlay and
    # remains reachable from the outer container at the same path.
    local runtime_dir="/run/user/$HOST_UID"
    mkdir -p "$runtime_dir"
    chown "$HOST_UID:$HOST_GID" "$runtime_dir"
    chmod 700 "$runtime_dir"

    local sock="$runtime_dir/docker.sock"
    local log=/tmp/dockerd-rootless.log

    runuser -u "$HOST_USER" -- \
        env \
            HOME="$USER_HOME" \
            XDG_RUNTIME_DIR="$runtime_dir" \
            XDG_DATA_HOME="$USER_HOME/.local/share" \
        rootlesskit \
            --net=slirp4netns \
            --mtu=65520 \
            --disable-host-loopback \
            --copy-up=/etc \
            --copy-up=/run \
        dockerd \
            --host="unix://$sock" \
            --storage-driver=fuse-overlayfs \
        </dev/null >>"$log" 2>&1 &
    local pid=$!

    echo "Starting rootless Docker daemon (pid=$pid)..." >&2
    local timeout=30
    until [[ -S "$sock" ]]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "ERROR: rootless dockerd died during startup. Log:" >&2
            cat "$log" >&2
            return 1
        fi
        if (( --timeout <= 0 )); then
            echo "ERROR: rootless dockerd did not become ready within 30s. Log:" >&2
            tail -50 "$log" >&2
            return 1
        fi
        sleep 1
    done
    echo "Rootless Docker daemon is ready (socket: $sock)." >&2

    export DOCKER_HOST="unix://$sock"
    export XDG_RUNTIME_DIR="$runtime_dir"
}

[[ "$ENABLE_DOCKER" == "1" ]] && start_rootless_docker

# ---------------------------------------------------------------------------
# Optional: install extra apt packages requested via --extra-package.
# ---------------------------------------------------------------------------
if [[ -n "${EXTRA_PACKAGES:-}" ]]; then
    echo "Installing extra packages: $EXTRA_PACKAGES" >&2
    apt-get update
    apt-get install -y --no-install-recommends $EXTRA_PACKAGES
    rm -rf /var/lib/apt/lists/*
fi

# Drop root privileges and run the chosen harness as the host user
case "$HARNESS" in
    pi)     exec runuser -u "$HOST_USER" -- pi "$@" ;;
    pi-acp) exec runuser -u "$HOST_USER" -- pi-acp "$@" ;;
    claude) exec runuser -u "$HOST_USER" -- claude "$@" ;;
    *)      echo "entrypoint: unknown HARNESS: $HARNESS" >&2; exit 2 ;;
esac
